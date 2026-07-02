#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extractor-module: LINKS (categorie 4) + AFBEELDINGEN (categorie 5).

Standalone plug-in voor de SEO-scraper (`seo_scraper_v2.py`). Een finalizer
wired dit later in: in `SiteScraper._analyse()` wordt per pagina een `ctx`
opgebouwd, `extract(ctx)` aangeroepen en de geretourneerde (platte) dict in de
page-record gemerged.

CONTRACT
--------
    def extract(ctx) -> dict

ctx-keys die de hoofd-scraper aanlevert:
    ctx['url']      -> str   eind-URL van de pagina (na redirects)
    ctx['html']     -> str   rauwe HTML
    ctx['soup']     -> bs4.BeautifulSoup  (lxml-parser)
    ctx['resp']     -> requests.Response  (de oorspronkelijke GET)
    ctx['base_url'] -> str   "scheme://host"
    ctx['session']  -> requests.Session   (hergebruik voor HEAD/GET-checks)
    ctx['rendered'] -> bool  (kwam de HTML via Playwright)
  optioneel (finalizer/CLI mag tunen; defaults hieronder):
    ctx['max_link_checks']  -> int  (default 40)  netwerk-sample voor broken/redirect links
    ctx['max_image_checks'] -> int  (default 30)  netwerk-sample voor broken/size images
    ctx['check_delay']      -> float(default 0.25) kleine pauze tussen netwerk-calls

extract() geeft een PLATTE dict van NIEUWE velden terug. Elk veld wordt in zijn
eigen try/except berekend, dus de functie kan nooit crashen. Het netwerk-budget
is hard begrensd via dedup + sample + een per-URL cache + timeout=10.

NIEUWE VELDEN
-------------
LINKS:
    external_links[]      {url, anchor, rel}     uitgaande links naar ANDERE domeinen
    link_rel_summary      {nofollow, sponsored, ugc, noopener, noreferrer}
    unsafe_cross_origin[] {url, anchor, rel, external}  target=_blank zonder rel=noopener
    anchor_quality        {non_descriptive_count, examples[]}
    broken_links[]        {url, status[, error]}  4xx/5xx (of onbereikbaar) op de sample
    redirect_links[]      {url, status, location} 3xx op de sample
    outlink_count         int  (intern+extern, http(s), excl. in-page #anchors)
    links_checked         int  (hoeveel links daadwerkelijk netwerk-gecheckt)
AFBEELDINGEN:
    image_details[]       per UNIEKE src (sample): {src, img_format, is_next_gen,
                          heeft_dimensies, srcset_present, sizes_present, img_title,
                          loading, alt, img_filesize_kb, content_type, http_status, broken}
    background_images[]   CSS-achtergrond-URLs uit inline style + <style>-blokken + lazy data-bg
    broken_images[]       {url, status}  4xx/5xx op de image-sample
    images_summary        {total, missing_alt, missing_dimensions, non_next_gen_count,
                          oversized_count, broken_count, lazy_count}
    images_checked        int
    links_images_meta     {network_calls, max_link_checks, max_image_checks, rendered}

NIET gedupliceerd (levert de hoofd-scraper al): internal_links, external_links_count,
images[]{src,alt,width,height,loading}, image_count, images_missing_alt, site-brede
interne anchor-frequenties.

Alleen stdlib + bs4 + requests. De extract()-logica zelf gebruikt enkel stdlib
(bs4-soup en requests-session komen via ctx binnen); de __main__-demo onderaan
importeert requests + bs4 om twee echte pagina's te testen.
"""

import json
import re
import time
from itertools import zip_longest
from urllib.parse import urljoin, urlparse, urldefrag


# --- generieke / niet-beschrijvende ankerteksten (NL + EN), genormaliseerd ---
GENERIC_ANCHORS = {
    # NL
    "klik hier", "klik", "hier", "klik dan hier", "druk hier", "klik op",
    "lees meer", "meer", "lees verder", "verder", "meer lezen", "verder lezen",
    "meer info", "meer informatie", "info", "informatie", "lees", "meer details",
    "bekijk", "bekijk hier", "bekijken", "details", "ga naar", "ga verder",
    "open", "link", "deze link", "via deze link", "website", "klik voor meer",
    "download", "volgende", "vorige", "terug", "start", "hier klikken",
    # EN
    "read more", "click here", "click", "here", "more", "learn more",
    "this", "this page", "this link", "go", "view", "view more", "see more",
    "see all", "continue", "continue reading", "find out more", "next",
    "previous", "back", "go here", "full story", "get started",
}

# next-gen vs. klassieke rasterformaten
NEXT_GEN = ("webp", "avif", "jxl")

# url(...) binnen CSS, en specifiek binnen een background-declaratie
_URL_IN_CSS = re.compile(r"""url\(\s*['"]?([^'")]+?)['"]?\s*\)""", re.I)
_BG_URL = re.compile(r"""background[^;{}]*?url\(\s*['"]?([^'")]+?)['"]?\s*\)""", re.I)
_EXT = re.compile(r"\.([a-z0-9]{2,5})$", re.I)
_IMG_EXT_IN_VAL = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg|bmp)(\?|#|$)", re.I)


# ------------------------------- helpers -------------------------------------

def _interleave(a, b):
    """Wissel twee lijsten af zodat een sample beide soorten dekt."""
    out = []
    for x, y in zip_longest(a, b):
        if x is not None:
            out.append(x)
        if y is not None:
            out.append(y)
    return out


def _rel_list(a):
    """rel-attribuut als lijst lowercase tokens (bs4 geeft list, soms str)."""
    rel = a.get("rel")
    if rel is None:
        return []
    if isinstance(rel, (list, tuple)):
        return [str(r).lower() for r in rel]
    return [t.lower() for t in str(rel).split()]


def _same_site(host, domain):
    """True als host bij hetzelfde domein hoort (www. genegeerd)."""
    try:
        h = (host or "").lower()
        d = (domain or "").lower()
        if not h:           # relatieve link is na urljoin al absoluut -> zelfde site
            return True
        if h.startswith("www."):
            h = h[4:]
        if d.startswith("www."):
            d = d[4:]
        return h == d
    except Exception:
        return False


def _ext_format(u):
    """Bestandsformaat uit de extensie van een (afbeeldings-)URL; '' indien onbekend."""
    try:
        path = urlparse(u).path
    except Exception:
        path = u or ""
    m = _EXT.search(path or "")
    if not m:
        return ""
    ext = m.group(1).lower()
    return "jpg" if ext == "jpeg" else ext


def _has_srcset(img):
    """img heeft srcset, of zit in een <picture> met een <source srcset>."""
    try:
        if (img.get("srcset") or "").strip():
            return True
        p = img.parent
        if p is not None and getattr(p, "name", "") == "picture":
            for s in p.find_all("source"):
                if (s.get("srcset") or "").strip():
                    return True
    except Exception:
        pass
    return False


# ------------------------------- extractor -----------------------------------

def extract(ctx) -> dict:
    out = {}
    try:
        url = ctx.get("url") or ""
        soup = ctx.get("soup")
        session = ctx.get("session")
    except Exception:
        return out
    if soup is None:
        return out

    try:
        domain = urlparse(url).netloc.lower()
    except Exception:
        domain = ""

    try:
        max_link_checks = max(0, int(ctx.get("max_link_checks", 40) or 0))
    except Exception:
        max_link_checks = 40
    try:
        max_image_checks = max(0, int(ctx.get("max_image_checks", 30) or 0))
    except Exception:
        max_image_checks = 30
    try:
        delay = max(0.0, float(ctx.get("check_delay", 0.25) or 0))
    except Exception:
        delay = 0.25
    timeout = 10

    # -------- gedeelde HTTP-check met cache + klein netwerk-budget ------------
    # site-brede cache (via ctx['head_cache']) -> HEAD/GET-checks worden over
    # pagina's heen hergebruikt i.p.v. per pagina opnieuw; val terug op lokaal.
    http_cache = ctx.get("head_cache")
    if not isinstance(http_cache, dict):
        http_cache = {}
    net_stats = {"calls": 0}

    def http_check(u, follow=False):
        """HEAD (val terug op GET) op u. Gecachet per (url, follow). Nooit crashen."""
        key = (u, follow)
        if key in http_cache:
            return http_cache[key]
        res = {"status": 0, "location": "", "content_type": "",
               "content_length": None, "error": ""}
        if not session or not u or u.startswith("data:"):
            res["error"] = "skipped"
            http_cache[key] = res
            return res
        r = None
        try:
            try:
                r = session.head(u, timeout=timeout, allow_redirects=follow)
            except Exception:
                r = None
            # sommige servers weigeren HEAD -> val terug op een lichte GET
            if r is None or r.status_code in (403, 405, 406, 409, 429, 501):
                try:
                    r = session.get(u, timeout=timeout, allow_redirects=follow, stream=True)
                except Exception:
                    if r is None:
                        raise
            res["status"] = r.status_code
            res["location"] = r.headers.get("Location", "") or ""
            res["content_type"] = r.headers.get("Content-Type", "") or ""
            cl = r.headers.get("Content-Length")
            if cl and str(cl).strip().isdigit():
                res["content_length"] = int(str(cl).strip())
        except Exception as e:
            res["error"] = str(e)[:200]
        finally:
            try:
                if r is not None:
                    r.close()
            except Exception:
                pass
        http_cache[key] = res
        net_stats["calls"] += 1
        if delay:
            time.sleep(delay)
        return res

    # -------- alle <a href> verzamelen (1x) ----------------------------------
    links = []
    try:
        for a in soup.find_all("a", href=True):
            raw = (a.get("href") or "").strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                full = urldefrag(urljoin(url, raw))[0]
            except Exception:
                continue
            pu = urlparse(full)
            if pu.scheme not in ("http", "https"):
                continue
            anchor = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            img_child = a.find("img")
            links.append({
                "full": full,
                "anchor": anchor,
                "rel": _rel_list(a),
                "target": (a.get("target") or "").strip().lower(),
                "has_img": img_child is not None,
                "img_alt": (img_child.get("alt") or "").strip() if img_child is not None else "",
                "internal": _same_site(pu.netloc, domain),
            })
    except Exception:
        links = []

    # outlink_count
    try:
        out["outlink_count"] = len(links)
    except Exception:
        out["outlink_count"] = 0

    # external_links[]  (gededupliceerd per URL)
    try:
        ext_map = {}
        for l in links:
            if l["internal"]:
                continue
            u = l["full"]
            if u not in ext_map:
                ext_map[u] = {"url": u, "anchor": l["anchor"][:140], "rel": list(l["rel"])}
            else:
                e = ext_map[u]
                if not e["anchor"] and l["anchor"]:
                    e["anchor"] = l["anchor"][:140]
                for r in l["rel"]:
                    if r not in e["rel"]:
                        e["rel"].append(r)
        out["external_links"] = list(ext_map.values())[:200]
    except Exception:
        out["external_links"] = []

    # link_rel_summary
    try:
        summ = {"nofollow": 0, "sponsored": 0, "ugc": 0, "noopener": 0, "noreferrer": 0}
        for l in links:
            for r in l["rel"]:
                if r in summ:
                    summ[r] += 1
        out["link_rel_summary"] = summ
    except Exception:
        out["link_rel_summary"] = {}

    # unsafe_cross_origin[]  (target=_blank zonder rel=noopener)
    try:
        seen = set()
        unsafe = []
        for l in links:
            if l["target"] == "_blank" and "noopener" not in l["rel"]:
                if l["full"] in seen:
                    continue
                seen.add(l["full"])
                unsafe.append({"url": l["full"], "anchor": l["anchor"][:140],
                               "rel": l["rel"], "external": not l["internal"]})
        out["unsafe_cross_origin"] = unsafe[:100]
    except Exception:
        out["unsafe_cross_origin"] = []

    # anchor_quality  (lege / image-only-zonder-alt / generieke ankers)
    try:
        non_desc = 0
        examples = []
        seen_ex = set()
        for l in links:
            text = l["anchor"]
            if not text:
                # leeg label: image-only mét alt is wél beschrijvend -> niet flaggen
                if l["has_img"] and l["img_alt"]:
                    continue
                label = "(afbeelding zonder alt)" if l["has_img"] else "(lege link)"
                non_desc += 1
                if l["full"] not in seen_ex and len(examples) < 15:
                    seen_ex.add(l["full"])
                    examples.append({"anchor": label, "url": l["full"]})
                continue
            norm = re.sub(r"\s+", " ", text).strip().lower()
            norm = norm.strip(" .:!?…»«<>→–—-")
            if norm in GENERIC_ANCHORS:
                non_desc += 1
                ekey = (norm, l["full"])
                if ekey not in seen_ex and len(examples) < 15:
                    seen_ex.add(ekey)
                    examples.append({"anchor": text[:80], "url": l["full"]})
        out["anchor_quality"] = {"non_descriptive_count": non_desc, "examples": examples}
    except Exception:
        out["anchor_quality"] = {"non_descriptive_count": 0, "examples": []}

    # broken_links[] + redirect_links[]  (netwerk-sample, GEEN redirects volgen)
    try:
        internal_urls, external_urls = [], []
        si, se = set(), set()
        page_norm = urldefrag(url)[0].rstrip("/")
        for l in links:
            u = l["full"]
            if u.rstrip("/") == page_norm:          # eigen pagina overslaan
                continue
            if l["internal"]:
                if u not in si:
                    si.add(u)
                    internal_urls.append(u)
            else:
                if u not in se:
                    se.add(u)
                    external_urls.append(u)
        sample = _interleave(internal_urls, external_urls)[:max_link_checks]
        broken, redirects = [], []
        for u in sample:
            res = http_check(u, follow=False)
            st = res["status"]
            if st and 300 <= st < 400:
                redirects.append({"url": u, "status": st, "location": res["location"]})
            elif st >= 400:
                broken.append({"url": u, "status": st})
            elif st == 0 and res["error"] and res["error"] != "skipped":
                broken.append({"url": u, "status": 0, "error": res["error"]})
        out["broken_links"] = broken
        out["redirect_links"] = redirects
        out["links_checked"] = len(sample)
    except Exception:
        out.setdefault("broken_links", [])
        out.setdefault("redirect_links", [])
        out.setdefault("links_checked", 0)

    # -------- afbeeldingen ----------------------------------------------------
    try:
        all_imgs = soup.find_all("img")
        total = missing_alt = missing_dims = non_next_gen = lazy = 0
        img_seen = set()
        img_entries = []          # uniek per src, max_image_checks lang
        for img in all_imgs:
            total += 1
            alt = (img.get("alt") or "").strip()
            if not alt:
                missing_alt += 1
            w = str(img.get("width") or "").strip()
            h = str(img.get("height") or "").strip()
            has_dims = bool(w) and bool(h)
            if not has_dims:
                missing_dims += 1
            ld = (img.get("loading") or "").strip().lower()
            if ld == "lazy":
                lazy += 1
            src = (img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                   or img.get("data-original") or "").strip()
            if not src:
                ss = (img.get("srcset") or "").strip()
                if ss:
                    src = ss.split(",")[0].strip().split(" ")[0]
            full = ""
            if src:
                full = src if src.startswith("data:") else urljoin(url, src)
            fmt = _ext_format(full) if full else ""
            if fmt not in NEXT_GEN:
                non_next_gen += 1
            if full and full not in img_seen and len(img_entries) < max_image_checks:
                img_seen.add(full)
                img_entries.append({
                    "src": full,
                    "img_format": fmt,
                    "is_next_gen": fmt in NEXT_GEN,
                    "heeft_dimensies": has_dims,
                    "srcset_present": _has_srcset(img),
                    "sizes_present": bool((img.get("sizes") or "").strip()),
                    "img_title": (img.get("title") or "").strip(),
                    "loading": ld,
                    "alt": alt[:140],
                })

        # netwerk op de unieke image-sample (redirects WEL volgen voor echte grootte)
        oversized = broken_imgs = 0
        broken_images = []
        for e in img_entries:
            s = e["src"]
            if s.startswith("data:"):
                e["img_filesize_kb"] = None
                e["http_status"] = 0
                e["broken"] = False
                continue
            res = http_check(s, follow=True)
            e["http_status"] = res["status"]
            if res["content_type"]:
                e["content_type"] = res["content_type"]
                if not e["img_format"]:          # extensie-loos? leid af uit content-type
                    sub = res["content_type"].split(";")[0].strip().lower()
                    if "/" in sub:
                        sub = sub.split("/")[-1]
                        e["img_format"] = sub
                        e["is_next_gen"] = sub in NEXT_GEN
            cl = res["content_length"]
            e["img_filesize_kb"] = round(cl / 1024, 1) if cl else None
            if e["img_filesize_kb"] and e["img_filesize_kb"] > 100:
                oversized += 1
            e["broken"] = res["status"] >= 400
            if e["broken"]:
                broken_imgs += 1
                broken_images.append({"url": s, "status": res["status"]})

        out["image_details"] = img_entries
        out["broken_images"] = broken_images
        out["images_checked"] = len([e for e in img_entries if not e["src"].startswith("data:")])
        out["images_summary"] = {
            "total": total,
            "missing_alt": missing_alt,
            "missing_dimensions": missing_dims,
            "non_next_gen_count": non_next_gen,
            "oversized_count": oversized,      # >100KB, op de gecheckte sample
            "broken_count": broken_imgs,       # op de gecheckte sample
            "lazy_count": lazy,
        }
    except Exception:
        out.setdefault("image_details", [])
        out.setdefault("broken_images", [])
        out.setdefault("images_checked", 0)
        out.setdefault("images_summary", {})

    # background_images[]  (inline style + lazy data-bg + <style>-blokken)
    try:
        bg = []
        bseen = set()

        def addbg(raw):
            if not raw:
                return
            raw = raw.strip().strip('\'"')
            if not raw or raw.startswith("data:") or raw.lower().startswith("javascript"):
                return
            try:
                full = urljoin(url, raw)
            except Exception:
                return
            if full not in bseen and len(bg) < 60:
                bseen.add(full)
                bg.append(full)

        for el in soup.find_all(style=True):
            stv = el.get("style") or ""
            low = stv.lower()
            if "url(" in low and "background" in low:
                for m in _BG_URL.findall(stv):
                    addbg(m)
        for attr in ("data-bg", "data-background", "data-background-image",
                     "data-src-bg", "data-bg-src", "data-lazy-bg"):
            for el in soup.find_all(attrs={attr: True}):
                val = el.get(attr) or ""
                if "url(" in val.lower():
                    for m in _URL_IN_CSS.findall(val):
                        addbg(m)
                elif _IMG_EXT_IN_VAL.search(val) or val.startswith("http"):
                    addbg(val)
        for st in soup.find_all("style"):
            css = st.string or st.get_text() or ""
            if "background" in css.lower() and "url(" in css.lower():
                for m in _BG_URL.findall(css):
                    addbg(m)
        out["background_images"] = bg
    except Exception:
        out["background_images"] = []

    # transparantie over het netwerk-budget
    try:
        out["links_images_meta"] = {
            "network_calls": net_stats["calls"],
            "max_link_checks": max_link_checks,
            "max_image_checks": max_image_checks,
            "rendered": bool(ctx.get("rendered")),
        }
    except Exception:
        pass

    return out


# ------------------------------- demo / test ---------------------------------

if __name__ == "__main__":
    import sys
    try:
        import requests
        from bs4 import BeautifulSoup
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"error": f"requests/bs4 niet beschikbaar: {exc}"}))
        sys.exit(1)

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    HEADERS = {"User-Agent": UA, "Accept-Language": "nl,en;q=0.8",
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

    targets = ["https://zekermobiel.nl/", "https://www.coolblue.nl/"]
    result = {}
    for t in targets:
        entry = {}
        try:
            sess = requests.Session()
            sess.headers.update(HEADERS)
            resp = sess.get(t, timeout=20, allow_redirects=True)
            try:
                soup = BeautifulSoup(resp.text, "lxml")
            except Exception:
                soup = BeautifulSoup(resp.text, "html.parser")
            pu = urlparse(resp.url)
            ctx = {
                "url": resp.url,
                "html": resp.text,
                "soup": soup,
                "resp": resp,
                "base_url": f"{pu.scheme}://{pu.netloc}",
                "session": sess,
                "rendered": False,
                # demo houdt het netwerk klein & beleefd (module-defaults zijn 40/30):
                "max_link_checks": 15,
                "max_image_checks": 12,
                "check_delay": 0.2,
            }
            entry = extract(ctx)
            entry["_demo_fetch_status"] = resp.status_code
            entry["_demo_final_url"] = resp.url
        except Exception as exc:
            entry = {"_demo_error": str(exc)[:300]}
        result[t] = entry

    print(json.dumps(result, indent=2, ensure_ascii=False))
