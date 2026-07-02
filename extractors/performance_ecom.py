#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
performance_ecom.py - standalone extractor-module voor seo_scraper_v2.py.

Categorie 7 (PERFORMANCE) + categorie 8 (E-COMMERCE) bovenop de bestaande
"alles-scraper". De finalizer wired dit later in `seo_scraper_v2.py`
(READ-ONLY referentie, NIET wijzigen) - typisch in SiteScraper._analyse(),
ongeveer zo:

    from extractors import performance_ecom
    page.update(performance_ecom.extract({
        "url": url, "html": res["html"], "soup": soup, "resp": res.get("resp"),
        "base_url": f"{p.scheme}://{self.domain}", "session": self.session,
        "rendered": res["rendered"],
        "psi": self.psi, "psi_key": self.psi_key,   # optioneel
    }))

CONTRACT
    extract(ctx) -> dict        # platte dict met UITSLUITEND nieuwe velden
    ctx keys:
        url, html, soup (bs4/lxml), resp (requests.Response | None),
        base_url, session (requests.Session), rendered (bool),
        optioneel: psi (bool, default False), psi_key (str | None),
        optioneel: psi_desktop (bool) -> voegt desktop-scores toe

Regels:
  - alleen stdlib + bs4 + requests (geen extra deps).
  - elk veld/elke groep in eigen try/except -> extract() crasht NOOIT.
  - velden die AL bestaan worden NIET gedupliceerd. Bestaand (overslaan):
    response_ms, size_kb (HTML), en producten{name,description,brand,sku,
    price,currency,availability,rating,rating_count,image,url}.
  - PSI (PageSpeed Insights) faalt ALTIJD zacht (quota/429/timeout) ->
    velden None + psi_note="psi_skipped"/"psi_error: ...".
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_TIMEOUT = 60  # ruim: PSI-call mag ~20-40s duren
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

__all__ = ["extract"]


# ============================ mini-helpers ================================
def _try(thunk, default=None):
    try:
        return thunk()
    except Exception:
        return default


def _num(x):
    """Coerce prijs/getal -> int/float; None bij mislukking."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return x
    s = str(x).strip().replace(" ", "").replace(" ", "")
    s = re.sub(r"[^0-9,.\-]", "", s)
    if not s or s in ("-", ".", ","):
        return None
    if "," in s and "." in s:          # 1.234,56 -> 1234.56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:                     # 234,00 -> 234.00
        s = s.replace(",", ".")
    try:
        f = float(s)
        return int(f) if f.is_integer() else round(f, 4)
    except Exception:
        return None


def _last_seg(v):
    if not v:
        return ""
    if isinstance(v, dict):
        v = v.get("@id") or v.get("url") or v.get("name") or ""
    return str(v).rstrip("/").split("/")[-1]


def _round(v):
    return int(round(v)) if isinstance(v, (int, float)) else None


# ============================ JSON-LD helpers =============================
def _iter_jsonld(soup):
    out = []
    for s in soup.find_all("script", type=lambda v: bool(v) and "ld+json" in v.lower()):
        raw = (s.string or s.get_text() or "").strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except Exception:
            # redding-poging bij rommel rond het JSON-object
            try:
                out.append(json.loads(raw[raw.index("{"): raw.rindex("}") + 1]))
            except Exception:
                pass
    return out


def _walk(node):
    """Alle dicts in (geneste) JSON-LD, incl. @graph en lijsten."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for it in node:
            yield from _walk(it)


def _has_type(node, *types):
    if not isinstance(node, dict):
        return False
    t = node.get("@type")
    ts = t if isinstance(t, list) else [t]
    low = {str(x).lower() for x in ts if x}
    return any(tp.lower() in low for tp in types)


def _name_of(v):
    if isinstance(v, dict):
        return v.get("name") or v.get("@id") or ""
    if isinstance(v, list):
        for it in v:
            n = _name_of(it)
            if n:
                return n
        return ""
    return v or ""


# ===================== 7a. PERFORMANCE uit headers =======================
_PROTO_MAP = {9: "HTTP/0.9", 10: "HTTP/1.0", 11: "HTTP/1.1", 20: "HTTP/2", 30: "HTTP/3"}


def _perf_defaults():
    return {
        "compression": None,
        "caching_headers": {"cache_control": None, "etag": None,
                            "expires": None, "last_modified": None},
        "has_caching": None,
        "http_protocol": None,
        "keep_alive": None,
    }


def _perf_headers(ctx):
    out = _perf_defaults()
    resp = ctx.get("resp")
    if resp is None:
        return out
    h = _try(lambda: resp.headers, {}) or {}

    enc = _try(lambda: (h.get("Content-Encoding") or "").strip().lower())
    out["compression"] = enc if enc else "none"

    cc = _try(lambda: h.get("Cache-Control"))
    etag = _try(lambda: h.get("ETag"))
    expires = _try(lambda: h.get("Expires"))
    lm = _try(lambda: h.get("Last-Modified"))
    out["caching_headers"] = {"cache_control": cc, "etag": etag,
                              "expires": expires, "last_modified": lm}

    def _caches():
        if etag or lm or expires:
            return True
        if cc:
            low = cc.lower()
            if "no-store" in low:
                return False
            if "max-age=0" in low and "s-maxage" not in low:
                return False
            if "max-age" in low or "public" in low or "immutable" in low:
                return True
        return False
    out["has_caching"] = _try(_caches, False)

    # protocol (LET OP: standaard requests/urllib3 spreekt HTTP/1.1;
    #           h2/h3 zie je hier zelden -> proxy voor onderzoek, geen waarheid)
    proto = _try(lambda: getattr(resp.raw, "version_string", None))
    if not proto:
        vi = _try(lambda: getattr(resp.raw, "version", None))
        if isinstance(vi, int):
            proto = _PROTO_MAP.get(vi, "HTTP/%s" % (vi / 10.0))
    out["http_protocol"] = proto

    conn = _try(lambda: (h.get("Connection") or "").strip().lower(), "")
    ka_hdr = _try(lambda: h.get("Keep-Alive"))
    if conn:
        out["keep_alive"] = ("keep-alive" in conn) or (conn != "close" and proto == "HTTP/1.1")
    elif ka_hdr:
        out["keep_alive"] = True
    elif proto in ("HTTP/1.1", "HTTP/2", "HTTP/3"):
        out["keep_alive"] = True   # persistente connecties zijn hier de norm
    return out


# ===================== 7b. PERFORMANCE via PageSpeed =====================
_PSI_FIELD = ["lcp_field", "inp_field", "cls_field", "fcp_field", "ttfb_field"]
_PSI_LAB = ["fcp", "lcp", "tbt", "cls", "speed_index", "tti", "total_byte_weight",
            "network_requests", "render_blocking_resources", "unused_css_kb",
            "unused_js_kb", "uses_text_compression", "dom_size", "server_response_time",
            "page_size_total_kb"]
_PSI_SCORE = ["performance_score", "seo_score", "accessibility_score", "best_practices_score"]


def _psi_defaults(note):
    d = {}
    for k in _PSI_FIELD:
        d[k] = {"percentile": None, "category": None}
    for k in _PSI_LAB:
        d[k] = None
    d["render_blocking_resources"] = {"count": None, "ms": None}
    for k in _PSI_SCORE:
        d[k] = None
    d["psi_strategy"] = None
    d["psi_desktop"] = None
    d["psi_note"] = note
    return d


def _norm_cat(c):
    if not c:
        return None
    u = str(c).upper()
    if u in ("FAST", "GOOD"):
        return "good"
    if u in ("AVERAGE", "NEEDS_IMPROVEMENT", "NI"):
        return "ni"
    if u in ("SLOW", "POOR"):
        return "poor"
    return None


def _field_metric(metrics, key):
    m = metrics.get(key) if isinstance(metrics, dict) else None
    if not isinstance(m, dict):
        return {"percentile": None, "category": None}
    return {"percentile": m.get("percentile"), "category": _norm_cat(m.get("category"))}


def _audit_num(audits, aid):
    a = audits.get(aid)
    return a.get("numericValue") if isinstance(a, dict) else None


def _audit_items(audits, aid):
    a = audits.get(aid)
    if not isinstance(a, dict):
        return None
    items = (a.get("details") or {}).get("items")
    return len(items) if isinstance(items, list) else None


def _audit_savings_kb(audits, aid):
    a = audits.get(aid)
    if not isinstance(a, dict):
        return None
    b = (a.get("details") or {}).get("overallSavingsBytes")
    if b is None:
        b = a.get("numericValue")
    return round(b / 1024.0, 1) if isinstance(b, (int, float)) else None


def _score(cats, key):
    c = cats.get(key)
    if not isinstance(c, dict):
        return None
    s = c.get("score")
    return int(round(s * 100)) if isinstance(s, (int, float)) else None


def _parse_psi(data, strategy):
    out = _psi_defaults(None)
    out["psi_strategy"] = strategy

    # ---- VELD-data (echte gebruikers; CrUX via loadingExperience) ----
    metrics = (data.get("loadingExperience") or {}).get("metrics") or {}
    out["lcp_field"] = _field_metric(metrics, "LARGEST_CONTENTFUL_PAINT_MS")
    out["inp_field"] = _field_metric(metrics, "INTERACTION_TO_NEXT_PAINT")
    out["cls_field"] = _field_metric(metrics, "CUMULATIVE_LAYOUT_SHIFT_SCORE")
    out["fcp_field"] = _field_metric(metrics, "FIRST_CONTENTFUL_PAINT_MS")
    out["ttfb_field"] = _field_metric(metrics, "EXPERIMENTAL_TIME_TO_FIRST_BYTE")

    # ---- LAB-data (Lighthouse) ----
    lh = data.get("lighthouseResult") or {}
    audits = lh.get("audits") or {}
    out["fcp"] = _round(_audit_num(audits, "first-contentful-paint"))
    out["lcp"] = _round(_audit_num(audits, "largest-contentful-paint"))
    out["tbt"] = _round(_audit_num(audits, "total-blocking-time"))
    cls = _audit_num(audits, "cumulative-layout-shift")
    out["cls"] = round(cls, 3) if isinstance(cls, (int, float)) else None
    out["speed_index"] = _round(_audit_num(audits, "speed-index"))
    out["tti"] = _round(_audit_num(audits, "interactive"))
    tbw = _audit_num(audits, "total-byte-weight")
    out["total_byte_weight"] = int(tbw) if isinstance(tbw, (int, float)) else None
    out["page_size_total_kb"] = round(tbw / 1024.0, 1) if isinstance(tbw, (int, float)) else None
    out["network_requests"] = _audit_items(audits, "network-requests")

    rb = audits.get("render-blocking-resources") or {}
    rb_ms = (rb.get("details") or {}).get("overallSavingsMs")
    if rb_ms is None:
        rb_ms = rb.get("numericValue")
    out["render_blocking_resources"] = {
        "count": _audit_items(audits, "render-blocking-resources"),
        "ms": _round(rb_ms),
    }
    out["unused_css_kb"] = _audit_savings_kb(audits, "unused-css-rules")
    out["unused_js_kb"] = _audit_savings_kb(audits, "unused-javascript")

    utc = audits.get("uses-text-compression")
    if isinstance(utc, dict) and utc.get("score") is not None:
        out["uses_text_compression"] = (utc.get("score") == 1)
    dom = _audit_num(audits, "dom-size")
    out["dom_size"] = int(dom) if isinstance(dom, (int, float)) else None
    out["server_response_time"] = _round(_audit_num(audits, "server-response-time"))

    # ---- Scores (0-1 -> 0-100) ----
    cats = lh.get("categories") or {}
    out["performance_score"] = _score(cats, "performance")
    out["seo_score"] = _score(cats, "seo")
    out["accessibility_score"] = _score(cats, "accessibility")
    out["best_practices_score"] = _score(cats, "best-practices")
    return out


def _run_psi(ctx, strategy):
    if requests is None:
        raise RuntimeError("requests niet beschikbaar")
    url = ctx.get("url")
    if not url:
        raise RuntimeError("geen url")
    params = [("url", url), ("strategy", strategy),
              ("category", "performance"), ("category", "seo"),
              ("category", "accessibility"), ("category", "best-practices")]
    key = ctx.get("psi_key")
    if key:
        params.append(("key", key))
    sess = ctx.get("session")
    getter = sess.get if sess is not None else requests.get
    headers = {"User-Agent": UA, "Accept": "application/json"}

    data = None
    for attempt in range(2):
        r = getter(PSI_ENDPOINT, params=params, headers=headers, timeout=PSI_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            break
        if r.status_code in (429, 500, 503) and attempt == 0:
            import time as _t
            _t.sleep(3)          # 1x korte backoff bij quota/overbelasting
            continue
        msg = _try(lambda: r.json().get("error", {}).get("message")) or ("HTTP %s" % r.status_code)
        raise RuntimeError("%s" % msg)
    if data is None:
        raise RuntimeError("HTTP 429/5xx na retry")
    return _parse_psi(data, strategy)


def _perf_psi(ctx):
    if not ctx.get("psi"):
        return _psi_defaults("psi_skipped")
    try:
        out = _run_psi(ctx, "mobile")
    except Exception as e:
        return _psi_defaults("psi_error: " + str(e)[:160])
    if ctx.get("psi_desktop"):
        try:
            dt = _run_psi(ctx, "desktop")
            out["psi_desktop"] = {k: dt.get(k) for k in _PSI_SCORE}
        except Exception as e:
            out["psi_desktop"] = {"psi_note": "psi_error: " + str(e)[:120]}
    return out


# ========================= 8. E-COMMERCE =================================
def _ecom_defaults():
    return {
        "gtin": None, "gtin13": None, "ean": None, "upc": None, "mpn": None,
        "sale_price": None, "compare_at_price": None,
        "price_valid_until": None, "condition": None,
        "reviews": [],
        "offer_shipping": {"present": False},
        "offer_return": {"present": False},
        "multiple_offers": {"count": None, "low": None, "high": None},
        "variants": [],
        "stock_count": None,
        "ecommerce_summary": {
            "has_product_schema": False, "has_price": False,
            "has_availability": False, "has_gtin": False,
            "has_brand": False, "merchant_listing_ready": False,
        },
    }


def _first_offer(node):
    if not isinstance(node, dict):
        return {}
    off = node.get("offers")
    if isinstance(off, list):
        return off[0] if (off and isinstance(off[0], dict)) else {}
    return off if isinstance(off, dict) else {}


def _list_price_from_spec(ps):
    """Strikethrough / 'van'-prijs uit priceSpecification met priceType ListPrice."""
    if not ps:
        return None
    for s in (ps if isinstance(ps, list) else [ps]):
        if not isinstance(s, dict):
            continue
        pt = str(s.get("priceType") or "").lower()
        if "listprice" in pt or "list_price" in pt:
            return _num(s.get("price"))
    return None


def _shipping(offer, node):
    sd = offer.get("shippingDetails") or node.get("shippingDetails")
    if isinstance(sd, list):
        sd = sd[0] if sd else None
    if not isinstance(sd, dict):
        return {"present": False}
    rate = sd.get("shippingRate")
    if isinstance(rate, list):
        rate = rate[0] if rate else {}
    rate = rate if isinstance(rate, dict) else {}
    val = _num(rate.get("value"))
    dest = sd.get("shippingDestination")
    if isinstance(dest, list):
        dest = dest[0] if dest else {}
    country = None
    if isinstance(dest, dict):
        country = dest.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name") or country.get("addressCountry")
    return {
        "present": True,
        "rate": val,
        "currency": rate.get("currency") or rate.get("priceCurrency"),
        "free": (val == 0) if val is not None else None,
        "country": country,
    }


def _return(offer, node):
    rp = offer.get("hasMerchantReturnPolicy") or node.get("hasMerchantReturnPolicy")
    if isinstance(rp, list):
        rp = rp[0] if rp else None
    if not isinstance(rp, dict):
        return {"present": False}
    days = rp.get("merchantReturnDays")
    return {
        "present": True,
        "days": _num(days) if days is not None else None,
        "policy": _last_seg(rp.get("returnPolicyCategory")) or None,
    }


def _multi_offers(node):
    off = node.get("offers")
    if isinstance(off, dict) and (_has_type(off, "AggregateOffer")
                                  or off.get("offerCount")
                                  or (off.get("lowPrice") and off.get("highPrice"))):
        return {"count": _num(off.get("offerCount")),
                "low": _num(off.get("lowPrice")),
                "high": _num(off.get("highPrice"))}
    if isinstance(off, list) and len(off) > 1:
        prices = [p for p in (_num(o.get("price")) for o in off if isinstance(o, dict))
                  if p is not None]
        return {"count": len(off),
                "low": min(prices) if prices else None,
                "high": max(prices) if prices else None}
    hv = node.get("hasVariant")          # ProductGroup -> tel de varianten als offers
    if hv:
        vs = [v for v in (hv if isinstance(hv, list) else [hv]) if isinstance(v, dict)]
        if len(vs) > 1:
            prices = [p for p in (_num(_first_offer(v).get("price")) for v in vs)
                      if p is not None]
            return {"count": len(vs),
                    "low": min(prices) if prices else None,
                    "high": max(prices) if prices else None}
    if off:
        return {"count": 1, "low": None, "high": None}
    return {"count": None, "low": None, "high": None}


def _prop(node, *names):
    ap = node.get("additionalProperty")
    if not ap:
        return None
    low = {n.lower() for n in names}
    for it in (ap if isinstance(ap, list) else [ap]):
        if isinstance(it, dict) and str(it.get("name") or "").lower() in low:
            return it.get("value")
    return None


def _axes_nl(axes):
    out = []
    for a in axes:
        seg = _last_seg(a).lower()
        if "colour" in seg or "color" in seg or "kleur" in seg:
            out.append("kleur")
        elif "size" in seg or "maat" in seg:
            out.append("maat")
        elif seg:
            out.append(seg)
    return sorted(set(out)) or None


def _variants(primary, group_nodes):
    axes, sources, seen = [], [], set()
    pool = list(group_nodes or [])
    if primary is not None and all(id(primary) != id(g) for g in pool):
        pool.append(primary)
    for g in pool:
        if not isinstance(g, dict):
            continue
        vb = g.get("variesBy")
        if vb:
            axes.extend(vb if isinstance(vb, list) else [vb])
        hv = g.get("hasVariant")
        if hv:
            for v in (hv if isinstance(hv, list) else [hv]):
                if isinstance(v, dict) and id(v) not in seen:
                    seen.add(id(v))
                    sources.append(v)
    axes_nl = _axes_nl(axes)
    variants = []
    for v in sources:
        if not isinstance(v, dict):
            continue
        off = _first_offer(v)
        variants.append({
            "name": v.get("name"),
            "sku": v.get("sku"),
            "price": _num(off.get("price")),
            "kleur": v.get("color") or _prop(v, "color", "kleur"),
            "maat": v.get("size") or _prop(v, "size", "maat"),
            "varies_by": axes_nl,
        })
    return variants[:60]


def _stock(offer, node, soup):
    for src in (offer, node):
        if not isinstance(src, dict):
            continue
        inv = src.get("inventoryLevel")
        if isinstance(inv, dict):
            v = _num(inv.get("value"))
            if v is not None:
                return v
        v = _num(inv) if not isinstance(inv, dict) else None
        if v is not None:
            return v
    # HTML-fallback
    try:
        for el in soup.select("[data-stock], [data-qty], [data-quantity], [itemprop=inventoryLevel]"):
            for attr in ("data-stock", "data-qty", "data-quantity", "content"):
                v = _num(el.get(attr))
                if v is not None and 0 <= v < 100000:
                    return v
    except Exception:
        pass
    try:
        txt = soup.get_text(" ", strip=True)
        m = re.search(r"(?:nog|nog maar|laatste)\s+(\d{1,4})\s+"
                      r"(?:stuks?|op voorraad|beschikbaar|in voorraad)", txt, re.I)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _reviews(nodes):
    out, seen = [], set()
    for n in nodes:
        if not isinstance(n, dict):
            continue
        rv = n.get("review")
        items = list(rv if isinstance(rv, list) else ([rv] if rv else []))
        if _has_type(n, "Review"):
            items.append(n)
        for r in items:
            if not isinstance(r, dict):
                continue
            author = _name_of(r.get("author"))
            rr = r.get("reviewRating") or r.get("ratingValue")
            rating = _num(rr.get("ratingValue")) if isinstance(rr, dict) else _num(rr)
            date = r.get("datePublished") or r.get("dateCreated")
            body = r.get("reviewBody") or r.get("description") or ""
            body = body.strip()[:1000] if isinstance(body, str) else ""
            key = (author, rating, str(date), body[:60])
            if key in seen:
                continue
            seen.add(key)
            out.append({"author": author or None, "rating": rating,
                        "date": date, "body": body or None})
    return out[:50]


def _microdata(soup):
    res = {"found": False}
    try:
        scope = soup.find(attrs={"itemtype": re.compile(r"schema.org/Product", re.I)})
        if not scope:
            return res
        res["found"] = True

        def ip(prop):
            el = scope.find(attrs={"itemprop": prop})
            if not el:
                return None
            return (el.get("content") or el.get_text(" ", strip=True) or "").strip() or None
        img_el = scope.find("img")
        res["name"] = ip("name")
        res["brand"] = ip("brand")
        res["image"] = ip("image") or (img_el.get("src") if img_el else None)
        res["price"] = ip("price")
        res["priceCurrency"] = ip("priceCurrency")
        res["availability"] = ip("availability")
        res["gtin13"] = ip("gtin13") or ip("gtin")
        res["mpn"] = ip("mpn")
    except Exception:
        pass
    return res


def _ecommerce(ctx):
    out = _ecom_defaults()
    soup = ctx.get("soup")
    if soup is None:
        return out

    product_like, group_nodes, review_nodes = [], [], []
    for d in _iter_jsonld(soup):
        for n in _walk(d):
            if not isinstance(n, dict):
                continue
            if _has_type(n, "Product", "ProductGroup"):
                product_like.append(n)
            if _has_type(n, "ProductGroup"):
                group_nodes.append(n)
            if _has_type(n, "Review"):
                review_nodes.append(n)

    # hasVariant-kinderen zijn varianten, NIET het hoofdproduct -> uitsluiten
    variant_ids = set()
    for g in product_like:
        hv = g.get("hasVariant")
        if hv:
            for v in (hv if isinstance(hv, list) else [hv]):
                if isinstance(v, dict):
                    variant_ids.add(id(v))
    candidates = [n for n in product_like if id(n) not in variant_ids]

    primary = None
    for n in candidates:                 # 1) ProductGroup mét offers (de ouder)
        if _has_type(n, "ProductGroup") and n.get("offers"):
            primary = n
            break
    if primary is None:                  # 2) elk hoofdproduct mét offers
        for n in candidates:
            if n.get("offers"):
                primary = n
                break
    if primary is None and candidates:   # 3) eerste hoofd-kandidaat
        primary = candidates[0]
    if primary is None and product_like:  # 4) desnoods een variant
        primary = product_like[0]

    micro = _try(lambda: _microdata(soup), {"found": False}) or {"found": False}

    if primary:
        offer = _first_offer(primary)

        def pick(*keys):
            for src in (primary, offer):
                for k in keys:
                    v = src.get(k)
                    if v not in (None, "", []):
                        return v
            return None

        out["gtin"] = _try(lambda: pick("gtin", "gtin14", "gtin8"))
        out["gtin13"] = _try(lambda: pick("gtin13"))
        out["mpn"] = _try(lambda: pick("mpn"))
        out["upc"] = _try(lambda: pick("upc", "gtin12"))
        out["ean"] = _try(lambda: pick("ean") or out["gtin13"]
                          or (out["gtin"] if out["gtin"] and len(str(out["gtin"])) == 13 else None))

        price = _num(offer.get("price") or offer.get("lowPrice"))
        high = _num(offer.get("highPrice"))
        list_price = _try(lambda: _list_price_from_spec(offer.get("priceSpecification")))
        explicit = _num(offer.get("listPrice") or primary.get("listPrice"))
        compare_at = None
        for cand in (list_price, explicit, high):
            if cand is not None and price is not None and cand > price:
                compare_at = cand
                break
        out["compare_at_price"] = compare_at
        out["sale_price"] = price if (compare_at is not None and price is not None) else None
        out["price_valid_until"] = _try(lambda: offer.get("priceValidUntil"))
        out["condition"] = _try(lambda: _last_seg(offer.get("itemCondition")
                                                  or primary.get("itemCondition")) or None)

        out["offer_shipping"] = _try(lambda: _shipping(offer, primary), {"present": False})
        out["offer_return"] = _try(lambda: _return(offer, primary), {"present": False})
        out["multiple_offers"] = _try(lambda: _multi_offers(primary),
                                      {"count": None, "low": None, "high": None})
        out["stock_count"] = _try(lambda: _stock(offer, primary, soup))
        out["variants"] = _try(lambda: _variants(primary, group_nodes), [])
    elif micro.get("found"):
        out["gtin13"] = micro.get("gtin13")
        out["mpn"] = micro.get("mpn")
        out["ean"] = micro.get("gtin13")

    out["reviews"] = _try(lambda: _reviews(product_like + review_nodes), [])

    # ---- samenvatting (Merchant-readiness) ----
    p_offer = _first_offer(primary) if primary else {}
    name_present = bool((primary.get("name") if primary else None) or micro.get("name"))
    img_present = bool((primary.get("image") if primary else None) or micro.get("image"))
    price_present = bool(p_offer.get("price") or p_offer.get("lowPrice")
                         or micro.get("price") or out["sale_price"] or out["compare_at_price"])
    cur_present = bool(p_offer.get("priceCurrency") or micro.get("priceCurrency"))
    avail_present = bool(p_offer.get("availability") or micro.get("availability"))
    brand_present = bool((primary.get("brand") if primary else None) or micro.get("brand"))
    gtin_present = any([out["gtin"], out["gtin13"], out["ean"], out["upc"]])

    out["ecommerce_summary"] = {
        "has_product_schema": bool(primary) or bool(micro.get("found")),
        "has_price": bool(price_present),
        "has_availability": bool(avail_present),
        "has_gtin": bool(gtin_present),
        "has_brand": bool(brand_present),
        "merchant_listing_ready": bool(name_present and img_present and price_present
                                       and cur_present and avail_present),
    }
    return out


# ============================== PUBLIC API ===============================
def extract(ctx):
    """Platte dict met UITSLUITEND nieuwe velden (cat. 7 + 8). Crasht nooit."""
    ctx = ctx or {}
    out = {}

    # 7a. performance uit headers (goedkoop, altijd)
    try:
        out.update({**_perf_defaults(), **_perf_headers(ctx)})
    except Exception:
        out.update(_perf_defaults())

    # 7b. PageSpeed Insights (alleen bij psi=True; faalt zacht)
    try:
        out.update(_perf_psi(ctx))
    except Exception as e:
        out.update(_psi_defaults("psi_error: " + str(e)[:160]))

    # 8. e-commerce (JSON-LD Product/Offer + microdata)
    try:
        out.update({**_ecom_defaults(), **_ecommerce(ctx)})
    except Exception:
        out.update(_ecom_defaults())

    return out


# ============================== ZELFTEST =================================
if __name__ == "__main__":
    import sys
    from bs4 import BeautifulSoup

    HEADERS = {"User-Agent": UA, "Accept-Language": "nl,en;q=0.8",
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

    def build_ctx(url, session, psi=False, psi_key=None, psi_desktop=False):
        resp, html = None, ""
        try:
            resp = session.get(url, timeout=25, allow_redirects=True)
            html = resp.text
        except Exception as exc:
            print("  [fetch-fout] %s: %s" % (url, exc), file=sys.stderr)
        soup = BeautifulSoup(html, "lxml")
        p = urlparse(resp.url if resp is not None else url)
        return {
            "url": resp.url if resp is not None else url,
            "html": html, "soup": soup, "resp": resp,
            "base_url": "%s://%s" % (p.scheme, p.netloc),
            "session": session, "rendered": False,
            "psi": psi, "psi_key": psi_key, "psi_desktop": psi_desktop,
        }

    sess = requests.Session()
    sess.headers.update(HEADERS)

    targets = [
        "https://zekermobiel.nl/p/medema-mini-crosser-x2.html",  # echte PDP met Product-schema
        "https://www.coolblue.nl/",
    ]
    print("=" * 72)
    print("DEMO 1 & 2 - extract() met psi=False (snel: headers + e-commerce)")
    print("=" * 72)
    for u in targets:
        ctx = build_ctx(u, sess, psi=False)
        status = ctx["resp"].status_code if ctx["resp"] is not None else "ERR"
        print("\n### %s  (HTTP %s, ~%d KB)" % (u, status, len(ctx["html"]) // 1024))
        print(json.dumps(extract(ctx), indent=2, ensure_ascii=False))

    print("\n" + "=" * 72)
    print("DEMO 3 - extract() met psi=True op de homepage (bewijst PSI-parsing)")
    print("PSI kan ~20-40s duren en zonder API-key 429'en -> dan faalt het zacht.")
    print("=" * 72)
    psi_url = "https://www.coolblue.nl/"
    ctx = build_ctx(psi_url, sess, psi=True, psi_key=None)
    print("\n### %s  (psi=True, strategy=mobile)" % psi_url)
    res = extract(ctx)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    print("\n[PSI-status] note=%r  performance_score=%r  seo_score=%r  lcp_field=%r"
          % (res.get("psi_note"), res.get("performance_score"),
             res.get("seo_score"), res.get("lcp_field")))
