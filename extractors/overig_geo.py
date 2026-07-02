# -*- coding: utf-8 -*-
"""
Extractor-module: OVERIG + GEO (categorie 9 + AI-search-laag).
Contract: extract(ctx) -> dict   (platte dict van NIEUWE velden, faalt zacht, crasht nooit)

ctx keys: url, html, soup (BeautifulSoup/lxml), resp (requests.Response of None),
          base_url (scheme://host), session (requests.Session), rendered (bool).

Velden:
  pagination, amphtml, pwa_manifest, rss_feeds, social_profiles, nap,
  well_known, page_404_quality, citability_signals, brand_mention_signals
"""
import json
import re
from urllib.parse import urljoin, urlparse

# site-niveau caches (per host: well-known + 404-probe maar 1x)
_WELLKNOWN_CACHE = {}
_404_CACHE = {}

_SOCIAL = {
    "facebook": re.compile(r"facebook\.com", re.I),
    "instagram": re.compile(r"instagram\.com", re.I),
    "x_twitter": re.compile(r"(?:twitter|x)\.com", re.I),
    "linkedin": re.compile(r"linkedin\.com", re.I),
    "youtube": re.compile(r"youtube\.com|youtu\.be", re.I),
    "tiktok": re.compile(r"tiktok\.com", re.I),
    "pinterest": re.compile(r"pinterest\.", re.I),
    "whatsapp": re.compile(r"wa\.me|whatsapp\.com", re.I),
}


def _iter_jsonld(soup):
    for s in soup.find_all("script", type=lambda v: v and "ld+json" in v):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)


def _types(node):
    t = node.get("@type")
    return [str(x).lower() for x in (t if isinstance(t, list) else [t]) if x]


def _pagination(soup, resp):
    out = {"rel_prev": None, "rel_next": None}
    try:
        for l in soup.find_all("link", rel=True):
            rels = [r.lower() for r in (l.get("rel") or [])]
            if "prev" in rels or "previous" in rels:
                out["rel_prev"] = l.get("href")
            if "next" in rels:
                out["rel_next"] = l.get("href")
        if resp is not None:
            link_hdr = resp.headers.get("Link", "")
            for m in re.finditer(r'<([^>]+)>\s*;\s*rel="?(\w+)"?', link_hdr):
                if m.group(2).lower() == "prev" and not out["rel_prev"]:
                    out["rel_prev"] = m.group(1)
                if m.group(2).lower() == "next" and not out["rel_next"]:
                    out["rel_next"] = m.group(1)
    except Exception:
        pass
    return out


def _manifest(soup, base_url, url, session):
    res = {"present": False}
    try:
        link = soup.find("link", rel=lambda v: v and "manifest" in (v if isinstance(v, str) else " ".join(v)).lower())
        if not link or not link.get("href"):
            return res
        res["present"] = True
        href = urljoin(url, link["href"])
        res["url"] = href
        try:
            r = session.get(href, timeout=10)
            if r.ok:
                m = r.json()
                res.update({
                    "name": m.get("name"),
                    "short_name": m.get("short_name"),
                    "theme_color": m.get("theme_color"),
                    "background_color": m.get("background_color"),
                    "display": m.get("display"),
                    "start_url": m.get("start_url"),
                    "icons_count": len(m.get("icons", []) or []),
                })
        except Exception as e:
            res["fetch_error"] = str(e)[:120]
    except Exception:
        pass
    return res


def _rss(soup, url):
    feeds = []
    try:
        for l in soup.find_all("link", type=re.compile(r"application/(rss|atom)\+xml", re.I)):
            if l.get("href"):
                feeds.append(urljoin(url, l["href"]))
    except Exception:
        pass
    return feeds


def _social_and_nap(soup, url):
    socials, phones, emails = {}, set(), set()
    address = None
    # social uit sameAs (JSON-LD)
    try:
        for node in _iter_jsonld(soup):
            same = node.get("sameAs")
            if same:
                for s in (same if isinstance(same, list) else [same]):
                    for plat, rx in _SOCIAL.items():
                        if rx.search(str(s)):
                            socials.setdefault(plat, str(s))
            if any(t in ("organization", "localbusiness") or t.endswith("business") for t in _types(node)):
                tel = node.get("telephone")
                if tel:
                    phones.add(str(tel))
                addr = node.get("address")
                if isinstance(addr, dict):
                    address = ", ".join(str(addr.get(k, "")) for k in
                                        ("streetAddress", "postalCode", "addressLocality") if addr.get(k)) or address
                elif isinstance(addr, str):
                    address = addr
    except Exception:
        pass
    # social + contact uit links
    try:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("tel:"):
                phones.add(href[4:].strip())
            elif href.startswith("mailto:"):
                emails.add(href[7:].split("?")[0].strip())
            else:
                for plat, rx in _SOCIAL.items():
                    if plat not in socials and rx.search(href) and "/sharer" not in href and "share?" not in href:
                        socials.setdefault(plat, urljoin(url, href))
    except Exception:
        pass
    social_list = [{"platform": p, "url": u} for p, u in socials.items()]
    nap = {"phones": sorted(phones)[:5], "emails": sorted(emails)[:5],
           "address": address, "has_contact": bool(phones or emails)}
    return social_list, nap


def _well_known(base_url, session):
    host = urlparse(base_url).netloc.lower()
    if host in _WELLKNOWN_CACHE:
        return _WELLKNOWN_CACHE[host]
    paths = {"llms_txt": "/llms.txt", "security_txt": "/.well-known/security.txt",
             "humans_txt": "/humans.txt", "ads_txt": "/ads.txt"}
    res = {}
    for key, path in paths.items():
        try:
            r = session.get(urljoin(base_url, path), timeout=8, allow_redirects=True)
            ctype = r.headers.get("Content-Type", "").lower()
            # echte tekst-resource (geen SPA-catch-all-HTML)
            res[key] = bool(r.status_code == 200 and len(r.content) > 5 and "text/html" not in ctype)
        except Exception:
            res[key] = False
    _WELLKNOWN_CACHE[host] = res
    return res


def _probe_404(base_url, session):
    host = urlparse(base_url).netloc.lower()
    if host in _404_CACHE:
        return _404_CACHE[host]
    res = {"is_proper_404": None, "soft_404": None}
    try:
        probe = urljoin(base_url, "/seo-probe-nonexistent-9z8x7c6v5b/")
        r = session.get(probe, timeout=10, allow_redirects=True)
        res["status_on_missing"] = r.status_code
        res["is_proper_404"] = (r.status_code == 404)
        # soft-404: 200 OK op een onbestaande pagina = fout
        res["soft_404"] = (r.status_code == 200)
    except Exception as e:
        res["error"] = str(e)[:120]
    _404_CACHE[host] = res
    return res


def _citability(soup):
    res = {}
    try:
        types = set()
        for node in _iter_jsonld(soup):
            types.update(_types(node))
        res["has_faq_schema"] = "faqpage" in types
        res["has_howto_schema"] = "howto" in types
        res["has_qapage_schema"] = "qapage" in types
        res["list_count"] = len(soup.find_all(["ul", "ol"]))
        res["table_count"] = len(soup.find_all("table"))
        res["has_definition_list"] = bool(soup.find("dl"))
        # vraag-achtige headings (goed voor AI-citatie / featured snippets)
        q = 0
        for h in soup.find_all(re.compile(r"^h[2-4]$")):
            txt = h.get_text(" ", strip=True).lower()
            if txt.endswith("?") or re.match(r"^(wat|hoe|waarom|wanneer|welke|wie|waar|kan|mag|moet|is|zijn|how|what|why|when|which|who|where)\b", txt):
                q += 1
        res["question_headings"] = q
        res["citable"] = bool(res["has_faq_schema"] or res["has_howto_schema"] or q >= 2 or res["list_count"] >= 3)
    except Exception:
        pass
    return res


def _brand_signals(soup):
    res = {"has_organization_schema": False, "has_sameas": False, "has_logo": False}
    try:
        for node in _iter_jsonld(soup):
            ts = _types(node)
            if any(t in ("organization", "localbusiness") or t.endswith("business") for t in ts):
                res["has_organization_schema"] = True
                if node.get("sameAs"):
                    res["has_sameas"] = True
                if node.get("logo"):
                    res["has_logo"] = True
        res["knowledge_graph_ready"] = res["has_organization_schema"] and res["has_sameas"] and res["has_logo"]
    except Exception:
        pass
    return res


def extract(ctx):
    soup = ctx.get("soup")
    url = ctx.get("url", "")
    base_url = ctx.get("base_url") or ""
    session = ctx.get("session")
    resp = ctx.get("resp")
    out = {}

    def safe(key, fn, *a):
        try:
            out[key] = fn(*a)
        except Exception as e:
            out[key] = {"error": str(e)[:120]}

    if soup is None:
        return {"overig_geo_error": "no soup in ctx"}

    safe("pagination", _pagination, soup, resp)
    try:
        amp = soup.find("link", rel="amphtml")
        out["amphtml"] = urljoin(url, amp["href"]) if amp and amp.get("href") else None
    except Exception:
        out["amphtml"] = None
    safe("pwa_manifest", _manifest, soup, base_url, url, session)
    out["rss_feeds"] = _rss(soup, url)
    social, nap = _social_and_nap(soup, url)
    out["social_profiles"] = social
    out["nap"] = nap
    if session is not None and base_url:
        safe("well_known", _well_known, base_url, session)
        safe("page_404_quality", _probe_404, base_url, session)
    safe("citability_signals", _citability, soup)
    safe("brand_mention_signals", _brand_signals, soup)
    return out


if __name__ == "__main__":
    import sys
    import requests
    from bs4 import BeautifulSoup
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
    for test_url in ["https://zekermobiel.nl/", "https://www.coolblue.nl/"]:
        try:
            s = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept-Language": "nl,en;q=0.8"})
            r = s.get(test_url, timeout=15, allow_redirects=True)
            p = urlparse(r.url)
            ctx = {"url": r.url, "html": r.text, "soup": BeautifulSoup(r.text, "lxml"),
                   "resp": r, "base_url": f"{p.scheme}://{p.netloc}", "session": s, "rendered": False}
            print(f"\n===== {test_url} =====")
            print(json.dumps(extract(ctx), indent=2, ensure_ascii=False)[:3000])
        except Exception as e:
            print(f"{test_url}: FOUT {e}", file=sys.stderr)
