# -*- coding: utf-8 -*-
"""
MODULE 1.8 — SEMRUSH-PARITEIT (order 03-07): de vier Semrush Site-Audit-checks
die de operator miste in onze eigen scraper:

  1. SITEMAP-GEZONDHEID  — elke sitemap-URL gecheckt op status (4xx/5xx),
                           redirect (3xx + doel) en canonical-mismatch
                           (sitemap-URL != canonical van de gecrawlde pagina).
                           Semrush: "incorrect pages found in sitemap.xml".
  2. UNMINIFIED JS/CSS   — first-party scripts/stylesheets opgehaald en met
                           een heuristiek (gem. regellengte + whitespace-ratio,
                           .min./hash-bestandsnamen tellen als geminificeerd)
                           geclassificeerd. Semrush: "unminified JS/CSS files".
  3. INVALID STRUCTURED DATA — aggregeert de bestaande per-pagina
                           schema_validation (technical_schema-extractor):
                           welke types invalid zijn + welke property mist,
                           met de pagina's erbij.
  4. H1 & WORD COUNT     — site-brede fixlijsten: pagina's met 0 of >1 H1 en
                           pagina's onder de woordgrens (default 200, Semrush-
                           drempel), mét het aantal.

Netwerk: JA (sitemap-HEAD/GETs + asset-downloads), maar begrensd
(SITEMAP_CAP url's, ASSET_CAP bestanden, 300KB per asset) en volledig
fail-soft. Schrijft daarnaast ctx["out"]/semrush-fixlijst.json met alle
concrete items zodat fixlijsten/rapporten erop kunnen bouwen.
"""
import json
import re
from collections import defaultdict
from urllib.parse import urljoin, urlparse

KEY = "semrush"
LABEL = "Semrush-pariteit (sitemap / minificatie / schema / H1 & tekst)"
ORDER = 15

SITEMAP_CAP = 150      # max sitemap-URL's om te checken
PAGE_HARVEST_CAP = 6   # pagina's waarvan we assets harvesten
ASSET_CAP = 30         # max JS/CSS-bestanden om te downloaden
ASSET_READ_MAX = 300_000
WORD_MIN = 200         # Semrush low-word-count-drempel
UA = "Mozilla/5.0 (compatible; seo-scraper-v2-semrush-audit)"


def _norm(u):
    """URL-normalisatie voor vergelijking: schema/trailing-slash/fragment weg."""
    try:
        p = urlparse((u or "").strip())
        path = p.path or "/"
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        return f"{p.netloc.lower()}{path}" + (f"?{p.query}" if p.query else "")
    except Exception:
        return u or ""


def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    return s


# --------------------------------------------------------------------------- #
# 1. Sitemap-gezondheid
# --------------------------------------------------------------------------- #
def _sitemap_health(ctx, sess):
    urls = list(ctx.get("sitemap_urls") or [])[:SITEMAP_CAP]
    canon_by_url = {_norm(p.get("url")): (p.get("canonical") or "")
                    for p in ctx.get("pages") or []}
    bad, redirects, canon_mismatch = [], [], []
    checked = 0
    for u in urls:
        try:
            r = sess.head(u, timeout=10, allow_redirects=False)
            if r.status_code in (405, 403, 501):  # HEAD niet toegestaan -> GET
                r = sess.get(u, timeout=12, allow_redirects=False, stream=True)
                r.close()
            checked += 1
            st = r.status_code
            if 300 <= st < 400:
                redirects.append({"url": u, "status": st,
                                  "naar": r.headers.get("Location", "")[:300]})
            elif st >= 400 or st == 0:
                bad.append({"url": u, "status": st})
            else:
                canon = canon_by_url.get(_norm(u))
                if canon and _norm(canon) != _norm(u):
                    canon_mismatch.append({"url": u, "canonical": canon})
        except Exception as e:
            bad.append({"url": u, "status": 0, "error": str(e)[:120]})
    return {"total_in_sitemap": len(ctx.get("sitemap_urls") or []),
            "checked": checked, "broken": bad, "redirects": redirects,
            "canonical_mismatch": canon_mismatch}


# --------------------------------------------------------------------------- #
# 2. Minificatie JS/CSS
# --------------------------------------------------------------------------- #
_MIN_NAME = re.compile(r"(\.min\.(js|css))([?#]|$)|(-|\.)[0-9a-f]{8,}\.(js|css)", re.I)


def _harvest_assets(ctx, sess):
    """Haal van een handvol gecrawlde pagina's de HTML opnieuw op en verzamel
    unieke script[src]- en stylesheet-URL's (record bevat die niet)."""
    from bs4 import BeautifulSoup
    pages = [p for p in (ctx.get("pages") or []) if p.get("status") == 200]
    assets, seen = [], set()
    for p in pages[:PAGE_HARVEST_CAP]:
        try:
            r = sess.get(p["url"], timeout=15)
            if not r.ok:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            found = [(s.get("src"), "js") for s in soup.find_all("script", src=True)]
            for l in soup.find_all("link", href=True):
                rel = l.get("rel") or []
                rel = " ".join(rel) if isinstance(rel, list) else str(rel)
                if "stylesheet" in rel.lower():
                    found.append((l.get("href"), "css"))
            for src, kind in found:
                full = urljoin(p["url"], (src or "").strip())
                if not full.startswith("http"):
                    continue
                k = full.split("#")[0]
                if k not in seen:
                    seen.add(k)
                    assets.append({"url": k, "type": kind, "page": p["url"]})
        except Exception:
            continue
    return assets


def _looks_minified(text, url):
    if _MIN_NAME.search(urlparse(url).path + ("?" + urlparse(url).query if urlparse(url).query else "")):
        return True, "bestandsnaam (.min./hash)"
    if len(text) < 2048:
        return True, "klein bestand (<2KB, verwaarloosbaar)"
    lines = text.splitlines() or [text]
    avg_line = len(text) / max(1, len(lines))
    ws = sum(text.count(c) for c in (" ", "\t", "\n")) / max(1, len(text))
    if avg_line >= 400:
        return True, "gem. regellengte >= 400"
    if avg_line >= 160 and ws < 0.14:
        return True, "compacte code"
    return False, f"gem. regellengte {avg_line:.0f}, whitespace {ws:.0%}"


def _minification(ctx, sess, domain):
    assets = _harvest_assets(ctx, sess)[:ASSET_CAP]
    unmin, ok_count, third_party = [], 0, 0
    for a in assets:
        host = urlparse(a["url"]).netloc.lower()
        first_party = domain.lower() in host or host.endswith(".vercel.app")
        try:
            r = sess.get(a["url"], timeout=15, stream=True)
            raw = r.raw.read(ASSET_READ_MAX, decode_content=True)
            r.close()
            text = raw.decode("utf-8", "ignore")
        except Exception:
            continue
        mini, why = _looks_minified(text, a["url"])
        if mini:
            ok_count += 1
        else:
            if not first_party:
                third_party += 1
            unmin.append({"url": a["url"], "type": a["type"],
                          "size_kb": round(len(text) / 1024, 1),
                          "reden": why, "first_party": first_party,
                          "voorbeeld_pagina": a["page"]})
    return {"assets_checked": len(assets), "minified": ok_count,
            "unminified": unmin, "third_party_unminified": third_party}


# --------------------------------------------------------------------------- #
# 3. Structured-data-aggregatie (bestaande per-pagina validatie)
# --------------------------------------------------------------------------- #
def _schema_agg(ctx):
    agg = defaultdict(lambda: {"pages": [], "missing": set()})
    for p in ctx.get("pages") or []:
        for v in p.get("schema_validation") or []:
            if not isinstance(v, dict) or v.get("status") != "invalid":
                continue
            key = v.get("type", "?")
            agg[key]["pages"].append(p["url"])
            agg[key]["missing"].update(v.get("missing_required") or [])
    return [{"type": t, "missing_required": sorted(d["missing"]),
             "page_count": len(set(d["pages"])),
             "pages": sorted(set(d["pages"]))[:25]}
            for t, d in sorted(agg.items())]


# --------------------------------------------------------------------------- #
# 4. H1 & word count
# --------------------------------------------------------------------------- #
def _h1_wordcount(ctx):
    no_h1, multi_h1, low_wc = [], [], []
    for p in ctx.get("pages") or []:
        if p.get("status") != 200:
            continue
        c = int(p.get("h1_count") or 0)
        if c == 0:
            no_h1.append(p["url"])
        elif c > 1:
            multi_h1.append({"url": p["url"], "h1_count": c})
        wc = int(p.get("word_count") or 0)
        if wc < WORD_MIN:
            low_wc.append({"url": p["url"], "word_count": wc})
    low_wc.sort(key=lambda x: x["word_count"])
    return {"no_h1": no_h1, "multi_h1": multi_h1,
            "low_word_count": low_wc, "word_min": WORD_MIN}


# --------------------------------------------------------------------------- #
def audit(ctx):
    log = ctx.get("log")
    domain = ctx.get("domain") or ""
    try:
        sess = _session()
    except Exception as e:
        return {"score": None, "summary": f"requests niet beschikbaar: {e}", "issues": []}

    res = {}
    for name, fn in (("sitemap", lambda: _sitemap_health(ctx, sess)),
                     ("minify", lambda: _minification(ctx, sess, domain)),
                     ("schema", lambda: _schema_agg(ctx)),
                     ("content", lambda: _h1_wordcount(ctx))):
        try:
            res[name] = fn()
        except Exception as e:
            if log:
                log.warning("semrush-subcheck '%s' faalde: %s", name, e)
            res[name] = {"error": str(e)[:200]}

    sm = res.get("sitemap") or {}
    mn = res.get("minify") or {}
    sc = res.get("schema") if isinstance(res.get("schema"), list) else []
    ct = res.get("content") or {}

    issues = []
    def issue(sev, title, fix, count):
        if count:
            issues.append({"severity": sev, "title": f"{title} ({count})", "fix": fix})

    issue("Critical", "Kapotte URL's (4xx/5xx) in sitemap.xml",
          "Verwijder deze URL's uit de sitemap of herstel de pagina's.",
          len(sm.get("broken") or []))
    issue("High", "Redirectende URL's in sitemap.xml",
          "Zet het redirect-DOEL in de sitemap, nooit de oude URL.",
          len(sm.get("redirects") or []))
    issue("High", "Sitemap-URL wijkt af van canonical",
          "Sitemap moet exact de canonical-URL bevatten.",
          len(sm.get("canonical_mismatch") or []))
    issue("Medium", "Unminified first-party JS/CSS",
          "Minify in de build-stap (esbuild/terser/cssnano) of serveer .min-varianten.",
          sum(1 for u in (mn.get("unminified") or []) if u.get("first_party")))
    issue("High", "Invalid structured data (verplichte property mist)",
          "Vul de ontbrekende verplichte schema-properties aan.",
          sum(x.get("page_count", 0) for x in sc))
    issue("Critical", "Pagina's zonder H1",
          "Voeg precies 1 H1 met het hoofdonderwerp toe.", len(ct.get("no_h1") or []))
    issue("High", "Pagina's met meerdere H1's",
          "Houd 1 H1 aan; degradeer de rest naar H2.", len(ct.get("multi_h1") or []))
    issue("Medium", f"Pagina's onder {WORD_MIN} woorden",
          "Verdiep de content (specs, FAQ, uitleg) of noindex/merge dunne pagina's.",
          len(ct.get("low_word_count") or []))

    # score: 100 - gewogen aftrek, begrensd
    npages = max(1, len(ctx.get("pages") or []))
    penalty = (len(sm.get("broken") or []) * 4 + len(sm.get("redirects") or []) * 2
               + len(sm.get("canonical_mismatch") or []) * 2
               + sum(1 for u in (mn.get("unminified") or []) if u.get("first_party")) * 3
               + min(20, sum(x.get("page_count", 0) for x in sc))
               + len(ct.get("no_h1") or []) * 100 / npages * 0.3
               + len(ct.get("multi_h1") or []) * 100 / npages * 0.15
               + len(ct.get("low_word_count") or []) * 100 / npages * 0.2)
    score = max(0, round(100 - penalty))

    summary = (f"Sitemap: {sm.get('checked', 0)} gecheckt, {len(sm.get('broken') or [])} kapot, "
               f"{len(sm.get('redirects') or [])} redirect, {len(sm.get('canonical_mismatch') or [])} canonical-mismatch. "
               f"Assets: {mn.get('assets_checked', 0)} gecheckt, "
               f"{len(mn.get('unminified') or [])} unminified. "
               f"Schema: {len(sc)} invalid type(s). "
               f"H1: {len(ct.get('no_h1') or [])} zonder, {len(ct.get('multi_h1') or [])} meerdere. "
               f"Dunne content (<{WORD_MIN}w): {len(ct.get('low_word_count') or [])}.")

    # machine-leesbare fixlijst-dump voor rapport-/fixlijst-generatie
    try:
        out = ctx.get("out")
        if out:
            (out / "semrush-fixlijst.json").write_text(
                json.dumps({"domain": domain, "sitemap": sm, "minify": mn,
                            "schema_invalid": sc, "content": ct},
                           ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        if log:
            log.warning("semrush-fixlijst.json schrijven faalde: %s", e)

    return {"score": score, "summary": summary, "issues": issues,
            "sitemap": {k: (v if not isinstance(v, list) else v[:25]) for k, v in sm.items()},
            "minify": mn, "schema_invalid": sc,
            "content_counts": {"no_h1": len(ct.get("no_h1") or []),
                               "multi_h1": len(ct.get("multi_h1") or []),
                               "low_word_count": len(ct.get("low_word_count") or [])}}
