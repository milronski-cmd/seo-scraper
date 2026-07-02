#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
technical_schema.py — standalone extractor-module voor seo_scraper_v2.py.

Levert categorie 3 (TECHNISCHE SEO) + categorie 6 (STRUCTURED DATA).
De finalizer wired deze module later in `seo_scraper_v2.py`; dit bestand
WIJZIGT die scraper niet en is op zichzelf draaibaar (zie __main__ onderaan).

CONTRACT
--------
    def extract(ctx) -> dict

ctx-keys:
    url        : str            — definitieve URL (na redirects)
    html       : str            — ruwe HTML
    soup       : BeautifulSoup  — geparset met lxml
    resp       : requests.Response (.status_code/.headers/.elapsed/.history/.raw)
    base_url   : str            — scheme://host
    session    : requests.Session (voor robots.txt / extra calls; site-breed cachen)
    rendered   : bool           — kwam de HTML uit een headless render?

Return: een PLATTE dict met UITSLUITEND nieuwe velden. Elk veld wordt in een
eigen try/except opgebouwd, zodat extract() nooit crasht (faalt zacht -> {"error": ...}
of een lege/neutrale waarde). Alleen stdlib + bs4 + requests.

NIET gedupliceerd (zit al in de scraper): status, response_ms, https, robots.txt
(opgehaald+gerespecteerd), sitemap, canonical, meta_robots, jsonld(+types-lijst),
Product + BreadcrumbList parsing, microdata(Product).
"""
import json
import re
import socket
import ssl
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import urljoin, urlparse

# --------------------------------------------------------------------------- #
# Site-brede caches (per host) — voorkomt herhaalde robots/TLS-calls per pagina #
# --------------------------------------------------------------------------- #
_ROBOTS_CACHE = {}   # host -> (robots_text, RobotFileParser)
_TLS_CACHE = {}      # host -> tls_cert dict

AI_BOTS = ["GPTBot", "Google-Extended", "ClaudeBot", "anthropic-ai",
           "CCBot", "PerplexityBot", "Bytespider", "Applebot-Extended"]

SECURITY_HEADERS = {
    "hsts": "Strict-Transport-Security",
    "csp": "Content-Security-Policy",
    "x_frame_options": "X-Frame-Options",
    "x_content_type_options": "X-Content-Type-Options",
    "referrer_policy": "Referrer-Policy",
    "permissions_policy": "Permissions-Policy",
}

# JSON-LD types waarvoor we de kern-velden uitparsen (cat. 6)
IMPORTANT_SCHEMA_TYPES = {
    "organization", "localbusiness", "website", "article", "newsarticle",
    "blogposting", "faqpage", "product", "offer", "review", "aggregaterating",
    "videoobject", "event", "recipe", "person",
}

# Door Google (de-facto) vereiste velden per type voor een geldig rich result.
# 'one_of' = minstens één van deze moet aanwezig zijn.
SCHEMA_REQUIRED = {
    "product":        {"required": ["name"], "one_of": ["offers", "review", "aggregateRating"],
                       "recommended": ["image", "description", "brand", "sku"]},
    "offer":          {"required": ["price", "priceCurrency", "availability"]},
    "article":        {"required": ["headline", "image", "datePublished", "author"]},
    "newsarticle":    {"required": ["headline", "image", "datePublished", "author"]},
    "blogposting":    {"required": ["headline", "image", "datePublished", "author"]},
    "breadcrumblist": {"required": ["itemListElement"]},
    "faqpage":        {"required": ["mainEntity"], "special": "faq"},
    "qapage":         {"required": ["mainEntity"]},
    "organization":   {"required": ["name"], "recommended": ["logo", "url", "sameAs"]},
    "localbusiness":  {"required": ["name", "address"],
                       "recommended": ["telephone", "openingHours", "geo", "priceRange"]},
    "review":         {"required": ["itemReviewed", "reviewRating", "author"]},
    "aggregaterating": {"required": ["ratingValue"], "one_of": ["reviewCount", "ratingCount"]},
    "videoobject":    {"required": ["name", "description", "thumbnailUrl", "uploadDate"]},
    "event":          {"required": ["name", "startDate", "location"]},
    "recipe":         {"required": ["name", "image"],
                       "recommended": ["recipeIngredient", "recipeInstructions"]},
    "person":         {"required": ["name"]},
    "website":        {"required": ["name", "url"]},
    "jobposting":     {"required": ["title", "description", "datePosted", "hiringOrganization"]},
}

# Type -> naam van het Google rich result waarvoor het in aanmerking komt.
RICH_RESULT = {
    "product": "Product (prijs/sterren/voorraad)",
    "recipe": "Recept",
    "faqpage": "Veelgestelde vragen (FAQ)",
    "qapage": "Vraag & antwoord",
    "breadcrumblist": "Breadcrumb",
    "article": "Artikel / Top Stories",
    "newsarticle": "Artikel / Top Stories",
    "blogposting": "Artikel / Top Stories",
    "event": "Evenement",
    "videoobject": "Video",
    "review": "Review-snippet (sterren)",
    "aggregaterating": "Review-snippet (sterren)",
    "organization": "Organisatie-logo / knowledge panel",
    "localbusiness": "Lokaal bedrijf",
    "website": "Sitelinks-zoekvak",
    "jobposting": "Vacature",
    "person": "Profielpagina",
}


# --------------------------------------------------------------------------- #
# Kleine helpers                                                              #
# --------------------------------------------------------------------------- #
def _norm_url(u):
    """Normaliseer een URL voor vergelijking (host lowercase, trailing slash weg)."""
    try:
        p = urlparse((u or "").strip())
        path = p.path or "/"
        if len(path) > 1:
            path = path.rstrip("/")
        out = f"{p.scheme.lower()}://{p.netloc.lower()}{path}"
        if p.query:
            out += "?" + p.query
        return out
    except Exception:
        return (u or "").strip()


def _walk_jsonld(node):
    """Yield alle dicts in (mogelijk geneste) JSON-LD, incl. @graph en lijsten."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_jsonld(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)


def _types_of(node):
    """Lijst van lowercase @type-strings van een node."""
    t = node.get("@type")
    if t is None:
        return []
    types = t if isinstance(t, list) else [t]
    return [str(x).lower() for x in types if x]


def _parse_jsonld(soup):
    """Parse alle <script type=application/ld+json> robuust naar Python-objecten."""
    docs = []
    for s in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = s.string
        if raw is None:
            raw = s.get_text()
        if not raw:
            continue
        raw = raw.strip()
        # veelvoorkomende CDATA/comment-wrappers strippen
        raw = re.sub(r"^/\*\s*<!\[CDATA\[\s*\*/|/\*\s*\]\]>\s*\*/$", "", raw).strip()
        raw = re.sub(r"^<!\[CDATA\[|\]\]>$", "", raw).strip()
        try:
            docs.append(json.loads(raw))
        except Exception:
            try:
                docs.append(json.loads(raw.replace("\n", " ")))
            except Exception:
                continue
    return docs


def _name_of(v):
    """Best-effort naam uit een waarde die str/dict/list kan zijn."""
    if isinstance(v, dict):
        return str(v.get("name") or v.get("@id") or v.get("url") or "")
    if isinstance(v, list):
        return ", ".join(_name_of(x) for x in v if x)[:300]
    return str(v) if v is not None else ""


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _present(node, key):
    """Is een sleutel zinvol aanwezig (niet leeg/None)?"""
    if key not in node:
        return False
    val = node[key]
    return val not in (None, "", [], {})


def _to_float(v):
    try:
        if isinstance(v, str):
            v = v.replace(",", ".").strip()
        return float(v)
    except Exception:
        return None


def _offer_fields(off):
    off = _first(off)
    if not isinstance(off, dict):
        return {}
    return {
        "price": off.get("price", off.get("lowPrice", "")),
        "priceCurrency": off.get("priceCurrency", ""),
        "availability": str(off.get("availability", "")).split("/")[-1],
        "priceValidUntil": off.get("priceValidUntil", ""),
    }


def _visible_text(soup):
    """Zichtbare paginatekst (lowercase) zonder de DOM te muteren (andere
    extractors delen dezelfde soup)."""
    parts = []
    skip = {"script", "style", "noscript", "template", "svg"}
    for el in soup.find_all(string=True):
        parent = getattr(el, "parent", None)
        if parent is not None and parent.name in skip:
            continue
        s = str(el).strip()
        if s:
            parts.append(s)
    return " ".join(parts).lower()


# --------------------------------------------------------------------------- #
# Site-brede helpers (robots.txt + TLS)                                       #
# --------------------------------------------------------------------------- #
def _get_robots(ctx):
    """Haal robots.txt op (gecachet per host) en geef (tekst, RobotFileParser)."""
    host = urlparse(ctx.get("base_url") or ctx.get("url") or "").netloc.lower()
    if host in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[host]
    text = ""
    try:
        base = (ctx.get("base_url") or "").rstrip("/")
        r = ctx["session"].get(base + "/robots.txt", timeout=10)
        body = r.text or ""
        if r.status_code == 200 and "<html" not in body[:300].lower():
            text = body
    except Exception:
        text = ""
    rp = robotparser.RobotFileParser()
    try:
        rp.parse(text.splitlines())
    except Exception:
        pass
    _ROBOTS_CACHE[host] = (text, rp)
    return text, rp


def _tls_cert(host):
    """TLS-certificaat-info via ssl.getpeercert (host:443). Site-niveau, faalt zacht."""
    if host in _TLS_CACHE:
        return _TLS_CACHE[host]
    result = {"valid": False, "issuer": "", "expires": "", "days_until_expiry": None}
    try:
        ssl_ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=8) as sock:
            with ssl_ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert() or {}
        issuer = {}
        for rdn in cert.get("issuer", ()):
            for pair in rdn:
                if len(pair) == 2:
                    issuer[pair[0]] = pair[1]
        not_after = cert.get("notAfter", "")
        days = None
        if not_after:
            s = not_after.replace(" GMT", "").strip()
            exp = datetime.strptime(s, "%b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
            days = (exp - datetime.now(timezone.utc)).days
        result = {
            "valid": True,  # default-context valideert keten + hostname; handshake gelukt = geldig
            "issuer": issuer.get("organizationName") or issuer.get("commonName") or "",
            "expires": not_after,
            "days_until_expiry": days,
        }
    except ssl.SSLCertVerificationError as e:
        result = {"valid": False, "issuer": "", "expires": "",
                  "days_until_expiry": None, "error": f"cert-verificatie faalde: {e.reason if hasattr(e, 'reason') else e}"}
    except Exception as e:
        result = {"valid": False, "issuer": "", "expires": "",
                  "days_until_expiry": None, "error": str(e)}
    _TLS_CACHE[host] = result
    return result


def _http_version(resp):
    v = getattr(getattr(resp, "raw", None), "version", None)
    return {10: "HTTP/1.0", 11: "HTTP/1.1", 20: "HTTP/2.0", 30: "HTTP/3.0"}.get(v, str(v) if v else "")


def _cdn_detected(headers):
    h = {str(k).lower(): str(v or "") for k, v in headers.items()}
    server = h.get("server", "").lower()
    via = h.get("via", "").lower()
    found = []
    if "cf-ray" in h or "cloudflare" in server or "cloudflare" in via:
        found.append("cloudflare")
    if "x-vercel-id" in h or "x-vercel-cache" in h or "vercel" in server:
        found.append("vercel")
    if ("fastly" in server or "fastly" in via or "x-fastly-request-id" in h
            or ("x-served-by" in h and "cache" in h.get("x-served-by", ""))):
        found.append("fastly")
    if ("akamai" in server or "akamai" in via or "x-akamai-transformed" in h
            or any(k.startswith("x-akamai") for k in h)):
        found.append("akamai")
    if "x-amz-cf-id" in h or "cloudfront" in via or "cloudfront" in server:
        found.append("cloudfront")
    return ", ".join(dict.fromkeys(found))


# --------------------------------------------------------------------------- #
# Cat. 6 — kern-velden per schema-type uitparsen                              #
# --------------------------------------------------------------------------- #
def _detail_for(t, node):
    """Kern-velden voor één belangrijk JSON-LD type."""
    if t == "organization":
        return {"name": node.get("name", ""), "url": node.get("url", ""),
                "logo": _name_of(node.get("logo")), "sameAs": node.get("sameAs", ""),
                "contactPoint": bool(node.get("contactPoint"))}
    if t == "localbusiness":
        return {"name": node.get("name", ""), "telephone": node.get("telephone", ""),
                "address": _name_of(node.get("address")), "priceRange": node.get("priceRange", ""),
                "openingHours": node.get("openingHours") or node.get("openingHoursSpecification", ""),
                "geo": bool(node.get("geo"))}
    if t == "website":
        pa = node.get("potentialAction") or {}
        pa = _first(pa) or {}
        return {"name": node.get("name", ""), "url": node.get("url", ""),
                "searchAction": bool(pa and "searchaction" in str(pa.get("@type", "")).lower())}
    if t in ("article", "newsarticle", "blogposting"):
        return {"headline": node.get("headline", ""), "image": _name_of(node.get("image")),
                "datePublished": node.get("datePublished", ""), "dateModified": node.get("dateModified", ""),
                "author": _name_of(node.get("author")), "publisher": _name_of(node.get("publisher"))}
    if t == "faqpage":
        me = node.get("mainEntity") or []
        me = me if isinstance(me, list) else [me]
        questions = [q.get("name", "") for q in me if isinstance(q, dict) and q.get("name")]
        return {"question_count": len(questions), "questions": questions[:8]}
    if t == "product":
        agg = node.get("aggregateRating") or {}
        return {"name": node.get("name", ""), "brand": _name_of(node.get("brand")),
                "sku": node.get("sku", "") or node.get("mpn", ""),
                "offers": _offer_fields(node.get("offers")),
                "ratingValue": (agg.get("ratingValue") if isinstance(agg, dict) else ""),
                "reviewCount": (agg.get("reviewCount") or agg.get("ratingCount") if isinstance(agg, dict) else "")}
    if t == "offer":
        return _offer_fields(node)
    if t == "review":
        rr = node.get("reviewRating") or {}
        return {"author": _name_of(node.get("author")), "itemReviewed": _name_of(node.get("itemReviewed")),
                "ratingValue": (rr.get("ratingValue") if isinstance(rr, dict) else ""),
                "reviewBody": (node.get("reviewBody", "") or "")[:200]}
    if t == "aggregaterating":
        return {"ratingValue": node.get("ratingValue", ""),
                "reviewCount": node.get("reviewCount", ""), "ratingCount": node.get("ratingCount", ""),
                "bestRating": node.get("bestRating", ""), "worstRating": node.get("worstRating", "")}
    if t == "videoobject":
        return {"name": node.get("name", ""), "uploadDate": node.get("uploadDate", ""),
                "duration": node.get("duration", ""), "thumbnailUrl": _name_of(node.get("thumbnailUrl")),
                "contentUrl": node.get("contentUrl", "") or node.get("embedUrl", "")}
    if t == "event":
        return {"name": node.get("name", ""), "startDate": node.get("startDate", ""),
                "endDate": node.get("endDate", ""), "location": _name_of(node.get("location")),
                "offers": _offer_fields(node.get("offers"))}
    if t == "recipe":
        ing = node.get("recipeIngredient") or node.get("ingredients") or []
        return {"name": node.get("name", ""), "image": _name_of(node.get("image")),
                "ingredient_count": len(ing) if isinstance(ing, list) else 0,
                "totalTime": node.get("totalTime", ""), "nutrition": bool(node.get("nutrition")),
                "ratingValue": _name_of(node.get("aggregateRating"))}
    if t == "person":
        return {"name": node.get("name", ""), "url": node.get("url", ""),
                "jobTitle": node.get("jobTitle", ""), "sameAs": node.get("sameAs", "")}
    return {"@type": node.get("@type")}


def _schema_types_detailed(nodes):
    out = {}
    for node in nodes:
        for t in _types_of(node):
            if t in IMPORTANT_SCHEMA_TYPES:
                try:
                    detail = _detail_for(t, node)
                except Exception as e:
                    detail = {"error": str(e)}
                out.setdefault(t, [])
                if len(out[t]) < 25:
                    out[t].append(detail)
    return out


def _faq_valid(node):
    """FAQPage: mainEntity moet Question's met name + acceptedAnswer.text bevatten."""
    me = node.get("mainEntity") or []
    me = me if isinstance(me, list) else [me]
    good = 0
    for q in me:
        if not isinstance(q, dict):
            continue
        ans = q.get("acceptedAnswer") or {}
        ans = _first(ans) or {}
        if q.get("name") and isinstance(ans, dict) and ans.get("text"):
            good += 1
    return good > 0, good


def _schema_validation(nodes):
    """Per gevonden bekend type: check verplichte velden -> status."""
    results = []
    seen = set()
    for node in nodes:
        for t in _types_of(node):
            spec = SCHEMA_REQUIRED.get(t)
            if not spec:
                continue
            missing = []
            # speciale FAQ-validatie
            if spec.get("special") == "faq":
                ok, n_q = _faq_valid(node)
                if not ok:
                    missing.append("mainEntity[Question{name+acceptedAnswer.text}]")
            else:
                for f in spec.get("required", []):
                    if not _present(node, f):
                        missing.append(f)
            if "one_of" in spec and not any(_present(node, f) for f in spec["one_of"]):
                missing.append("|".join(spec["one_of"]))
            missing_rec = [f for f in spec.get("recommended", []) if not _present(node, f)]

            if missing:
                status = "invalid"
            elif missing_rec:
                status = "warning"
            else:
                status = "valid"

            entry = {"type": t, "missing_required": missing,
                     "missing_recommended": missing_rec, "status": status}
            key = (t, tuple(missing), tuple(missing_rec))
            if key not in seen:
                seen.add(key)
                results.append(entry)
    return results


def _rich_result_eligible(validation, nodes):
    """Welke types komen in aanmerking voor een rich result (geldig of warning)."""
    out = []
    seen = set()
    # WebSite alleen met SearchAction
    website_has_search = False
    for node in nodes:
        if "website" in _types_of(node):
            pa = _first(node.get("potentialAction") or {}) or {}
            if isinstance(pa, dict) and "searchaction" in str(pa.get("@type", "")).lower():
                website_has_search = True
    for v in validation:
        t = v["type"]
        if t not in RICH_RESULT or v["status"] == "invalid":
            continue
        if t == "website" and not website_has_search:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append({"type": t, "rich_result": RICH_RESULT[t], "status": v["status"]})
    return out


# --------------------------------------------------------------------------- #
# Cat. 6 — verzonnen sterren + RDFa/microdata + drift                         #
# --------------------------------------------------------------------------- #
def _fabricated_aggregaterating(nodes, soup, html, visible_text):
    """Detecteer (vermoedelijk) verzonnen AggregateRating-sterren — een Google-
    policy-overtreding. Flag bij: rating buiten 0..bestRating, count 0/absurd, of
    rating-schema zonder enige zichtbare review-/rating-indicator op de pagina."""
    aggs = []
    for node in nodes:
        types = _types_of(node)
        if "aggregaterating" in types:
            aggs.append(node)
        # AggregateRating genest in Product/anders
        sub = node.get("aggregateRating")
        if isinstance(sub, dict) and "aggregaterating" not in types:
            aggs.append(sub)
        # losse Review met reviewRating telt ook als 'rating-schema aanwezig'
        if ("review" in types) and isinstance(node.get("reviewRating"), dict):
            aggs.append(node.get("reviewRating"))

    if not aggs:
        return {"flag": False, "reason": "geen AggregateRating/Review-schema", "details": []}

    reasons = []
    details = []
    for agg in aggs:
        rv = _to_float(agg.get("ratingValue"))
        best = _to_float(agg.get("bestRating")) or 5.0
        worst = _to_float(agg.get("worstRating"))
        worst = 0.0 if worst is None else worst
        if rv is not None and (rv > best or rv < worst or rv > 5.0 and best <= 5.0):
            reasons.append("rating_buiten_schaal")
            details.append(f"ratingValue={agg.get('ratingValue')} (best={agg.get('bestRating') or 5})")
        count = agg.get("reviewCount", agg.get("ratingCount"))
        cnt = _to_float(count)
        if cnt is not None:
            if cnt == 0:
                reasons.append("count_nul")
                details.append("reviewCount/ratingCount = 0")
            elif cnt > 5_000_000:
                reasons.append("count_absurd")
                details.append(f"count = {count}")

    # zichtbare review-/rating-indicatoren op de pagina?
    has_visible = False
    try:
        has_visible = bool(
            re.search(r"\b\d(?:[.,]\d)?\s*(?:/|van|uit|out of)\s*5\b", visible_text)
            or re.search(r"\b\d{1,7}\s*(?:reviews?|beoordelingen|recensies|ratings?|sterren|stars?)\b", visible_text)
            or soup.find(class_=re.compile(r"star|rating|review|beoordel", re.I))
            or soup.find(attrs={"aria-label": re.compile(r"star|rating|beoordel|review", re.I)})
            or soup.find(attrs={"itemprop": re.compile(r"ratingValue|reviewCount|ratingCount", re.I)})
            or ("★" in html or "⭐" in html or "☆" in html)
        )
    except Exception:
        has_visible = True  # bij twijfel niet beschuldigen
    if not has_visible:
        reasons.append("geen_zichtbare_rating")
        details.append("rating-schema aanwezig maar geen zichtbare sterren/reviews op de pagina")

    reasons = list(dict.fromkeys(reasons))
    return {"flag": bool(reasons), "reason": ", ".join(reasons) if reasons else "geen verdachte signalen",
            "details": details, "aggregate_ratings_found": len(aggs)}


def _microdata_types(soup):
    """ALLE microdata itemtype-types (niet alleen Product)."""
    types = []
    for el in soup.find_all(attrs={"itemtype": True}):
        it = el.get("itemtype")
        for one in (it if isinstance(it, list) else [it]):
            if not one:
                continue
            name = str(one).rstrip("/").split("/")[-1]
            if name:
                types.append(name)
    return sorted(set(types))


def _rdfa_present(soup):
    """Sterke RDFa-indicatie: typeof/vocab-attributen (og: property op <meta> telt niet)."""
    try:
        if soup.find(attrs={"typeof": True}) or soup.find(attrs={"vocab": True}):
            return True
        # property= op niet-meta elementen (meta property=og:* is geen page-RDFa)
        for el in soup.find_all(attrs={"property": True}):
            if el.name != "meta":
                return True
    except Exception:
        pass
    return False


def _schema_drift(nodes, visible_text):
    """Best-effort: schema-prijs/availability/rating versus de zichtbare tekst."""
    mismatches = []
    checked = False

    # verzamel schema-waarden uit Product/Offer/AggregateRating
    prices, avails, ratings = [], [], []
    for node in nodes:
        types = _types_of(node)
        if "product" in types or "offer" in types:
            off = _offer_fields(node.get("offers")) if "product" in types else _offer_fields(node)
            if off.get("price") not in (None, ""):
                prices.append(str(off["price"]))
            if off.get("availability"):
                avails.append(str(off["availability"]).lower())
        agg = node.get("aggregateRating")
        if "aggregaterating" in types:
            agg = node
        if isinstance(agg, dict) and agg.get("ratingValue") not in (None, ""):
            ratings.append(str(agg.get("ratingValue")))

    # prijs: staat de schema-prijs ergens als zichtbaar bedrag?
    if prices:
        checked = True
        vis_prices = re.findall(r"\d{1,4}(?:[.,]\d{3})*(?:[.,]\d{2})", visible_text)
        vis_norm = set()
        for vp in vis_prices:
            f = _to_float(vp.replace(".", "").replace(",", ".")) if vp.count(",") == 1 else _to_float(vp.replace(",", ""))
            if f is not None:
                vis_norm.add(round(f, 2))
        for p in prices:
            pf = _to_float(p)
            if pf is not None and round(pf, 2) not in vis_norm and round(pf) not in {round(x) for x in vis_norm}:
                mismatches.append({"field": "price", "schema": p,
                                   "note": "schema-prijs niet teruggevonden in zichtbare tekst (low-confidence)"})

    # availability: schema InStock terwijl tekst uitverkocht zegt (of omgekeerd)
    if avails:
        checked = True
        oos = any(w in visible_text for w in
                  ("uitverkocht", "niet op voorraad", "niet leverbaar", "out of stock", "sold out", "tijdelijk uitverkocht"))
        ins = any(w in visible_text for w in
                  ("op voorraad", "in stock", "direct leverbaar", "leverbaar", "beschikbaar"))
        for a in avails:
            if "instock" in a and oos and not ins:
                mismatches.append({"field": "availability", "schema": a,
                                   "note": "schema=InStock maar tekst duidt op uitverkocht"})
            if ("outofstock" in a or "soldout" in a) and ins and not oos:
                mismatches.append({"field": "availability", "schema": a,
                                   "note": "schema=OutOfStock maar tekst duidt op leverbaar"})

    # rating: schema-rating nergens zichtbaar in de tekst
    if ratings:
        checked = True
        for r in ratings:
            rnum = r.replace(".", ",")
            if r not in visible_text and rnum not in visible_text:
                mismatches.append({"field": "rating", "schema": r,
                                   "note": "schema-rating niet zichtbaar in tekst (low-confidence)"})

    return {"checked": checked, "mismatch_count": len(mismatches),
            "mismatches": mismatches[:20], "note": "best-effort heuristiek, low-confidence"}


# --------------------------------------------------------------------------- #
# Cat. 3 — technische velden                                                  #
# --------------------------------------------------------------------------- #
def _security_headers(headers):
    out = {}
    for key, header_name in SECURITY_HEADERS.items():
        val = headers.get(header_name, "")
        out[key] = {"present": bool(val), "value": (val or "")[:300]}
    return out


def _redirect_info(resp, soup, html):
    history = list(getattr(resp, "history", []) or [])
    chain = [{"status": h.status_code, "url": h.url} for h in history]
    types = [str(h.status_code) for h in history]

    # meta-refresh redirect op de eindpagina
    meta_refresh = False
    try:
        mr = soup.find("meta", attrs={"http-equiv": re.compile(r"^\s*refresh\s*$", re.I)})
        if mr and "url=" in (mr.get("content", "") or "").lower():
            meta_refresh = True
    except Exception:
        pass
    if meta_refresh:
        types.append("meta-refresh")

    # JS-redirect: alleen flaggen bij een kleine "bounce"-pagina (anti-false-positive)
    try:
        if html and len(html) < 4000 and re.search(
                r"(?:window\.)?location(?:\.href)?\s*=\s*['\"]|location\.replace\s*\(", html):
            types.append("js")
    except Exception:
        pass

    # loop-detectie: herhaalt een URL zich in de keten?
    urls = [h.url for h in history] + [getattr(resp, "url", "")]
    norm = [_norm_url(u) for u in urls if u]
    redirect_loop = len(norm) != len(set(norm))

    return {
        "redirect_chain": chain,
        "redirect_count": len(history),
        "redirect_type": "+".join(dict.fromkeys(types)) if types else "",
        "redirect_loop": redirect_loop,
    }


def _url_structure(url):
    p = urlparse(url)
    path = p.path or "/"
    folders = [seg for seg in path.split("/") if seg]
    return {
        "length": len(url),
        "depth": len(folders),
        "has_params": bool(p.query),
        "has_uppercase": bool(re.search(r"[A-Z]", path + ("?" + p.query if p.query else ""))),
        "has_underscore": "_" in path,
        "has_non_ascii": any(ord(c) > 127 for c in url),
        "has_double_slash": "//" in path,
        "over_115_chars": len(url) > 115,
        "trailing_slash": path != "/" and path.endswith("/"),
    }


def _mixed_content(url, soup):
    """Op een https-pagina: resources die via http:// of protocol-relatief laden."""
    if not url.lower().startswith("https://"):
        return [], 0
    items = []
    resource_attrs = [
        ("img", "src"), ("script", "src"), ("iframe", "src"), ("source", "src"),
        ("video", "src"), ("audio", "src"), ("embed", "src"), ("object", "data"),
        ("img", "data-src"),
    ]
    for tag, attr in resource_attrs:
        for el in soup.find_all(tag):
            val = (el.get(attr) or "").strip()
            if not val:
                continue
            if val.lower().startswith("http://"):
                items.append({"tag": tag, "attr": attr, "url": val[:300], "type": "http-insecure"})
            elif val.startswith("//"):
                items.append({"tag": tag, "attr": attr, "url": val[:300], "type": "protocol-relative"})
    # <link> alleen voor echte resources (stylesheet/preload/icon/manifest)
    res_rel = re.compile(r"stylesheet|preload|prefetch|icon|manifest|preconnect|dns-prefetch", re.I)
    for el in soup.find_all("link", href=True):
        rel = " ".join(el.get("rel", []) or []) if isinstance(el.get("rel"), list) else (el.get("rel") or "")
        if not res_rel.search(rel):
            continue
        val = el.get("href", "").strip()
        if val.lower().startswith("http://"):
            items.append({"tag": "link", "attr": "href", "url": val[:300], "type": "http-insecure"})
        elif val.startswith("//"):
            items.append({"tag": "link", "attr": "href", "url": val[:300], "type": "protocol-relative"})
    return items[:100], len(items)


def _indexability(ctx, soup, robots_blocked):
    """Combineer status/redirect/robots/noindex/canonical tot één conclusie."""
    url = ctx["url"]
    resp = ctx["resp"]
    status = getattr(resp, "status_code", 0)

    # noindex uit meta robots + X-Robots-Tag
    meta_robots = ""
    mr = soup.find("meta", attrs={"name": re.compile(r"^\s*(robots|googlebot)\s*$", re.I)})
    if mr:
        meta_robots = (mr.get("content", "") or "").lower()
    x_robots = str(resp.headers.get("X-Robots-Tag", "")).lower()
    noindex = ("noindex" in meta_robots or "none" in meta_robots
               or "noindex" in x_robots or "none" in x_robots)

    # canonical naar andere URL?
    canon_el = soup.find("link", rel=lambda v: v and "canonical" in (v if isinstance(v, str) else " ".join(v)).lower())
    canon = ""
    if canon_el and canon_el.get("href"):
        canon = urljoin(url, canon_el.get("href"))
    canonicalised = bool(canon and _norm_url(canon) != _norm_url(url))

    # prioriteit van conclusies
    if status == 0 or status >= 400:
        return False, f"error (HTTP {status})"
    if 300 <= status < 400:
        return False, f"redirect (HTTP {status})"
    if robots_blocked:
        return False, "blocked (robots.txt verbiedt Googlebot)"
    if noindex:
        return False, "noindex (meta-robots/X-Robots-Tag)"
    if canonicalised:
        return False, f"canonicalised (canonical -> {canon})"
    return True, "ok"


def _canonical_conflict(ctx, soup):
    url = ctx["url"]
    canon_el = soup.find("link", rel=lambda v: v and "canonical" in (v if isinstance(v, str) else " ".join(v)).lower())
    canon = urljoin(url, canon_el.get("href")) if (canon_el and canon_el.get("href")) else ""

    og = soup.find("meta", attrs={"property": re.compile(r"^\s*og:url\s*$", re.I)})
    og_url = urljoin(url, og.get("content")) if (og and og.get("content")) else ""

    mr = soup.find("meta", attrs={"name": re.compile(r"^\s*robots\s*$", re.I)})
    meta_robots = (mr.get("content", "") or "").lower() if mr else ""
    x_robots = str(ctx["resp"].headers.get("X-Robots-Tag", "")).lower()
    noindex = "noindex" in meta_robots or "noindex" in x_robots

    details = []
    if canon and _norm_url(canon) != _norm_url(url):
        details.append(f"canonical ({canon}) wijkt af van de eigen URL")
    if canon and og_url and _norm_url(canon) != _norm_url(og_url):
        details.append(f"canonical wijkt af van og:url ({og_url})")
    if noindex and canon:
        details.append("noindex + canonical samen (tegenstrijdig signaal voor Google)")
    return {"conflict": bool(details), "details": details,
            "canonical": canon, "og_url": og_url}


def _robots_txt_ai_bots(ctx):
    text, rp = _get_robots(ctx)
    root = (ctx.get("base_url") or "").rstrip("/") + "/"
    out = {"_robots_found": bool(text)}
    for bot in AI_BOTS:
        try:
            allowed = rp.can_fetch(bot, root)
        except Exception:
            allowed = True
        explicit = bool(re.search(r"(?im)^\s*user-agent:\s*" + re.escape(bot) + r"\s*$", text))
        out[bot] = {"allowed": bool(allowed), "blocked": (not allowed),
                    "explicitly_listed": explicit}
    return out


# --------------------------------------------------------------------------- #
# Hoofd-entrypoint                                                            #
# --------------------------------------------------------------------------- #
def extract(ctx) -> dict:
    """Bouw een platte dict van nieuwe technische + structured-data-velden.
    Elk veld in een eigen try/except: extract() crasht nooit."""
    out = {}
    soup = ctx.get("soup")
    html = ctx.get("html") or ""
    resp = ctx.get("resp")
    url = ctx.get("url") or ""

    # JSON-LD één keer parsen en hergebruiken
    try:
        jsonld_docs = _parse_jsonld(soup) if soup is not None else []
        jsonld_nodes = [n for d in jsonld_docs for n in _walk_jsonld(d) if isinstance(n, dict)]
    except Exception as e:
        jsonld_docs, jsonld_nodes = [], []
        out["_jsonld_parse_error"] = str(e)

    # zichtbare tekst één keer
    try:
        visible_text = _visible_text(soup) if soup is not None else ""
    except Exception:
        visible_text = ""

    # ---- TECHNISCH ---------------------------------------------------------
    try:
        out["security_headers"] = _security_headers(resp.headers)
    except Exception as e:
        out["security_headers"] = {"error": str(e)}

    try:
        out.update(_redirect_info(resp, soup, html))
    except Exception as e:
        out["redirect_chain"] = []
        out["redirect_error"] = str(e)

    try:
        out["ttfb_ms"] = round(resp.elapsed.total_seconds() * 1000)
    except Exception as e:
        out["ttfb_ms"] = {"error": str(e)}

    # robots-blocked (voor indexability) — gecachet per host
    try:
        _, rp = _get_robots(ctx)
        robots_blocked = not rp.can_fetch("Googlebot", url)
    except Exception:
        robots_blocked = False

    try:
        indexable, reason = _indexability(ctx, soup, robots_blocked)
        out["indexability"] = indexable
        out["indexability_reason"] = reason
    except Exception as e:
        out["indexability"] = None
        out["indexability_reason"] = f"error: {e}"

    try:
        items, count = _mixed_content(url, soup)
        out["mixed_content"] = items
        out["mixed_content_count"] = count
    except Exception as e:
        out["mixed_content"] = []
        out["mixed_content_count"] = 0
        out["mixed_content_error"] = str(e)

    try:
        out["url_structure"] = _url_structure(url)
    except Exception as e:
        out["url_structure"] = {"error": str(e)}

    try:
        out["canonical_conflict"] = _canonical_conflict(ctx, soup)
    except Exception as e:
        out["canonical_conflict"] = {"error": str(e)}

    try:
        out["server_header"] = resp.headers.get("Server", "")
    except Exception as e:
        out["server_header"] = ""
    try:
        out["powered_by"] = resp.headers.get("X-Powered-By", "")
    except Exception:
        out["powered_by"] = ""
    try:
        out["http_version"] = _http_version(resp)
    except Exception as e:
        out["http_version"] = ""
    try:
        out["cdn_detected"] = _cdn_detected(resp.headers)
    except Exception as e:
        out["cdn_detected"] = ""

    try:
        host = urlparse(ctx.get("base_url") or url).netloc.split(":")[0].lower()
        out["tls_cert"] = _tls_cert(host) if host else {"valid": False, "error": "geen host"}
    except Exception as e:
        out["tls_cert"] = {"valid": False, "error": str(e)}

    try:
        out["robots_txt_ai_bots"] = _robots_txt_ai_bots(ctx)
    except Exception as e:
        out["robots_txt_ai_bots"] = {"error": str(e)}

    # ---- STRUCTURED DATA ---------------------------------------------------
    try:
        out["schema_types_detailed"] = _schema_types_detailed(jsonld_nodes)
    except Exception as e:
        out["schema_types_detailed"] = {"error": str(e)}

    try:
        validation = _schema_validation(jsonld_nodes)
        out["schema_validation"] = validation
    except Exception as e:
        validation = []
        out["schema_validation"] = [{"error": str(e)}]

    try:
        out["rich_result_eligible"] = _rich_result_eligible(validation, jsonld_nodes)
    except Exception as e:
        out["rich_result_eligible"] = [{"error": str(e)}]

    try:
        out["fabricated_aggregaterating"] = _fabricated_aggregaterating(
            jsonld_nodes, soup, html, visible_text)
    except Exception as e:
        out["fabricated_aggregaterating"] = {"flag": False, "error": str(e)}

    try:
        out["rdfa_present"] = _rdfa_present(soup)
    except Exception as e:
        out["rdfa_present"] = False
    try:
        out["microdata_types"] = _microdata_types(soup)
    except Exception as e:
        out["microdata_types"] = []

    try:
        out["schema_drift"] = _schema_drift(jsonld_nodes, visible_text)
    except Exception as e:
        out["schema_drift"] = {"checked": False, "error": str(e)}

    return out


# --------------------------------------------------------------------------- #
# Standalone test                                                            #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import requests
    from bs4 import BeautifulSoup

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    HEADERS = {"User-Agent": UA, "Accept-Language": "nl,en;q=0.8",
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

    TEST_URLS = ["https://zekermobiel.nl/", "https://rideparts.nl/", "https://www.coolblue.nl/"]

    def build_ctx(url, session):
        resp = session.get(url, timeout=25, allow_redirects=True)
        html = resp.text
        soup = BeautifulSoup(html, "lxml")
        p = urlparse(resp.url)
        base_url = f"{p.scheme}://{p.netloc}"
        return {"url": resp.url, "html": html, "soup": soup, "resp": resp,
                "base_url": base_url, "session": session, "rendered": False}

    results = {}
    session = requests.Session()
    session.headers.update(HEADERS)
    for u in TEST_URLS:
        try:
            ctx = build_ctx(u, session)
            results[u] = extract(ctx)
        except Exception as e:
            results[u] = {"_fetch_error": str(e)}

    print(json.dumps(results, indent=2, ensure_ascii=False))
