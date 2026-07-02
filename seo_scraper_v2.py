#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
SEO Webscraper v2 PRO (Sage) — "alles-scraper" voor ranking-onderzoek.
Bouwt voort op Echo's seo_scraper.py (from-agent4/2026-06-09_seo-webscraper).

Basis (v2.0, 2026-06-11):
  - KEYWORDS: top-unigrams/bigrams/trigrams per pagina + site-niveau,
    keyword-densiteit, NL+EN-stopwoorden, dekking in title/H1/URL/description
  - PRODUCTEN: JSON-LD Product + microdata + heuristiek -> products.json/csv
    (naam, prijs, valuta, merk, beschrijving, rating, voorraad, afbeelding)
  - CONTENT: volledige paginatekst opgeslagen in content/*.txt + breadcrumbs
  - ANCHOR-TEKSTEN: interne linkstructuur met anchorteksten (interne-SEO-signaal)
  - PLAYWRIGHT-FALLBACK: bij 403/429/503 of lege body wordt de pagina via een
    echte headless Chromium gerenderd (omzeilt simpele botblokkades, ziet ook JS-content)
  - ANALYSE.md: rapport dat per datasoort uitlegt wat het betekent voor ranking

100%-capability + PRO-laag (v2.1, 2026-06-30, Atlas; geconsolideerd v2.2, 2026-07-02, Sage):
  - 157 VELDEN per pagina via extractors/ (head/content, links/images,
    technical/schema, performance/e-commerce incl. PSI, overig/GEO)
  - SEO-SCORE 0-100 per pagina en per site + fixlijst met severity en
    waarom/fix (scoring.py) -> score.json; per pagina seo_health_score
    (LET OP: 'seo_score' is de Lighthouse/PSI-categoriescore — andere key!)
  - HTML-DASHBOARD (report.py) -> report.html: gauges, vergelijkingstabel,
    top-issues, CWV, near-dups, orphans, PageRank
  - ROBUUSTHEID (robust.py): charset-decode, retry+backoff, redirect-loops,
    www-normalisatie, run.log — crasht nergens op
  - ANALYSE (advanced_analysis.py): near-duplicate-clustering, orphans,
    interne PageRank, --compare "concurrent verslaan" -> compare.json
  - SNELHEID: --concurrency N wave-based threadpool, --fast modus
  - PSI is FAIL-SOFT: zonder --psi geen calls; met --psi zonder key/quota
    komt er psi_note="psi_error: ..." in het record en draait de run door.
  - SITEMAPS (v2.2, afgestemd met Janus' v2_1-lijn): alle robots.txt-sitemaps
    gemergd + sitemap-index tot 10 subs gevolgd + .xml.gz-decompressie +
    robots Crawl-delay gerespecteerd; afkap wordt geprint, nooit stil.

Fase 1 — audit-machine (v2.3, 2026-07-02, Sage):
  - MODULE 1.1 SCREENSHOTS (--screenshots, screenshots.py): per pagina desktop
    1440x900 + mobiel 390x844, above-the-fold + full-page PNG, én een
    gerenderde DOM-snapshot (post-JS) -> <site>\screenshots\<slug>\
  - AUDITS-REGISTRY (audits\): plugin-contract voor modules 1.2-1.7 —
    bestand droppen = meedraaien (fail-soft, eigen report-sectie + fixlijst).
    Contract + wiring-afspraken: INTEGRATION.md. Test-harnas: _audit_harness.py.

Gebruik:
    python seo_scraper_v2.py https://voorbeeld.nl [https://site2.nl ...]
    Kern-opties: --max-pages N (30) --out DIR --compare --fast --screenshots
                 --concurrency N --psi [--psi-key KEY] --dup-threshold 0.80
                 --delay S (1.0) --no-images --no-render
"""
__version__ = "2.3.1"  # fase 1b: render-meta + design-audits 1.3/1.4/1.7 + safe_name-hash
import argparse
import csv
import gzip
import hashlib
import json
import re
import sys
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

# --- nieuwe extractor-modules inpluggen ---------------------------------------
import os
import threading
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extractors import head_content, links_images, technical_schema, performance_ecom, overig_geo
# --- pro-laag: scoring, onderscheidende analyse, HTML-dashboard, robuustheid ---
import scoring
import advanced_analysis
import report as report_mod
import robust

EXTRACTORS = [
    ("head_content", head_content),
    ("links_images", links_images),
    ("technical_schema", technical_schema),
    ("performance_ecom", performance_ecom),
    ("overig_geo", overig_geo),
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "nl,en;q=0.8",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

LOGO_HINTS = re.compile(r"logo|brand|merk", re.I)

# NL + EN stopwoorden (compact maar dekkend voor keyword-extractie)
STOPWORDS = set("""
de het een en van in op te dat die is was zijn voor met als aan er om ook maar uit bij nog naar dan
je jij u we wij ze zij ik hij hem haar hun ons onze mijn jouw uw dit deze daar hier wat wie waar hoe
niet geen wel al alle alles iets niets veel meer meest minder zo toch dus want of over onder tussen
door tegen na tot per via wordt worden werd werden heeft hebben had hadden kan kunnen kon kunt zal
zullen zou zouden moet moeten mag mogen doet doen deed gaan gaat ging komen komt kwam maken maakt
nieuw nieuwe goed goede groot grote klein kleine eigen elke elk iedere ieder zelf zonder binnen
the a an and or of to in on at for with as by from is are was were be been being this that these
those it its it's i you he she we they them his her their our your my me him us do does did done
have has had having will would can could shall should may might must not no nor so if then than
too very just about into over under again further once here there when where why how all any both
each few more most other some such only own same s t don now
""".split())

WORD_RE = re.compile(r"[a-zà-ÿ0-9][a-zà-ÿ0-9'\-]{1,}", re.I)


def tokenize(text):
    return [w.lower() for w in WORD_RE.findall(text)]


def ngrams(tokens, n):
    out = []
    for i in range(len(tokens) - n + 1):
        gram = tokens[i:i + n]
        # n-gram mag niet beginnen/eindigen met stopwoord, en niet puur cijfers zijn
        if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
            continue
        if all(g.isdigit() for g in gram):
            continue
        out.append(" ".join(gram))
    return out


def keyword_profile(text, title="", h1="", url="", description="", top=25):
    """Top-keywords (1/2/3-grams) met densiteit + waar ze terugkomen."""
    tokens = tokenize(text)
    total = max(1, len(tokens))
    uni = Counter(t for t in tokens if t not in STOPWORDS and not t.isdigit() and len(t) > 2)
    bi = Counter(ngrams(tokens, 2))
    tri = Counter(ngrams(tokens, 3))
    haystacks = {"title": title.lower(), "h1": h1.lower(),
                 "url": url.lower(), "description": description.lower()}

    def pack(counter, k):
        rows = []
        for term, cnt in counter.most_common(k):
            rows.append({
                "term": term, "count": cnt,
                "density_pct": round(100 * cnt * len(term.split()) / total, 2),
                "in": [place for place, hay in haystacks.items() if term in hay],
            })
        return rows

    return {"word_count": total,
            "top_unigrams": pack(uni, top),
            "top_bigrams": pack(bi, 15),
            "top_trigrams": pack(tri, 10)}


def safe_name(url: str) -> str:
    """Uniek, stabiel bestandsnaam per URL, mét korte hash van pad+query.
    Waarom: sites met per-product-submappen hergebruiken dezelfde bestandsnaam
    (assets/producten/<sku>/p00.cut.webp) — zonder hash overschrijven die
    downloads elkaar en wordt de beeld-audit half blind (movevolt: 139 foto's
    -> 3 bestanden; les 02-07). Formaat: <stem>_<hash8>.<ext>."""
    pu = urlparse(url)
    tag = hashlib.md5((pu.path + ("?" + pu.query if pu.query else ""))
                      .encode("utf-8", "ignore")).hexdigest()[:8]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", pu.path.split("/")[-1])[:60]
    if not base or "." not in base:
        return (base or "img") + "_" + tag + ".bin"
    stem, _, ext = base.rpartition(".")
    return (stem or "img") + "_" + tag + "." + ext


def _walk_jsonld(node):
    """Vind alle dicts in (mogelijk geneste) JSON-LD, incl. @graph en lijsten."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_jsonld(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)


def extract_products(jsonld, soup, url):
    """Producten uit JSON-LD Product, microdata en og:type=product."""
    products = []
    for doc in jsonld:
        for node in _walk_jsonld(doc):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if not any(str(x).lower() == "product" for x in types if x):
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            agg = node.get("aggregateRating") or {}
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            img = node.get("image")
            if isinstance(img, list):
                img = img[0] if img else ""
            if isinstance(img, dict):
                img = img.get("url", "")
            products.append({
                "source": "jsonld", "url": url,
                "name": node.get("name", ""),
                "description": (node.get("description") or "")[:2000],
                "brand": brand or "",
                "sku": node.get("sku", "") or node.get("mpn", ""),
                "price": offers.get("price", "") or offers.get("lowPrice", ""),
                "currency": offers.get("priceCurrency", ""),
                "availability": str(offers.get("availability", "")).split("/")[-1],
                "rating": agg.get("ratingValue", ""),
                "rating_count": agg.get("reviewCount", "") or agg.get("ratingCount", ""),
                "image": img or "",
            })
    # microdata-fallback
    for scope in soup.find_all(attrs={"itemtype": re.compile(r"schema.org/Product", re.I)}):
        def ip(prop):
            el = scope.find(attrs={"itemprop": prop})
            if not el:
                return ""
            return (el.get("content") or el.get_text(" ", strip=True) or "").strip()
        if ip("name"):
            products.append({
                "source": "microdata", "url": url,
                "name": ip("name"), "description": ip("description")[:2000],
                "brand": ip("brand"), "sku": ip("sku"),
                "price": ip("price"), "currency": ip("priceCurrency"),
                "availability": ip("availability").split("/")[-1],
                "rating": ip("ratingValue"), "rating_count": ip("reviewCount"),
                "image": ip("image"),
            })
    return products


def extract_breadcrumbs(jsonld, soup):
    for doc in jsonld:
        for node in _walk_jsonld(doc):
            if str(node.get("@type", "")).lower() == "breadcrumblist":
                items = node.get("itemListElement", [])
                names = []
                for it in items if isinstance(items, list) else []:
                    if isinstance(it, dict):
                        nm = it.get("name") or (it.get("item") or {}).get("name") if isinstance(it.get("item"), dict) else it.get("name")
                        if nm:
                            names.append(str(nm))
                if names:
                    return names
    nav = soup.find(attrs={"aria-label": re.compile("breadcrumb", re.I)}) or \
        soup.find(class_=re.compile("breadcrumb", re.I))
    if nav:
        return [a.get_text(strip=True) for a in nav.find_all("a") if a.get_text(strip=True)][:10]
    return []


class Renderer:
    """Lazy Playwright headless-Chromium voor bot-geblokkeerde of JS-zware pagina's."""
    def __init__(self):
        self._pw = self._browser = None
        self.used = 0

    def get_html(self, url, timeout_ms=30000):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        ctx = self._browser.new_context(user_agent=UA, locale="nl-NL")
        page = ctx.new_page()
        try:
            resp = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)  # JS-content even laten landen
            html = page.content()
            status = resp.status if resp else 0
            self.used += 1
            return html, status, page.url
        finally:
            ctx.close()

    def close(self):
        """Idempotent én herstartbaar: refs gaan op None zodat get_html() bij
        een volgend gebruik lazy een verse Playwright start. Nodig omdat de
        screenshot-pass (module 1.1) de renderer eerst sluit — twee sync-
        Playwright-sessies in één thread conflicteren ('Sync API inside the
        asyncio loop', les fatbikeskopen 02-07)."""
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = None


class SiteScraper:
    def __init__(self, start_url, max_pages=30, delay=1.0, download_images=True,
                 out_root=Path("output"), renderer=None, psi=False, psi_key=None,
                 concurrency=1, fast=False, dup_threshold=0.80, logger=None,
                 max_link_checks=40, max_image_checks=30, check_delay=0.25,
                 screenshots=False, include=None):
        self.start_url = start_url if start_url.startswith("http") else "https://" + start_url
        p = urlparse(self.start_url)
        self.domain = p.netloc.lower()
        self.max_pages = max_pages
        self.delay = delay
        self.download_images = download_images
        self.renderer = renderer
        self.psi = psi
        self.psi_key = psi_key
        self.screenshots = bool(screenshots)
        self.shots_manifest = {"enabled": False}
        # --include: alleen URL's die deze regex matchen worden gecrawld
        # (seed + gevonden links; de start-URL mag altijd). Fail-soft bij
        # een ongeldige regex. Voorbeeld NL-subset van een meertalige site:
        #   --include "rideparts\.nl/(?!(en|de|fr|sv|no|es|it|pl|da|fi)/)"
        self.include_rx = None
        if include:
            try:
                self.include_rx = re.compile(include)
            except re.error as e:
                print(f"  --include-regex ongeldig ({e}) — filter genegeerd")
        self.out_root = out_root
        self.out = out_root / self.domain.replace(":", "_")
        for sub in ("images", "logos", "content"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        # robuuste sessie met automatische retry + exponentiele backoff
        self.session = robust.make_session(retries=0 if renderer is None else 2,
                                           backoff=0.6, headers=HEADERS)
        self.log = logger or robust.get_logger(out_root)
        # snelheid / robuustheid-instellingen
        self.concurrency = max(1, int(concurrency))
        self.fast = bool(fast)
        self.dup_threshold = float(dup_threshold)
        self.max_link_checks = max_link_checks
        self.max_image_checks = max_image_checks
        self.check_delay = check_delay
        self.head_cache = {}           # site-brede HEAD/GET-check-cache (batchen)
        self.page_texts = {}           # url -> volledige tekst (voor near-dup), niet in output
        self.pages = []
        self.products = []
        self.anchor_counter = Counter()
        self.seen_images = set()
        self.site_seo = {}
        self.analysis = {}
        self.lock = threading.Lock()
        self.rp = robotparser.RobotFileParser()
        try:
            r = self.session.get(f"{p.scheme}://{self.domain}/robots.txt", timeout=10)
            self.robots_txt = r.text if r.ok else ""
            self.rp.parse(self.robots_txt.splitlines())
        except Exception as e:
            self.log.info("robots.txt onbereikbaar voor %s: %s", self.domain, e)
            self.robots_txt = ""
            self.rp.parse([])
        # Crawl-delay uit robots.txt respecteren: max(--delay, Crawl-delay), gecapt
        # op 10s zodat een absurde waarde de run niet doodmaakt (cap wordt gelogd).
        try:
            cd = self.rp.crawl_delay(UA)
            if cd and float(cd) > self.delay:
                eff = min(float(cd), 10.0)
                if eff < float(cd):
                    self.log.warning("%s: robots Crawl-delay=%ss gecapt op %ss", self.domain, cd, eff)
                print(f"  robots.txt Crawl-delay={cd}s -> wachttijd {eff}s")
                self.delay = eff
        except Exception:
            pass

    @staticmethod
    def _host_key(host):
        """Host genormaliseerd voor 'zelfde site'-vergelijking (www. eraf).
        Zo telt een redirect coolblue.nl -> www.coolblue.nl als dezelfde site."""
        h = (host or "").lower().strip()
        return h[4:] if h.startswith("www.") else h

    def _is_same_site(self, url):
        try:
            return self._host_key(urlparse(url).netloc) == self._host_key(self.domain)
        except Exception:
            return False

    def _visit_key(self, url):
        """Genormaliseerde dedup-sleutel: scheme weg, www. weg, trailing-slash weg,
        fragment weg (query blijft). Voorkomt dubbel crawlen van coolblue.nl én
        www.coolblue.nl, of http- én https-varianten van dezelfde pagina."""
        try:
            s = (url or "").split("#")[0]
            pu = urlparse(s)
            key = self._host_key(pu.netloc) + (pu.path.rstrip("/") or "/")
            if pu.query:
                key += "?" + pu.query
            return key
        except Exception:
            return (url or "").split("#")[0].rstrip("/")

    def allowed(self, url):
        try:
            return self.rp.can_fetch(UA, url)
        except Exception:
            return True

    def _included(self, url):
        """--include-filter (None = alles). Zie __init__/CLI-help."""
        return self.include_rx is None or bool(self.include_rx.search(url or ""))

    def fetch(self, url):
        """requests eerst (robuust gedecodeerd, met retry+backoff via de sessie);
        bij botblokkade of lege body -> optionele Playwright-render. Nooit crashen."""
        t0 = time.time()
        html, status, final_url, rendered, size = "", 0, url, False, 0
        resp = None
        try:
            r = self.session.get(url, timeout=20, allow_redirects=True)
            resp = r  # bewaar Response voor de modules (headers/redirects/ttfb/compression)
            status, final_url, size = r.status_code, r.url, len(r.content or b"")
            if robust.detect_redirect_loop(r.history, r.url):
                self.log.warning("redirect-loop overgeslagen: %s", url)
                return None
            ctype = r.headers.get("Content-Type", "")
            if robust.looks_like_html(ctype, (r.content or b"")[:2048]) or status in (403, 429, 503):
                # robuuste decode: header-charset > BOM > meta > auto-detect > utf-8.
                # Fixt mojibake op header-loze sites; correcte sites byte-identiek.
                text, enc, src, truncated = robust.decode_html(r)
                html = text
                if truncated:
                    self.log.warning("zeer grote pagina afgekapt voor parsing: %s", url)
            else:
                return None  # geen HTML (pdf, afbeelding, zip, ...)
        except requests.exceptions.RequestException as e:
            self.log.warning("requests-fout %s: %s", url, e)
        except Exception as e:
            self.log.warning("onverwachte fetch-fout %s: %s", url, e)
        blocked = status in (403, 429, 503) or len(html) < 500
        if blocked and self.renderer is not None:
            try:
                html, status, final_url = self.renderer.get_html(url)
                size = len(html.encode("utf-8", "ignore"))
                rendered = True
                resp = None  # Playwright-render -> geen requests.Response beschikbaar
                self.log.info("Playwright-render (%s) %s", status, url)
            except Exception as e:
                self.log.warning("Playwright-fout %s: %s", url, e)
        if not html:
            return None
        return {"html": html, "status": status, "url": final_url,
                "ms": round((time.time() - t0) * 1000), "size": size, "rendered": rendered,
                "resp": resp}

    def crawl(self):
        extra = ""
        if self.concurrency > 1:
            extra += f", {self.concurrency} gelijktijdig"
        if self.fast:
            extra += ", FAST"
        print(f"\n=== {self.domain} (max {self.max_pages} pagina's{extra}) ===")
        queue = deque([self.start_url])
        visited = set()
        self.sitemap_urls = self._get_sitemap()
        seeds = [u for u in self.sitemap_urls if self._included(u)]
        if self.include_rx is not None:
            print(f"  include-filter: {len(seeds)}/{len(self.sitemap_urls)} sitemap-URL's matchen")
        for u in seeds[: self.max_pages]:
            queue.append(u)

        if self.concurrency > 1:
            self._crawl_concurrent(queue, visited)
        else:
            self._crawl_sequential(queue, visited)

        self._capture_screenshots()   # module 1.1 — vóór _finalize: audits
        self._finalize()              # (1.2-1.7) werken op de échte render
        self._save()
        return self.summary()

    def _capture_screenshots(self):
        """Module 1.1 (--screenshots): desktop+mobiel fold/full-PNG's + gerenderde
        DOM per pagina. Fail-soft: elke fout wordt genoteerd, nooit een crash."""
        if not self.screenshots or not self.pages:
            return
        print(f"  screenshots: desktop 1440x900 + mobiel 390x844 voor {len(self.pages)} pagina's ...")
        try:
            # Renderer-fallback (indien gebruikt tijdens de crawl) EERST sluiten:
            # twee sync-Playwright-sessies in één thread geven "Sync API inside
            # the asyncio loop" en dan mislukken ALLE shots (les 02-07). close()
            # is herstartbaar — een volgende site her-start hem lazy.
            if self.renderer is not None:
                self.renderer.close()
            import screenshots as screenshots_mod
            self.shots_manifest = screenshots_mod.capture_site(self.pages, self.out, log=self.log)
        except Exception as e:
            self.log.warning("screenshot-module faalde voor %s: %s", self.domain, e)
            self.shots_manifest = {"enabled": True, "error": str(e)[:200]}

    def _record_page(self, res):
        """Analyseer + registreer 1 fetch-resultaat. Returnt het page-record."""
        page = self._analyse(res)
        self.pages.append(page)
        tag = " (render)" if res["rendered"] else ""
        print(f"  [{len(self.pages):>3}] {res['status']} {res['ms']:>5}ms  {res['url'][:85]}{tag}")
        return page

    def _enqueue_links(self, page, queue, visited):
        # markeer de FINALE url (na redirect) zodat een latere www-/redirect-variant
        # van dezelfde pagina niet opnieuw gecrawld wordt
        visited.add(self._visit_key(page.get("url", "")))
        for link in page["internal_links"]:
            if not self._included(link):
                continue
            ln = self._visit_key(link)
            if ln not in visited and len(queue) < self.max_pages * 5:
                queue.append(link)

    def _crawl_sequential(self, queue, visited):
        while queue and len(self.pages) < self.max_pages:
            url = queue.popleft()
            norm = self._visit_key(url)
            if norm in visited or not self.allowed(url):
                continue
            visited.add(norm)
            res = self.fetch(url)
            if not res:
                continue
            page = self._record_page(res)
            self._enqueue_links(page, queue, visited)
            if self.delay:
                time.sleep(self.delay)

    def _crawl_concurrent(self, queue, visited):
        """Wave-based BFS: een batch GELIJKTIJDIG fetchen (threadpool, cap=concurrency),
        daarna sequentieel analyseren (gedeelde staat blijft single-threaded -> veilig).
        Respecteert robots (allowed) en pauzeert `delay` tussen de waves."""
        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            while queue and len(self.pages) < self.max_pages:
                batch, seen_batch = [], set()
                while (queue and len(batch) < self.concurrency
                       and (len(self.pages) + len(batch)) < self.max_pages):
                    url = queue.popleft()
                    norm = self._visit_key(url)
                    if norm in visited or norm in seen_batch or not self.allowed(url):
                        continue
                    seen_batch.add(norm)
                    batch.append(url)
                if not batch:
                    if queue:
                        continue
                    break
                futs = {ex.submit(self.fetch, u): u for u in batch}
                fetched = {}
                for fut, u in futs.items():
                    try:
                        fetched[u] = fut.result()
                    except Exception as e:
                        self.log.warning("fetch-fout %s: %s", u, e)
                        fetched[u] = None
                for u in batch:
                    visited.add(self._visit_key(u))
                    res = fetched.get(u)
                    if not res:
                        continue
                    page = self._record_page(res)
                    self._enqueue_links(page, queue, visited)
                if self.delay:
                    time.sleep(self.delay)

    def _finalize(self):
        """Na de crawl: SEO-score per pagina + per site, de onderscheidende
        analyse (near-duplicates, orphans, interne-link-PageRank) en de
        audit-plugins (audits/ — modules 1.2-1.7). Alles fail-soft."""
        try:
            self.site_seo = scoring.score_site(self.pages, psi_enabled=self.psi)
        except Exception as e:
            self.log.warning("scoring-fout %s: %s", self.domain, e)
            self.site_seo = {}
        try:
            self.analysis = advanced_analysis.analyze_site(
                self.pages, self.page_texts, getattr(self, "sitemap_urls", []),
                dup_threshold=self.dup_threshold)
        except Exception as e:
            self.log.warning("analyse-fout %s: %s", self.domain, e)
            self.analysis = {}
        self.analysis["screenshots"] = self.shots_manifest
        self.analysis["audits"] = self._run_audits()

    def _audit_ctx(self):
        """Context voor audit-modules — zie INTEGRATION.md (contract).
        Alleen keys TOEVOEGEN, nooit hernoemen/verwijderen (compat)."""
        return {
            "domain": self.domain,
            "pages": self.pages,                 # post-extractors; incl. p["screenshots"] als 1.1 aanstond
            "page_texts": self.page_texts,       # url -> volledige tekst
            "sitemap_urls": getattr(self, "sitemap_urls", []),
            "analysis": self.analysis,           # near-dups/orphans/link_graph/screenshots
            "products": self.products,
            "out": self.out,                     # Path site-outputmap (screenshots/, content/, ...)
            "screenshots": self.shots_manifest,  # manifest module 1.1
            "safe_name": safe_name,              # src-URL -> bestandsnaam in images/
            "log": self.log,
            "fast": self.fast,
            "psi_enabled": self.psi,
        }

    def _run_audits(self):
        """Draai alle audits/-plugins (registry). Eén kapotte audit = één
        error-entry in de output, nooit een gebroken run (fail-soft)."""
        results = {}
        try:
            import audits as audits_pkg
            ctx = self._audit_ctx()
            for key, mod in audits_pkg.discover():
                label = getattr(mod, "LABEL", key)
                try:
                    res = mod.audit(ctx)
                    if not isinstance(res, dict):
                        raise TypeError("audit() gaf geen dict terug")
                    res.setdefault("label", label)
                    res.setdefault("order", getattr(mod, "ORDER", 500))
                    results[key] = res
                except Exception as e:
                    self.log.warning("audit '%s' faalde (%s): %s", key, self.domain, e)
                    results[key] = {"label": label, "error": str(e)[:200],
                                    "order": getattr(mod, "ORDER", 500)}
        except Exception as e:
            self.log.warning("audits-discovery faalde: %s", e)
        return results

    def _fetch_xml(self, url):
        """Sitemap ophalen als BeautifulSoup; decomprimeert .xml.gz-bestanden
        (magic-bytes of .gz-extensie — transfer-encoding gzip dekt requests al af)."""
        try:
            r = self.session.get(url, timeout=15)
            if not r.ok:
                return None
            content = r.content
            if url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
                try:
                    content = gzip.decompress(content)
                except Exception:
                    pass
            text = content.decode("utf-8", "ignore")
            if "<" not in text:
                return None
            return BeautifulSoup(text, "lxml-xml")
        except Exception:
            return None

    def _get_sitemap(self):
        """Sitemap-URLs verzamelen over ÁLLE aangekondigde sitemaps (robots.txt-
        Sitemap-regels in bron-volgorde + /sitemap.xml als vangnet), gemergd en
        gededupliceerd; een sitemap-INDEX wordt over max 10 sub-sitemaps gevolgd.
        Stoppen bij de eerste niet-lege bron mist bij meertalige sites hele
        taalversies (movevolt: 4 robots-regels, sitemap-de.xml won als eerste).
        URL-cap max(2000, max_pages*5); elke afkap wordt geprint, nooit stil."""
        cap = max(2000, self.max_pages * 5)
        candidates = []
        for line in self.robots_txt.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(line.split(":", 1)[1].strip())
        candidates.append(f"https://{self.domain}/sitemap.xml")
        candidates = list(dict.fromkeys(c for c in candidates if c))[:5]

        def locs(soup):
            return [l.get_text(strip=True) for l in soup.find_all("loc")]

        urls, seen_sm, sources, capped = [], set(), 0, False
        for sm in candidates:
            if sm in seen_sm or capped:
                continue
            seen_sm.add(sm)
            soup = self._fetch_xml(sm)
            if soup is None:
                continue
            sources += 1
            if soup.find("sitemapindex"):
                subs = locs(soup)
                if len(subs) > 10:
                    print(f"  sitemap-index {sm}: {len(subs)} sub-sitemaps, eerste 10 gevolgd")
                for sub in subs[:10]:
                    if sub in seen_sm:
                        continue
                    seen_sm.add(sub)
                    sub_soup = self._fetch_xml(sub)
                    if sub_soup:
                        urls += locs(sub_soup)
                    if len(urls) >= cap:
                        capped = True
                        break
            else:
                urls += locs(soup)
            if len(urls) >= cap:
                capped = True
        if capped:
            print(f"  sitemap: afgekapt op {cap} URLs (cap = max(2000, max_pages*5))")
        urls = [u for u in dict.fromkeys(urls) if self._is_same_site(u)]
        if urls:
            (self.out / "sitemap-urls.txt").write_text("\n".join(urls), encoding="utf-8")
            print(f"  sitemap: {len(urls)} URLs via {sources} sitemap-bron(nen)")
        return urls

    def _analyse(self, res):
        soup = BeautifulSoup(res["html"], "lxml")
        url = res["url"]

        def meta(name=None, prop=None):
            tag = soup.find("meta", attrs={"name": name} if name else {"property": prop})
            return (tag.get("content") or "").strip() if tag else ""

        jsonld = []
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                jsonld.append(json.loads(s.string or ""))
            except Exception:
                pass

        headings = {f"h{i}": [h.get_text(" ", strip=True)[:200] for h in soup.find_all(f"h{i}")]
                    for i in range(1, 7)}
        products = extract_products(jsonld, soup, url)
        self.products.extend(products)
        breadcrumbs = extract_breadcrumbs(jsonld, soup)

        # interne anchor-teksten verzamelen vóór tekst-extractie
        internal, external = [], []
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            if not href.startswith("http"):
                continue
            if self._is_same_site(href):
                internal.append(href)
                anchor = a.get_text(" ", strip=True)[:80]
                if anchor:
                    self.anchor_counter[anchor.lower()] += 1
            else:
                external.append(href)

        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.extract()
        text = soup.get_text(" ", strip=True)
        # tekst in geheugen bewaren voor near-duplicate-analyse (niet in output)
        self.page_texts[url] = text[:40000]

        title = soup.title.get_text(strip=True) if soup.title else ""
        h1 = headings["h1"][0] if headings["h1"] else ""
        desc = meta("description")
        kw = keyword_profile(text, title=title, h1=h1, url=url, description=desc)

        # volledige content bewaren
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", urlparse(url).path).strip("_") or "home"
        (self.out / "content" / f"{slug[:100]}.txt").write_text(text, encoding="utf-8")

        images, missing_alt, logos = [], 0, []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src:
                continue
            full = urljoin(url, src)
            alt = (img.get("alt") or "").strip()
            if not alt:
                missing_alt += 1
            images.append({"src": full, "alt": alt, "width": img.get("width"),
                           "height": img.get("height"), "loading": img.get("loading")})
            blob = " ".join([src, alt, " ".join(img.get("class", [])), img.get("id") or ""])
            if LOGO_HINTS.search(blob):
                logos.append(full)
        og_img = meta(prop="og:image")
        if og_img:
            logos.append(urljoin(url, og_img))
        for l in soup.find_all("link", rel=re.compile("icon", re.I)):
            if l.get("href"):
                logos.append(urljoin(url, l["href"]))
        if self.download_images:
            self._download(images[:25], self.out / "images")
            self._download([{"src": u} for u in logos], self.out / "logos")

        hreflangs = [{"lang": l.get("hreflang"), "href": l.get("href")}
                     for l in soup.find_all("link", rel="alternate") if l.get("hreflang")]
        canonical = soup.find("link", rel="canonical")

        record = {
            "url": url, "status": res["status"], "response_ms": res["ms"],
            "size_kb": round(res["size"] / 1024, 1), "rendered_js": res["rendered"],
            "https": url.startswith("https://"),
            "title": title, "title_length": len(title),
            "meta_description": desc, "meta_description_length": len(desc),
            "meta_keywords": meta("keywords"), "meta_robots": meta("robots"),
            "viewport": meta("viewport"),
            "canonical": canonical.get("href") if canonical else "",
            "hreflang": hreflangs,
            "og": {k: meta(prop=f"og:{k}") for k in ("title", "description", "image", "type", "url", "site_name")},
            "twitter": {k: meta(f"twitter:{k}") for k in ("card", "title", "description", "image")},
            "jsonld_types": sorted({str(n.get("@type")) for d in jsonld for n in _walk_jsonld(d) if n.get("@type")}),
            "jsonld": jsonld,
            "headings": headings, "h1_count": len(headings["h1"]),
            "breadcrumbs": breadcrumbs,
            "keywords": kw, "word_count": kw["word_count"],
            "products_found": len(products),
            "internal_links": sorted(set(internal)),
            "external_links_count": len(set(external)),
            "image_count": len(images), "images_missing_alt": missing_alt,
            "images": images, "logo_candidates": sorted(set(logos)),
            "lang": (soup.html.get("lang") if soup.html else "") or "",
        }

        # --- nieuwe extractor-modules draaien op een VERSE soup ----------------
        # De bovenstaande `soup` is gemuteerd (script/style/svg verwijderd); de
        # modules hebben de VOLLEDIGE HTML nodig (o.a. JSON-LD in <script>!).
        from urllib.parse import urlparse as _up
        soup_fresh = BeautifulSoup(res["html"], "lxml")
        pu = _up(url)
        ctx = {"url": url, "html": res["html"], "soup": soup_fresh, "resp": res.get("resp"),
               "base_url": f"{pu.scheme}://{pu.netloc}", "session": self.session,
               "rendered": res["rendered"], "delay": self.delay,
               "psi": getattr(self, "psi", False), "psi_key": getattr(self, "psi_key", None),
               "head_cache": self.head_cache,
               "max_link_checks": self.max_link_checks,
               "max_image_checks": self.max_image_checks,
               "check_delay": self.check_delay}
        for name, mod in EXTRACTORS:
            try:
                new = mod.extract(ctx) or {}
                for k, v in new.items():
                    if k not in record:          # bestaande sleutels NOOIT overschrijven
                        record[k] = v
            except Exception as e:
                record[f"_{name}_error"] = str(e)[:200]
        return record

    def _download(self, items, folder):
        for it in items:
            src = it["src"]
            if src in self.seen_images or src.startswith("data:"):
                continue
            self.seen_images.add(src)
            try:
                r = self.session.get(src, timeout=15)
                if r.ok and len(r.content) > 100:
                    (folder / safe_name(src)).write_bytes(r.content)
            except Exception:
                pass

    def _site_keywords(self, top=30):
        """Site-brede keyword-telling over alle pagina's heen."""
        uni, bi = Counter(), Counter()
        for p in self.pages:
            for row in p["keywords"]["top_unigrams"]:
                uni[row["term"]] += row["count"]
            for row in p["keywords"]["top_bigrams"]:
                bi[row["term"]] += row["count"]
        return {"unigrams": uni.most_common(top), "bigrams": bi.most_common(20)}

    def _save(self):
        (self.out / "robots.txt").write_text(self.robots_txt, encoding="utf-8")
        # heel record dumpen; alleen als pages.json onleesbaar groot wordt de rauwe
        # jsonld weglaten (jsonld_types + schema_* blijven behouden voor analyse).
        raw = json.dumps(self.pages, ensure_ascii=False, indent=2)
        if len(raw.encode("utf-8", "ignore")) > 8_000_000:
            slim = []
            for p in self.pages:
                q = dict(p)
                q.pop("jsonld", None)
                q["jsonld_omitted_from_pagesjson"] = True
                slim.append(q)
            raw = json.dumps(slim, ensure_ascii=False, indent=2)
            print("  pages.json > 8MB -> rauwe jsonld weggelaten (jsonld_types behouden)")
        (self.out / "pages.json").write_text(raw, encoding="utf-8")
        cols = ["url", "status", "rendered_js", "response_ms", "size_kb", "title", "title_length",
                "meta_description", "meta_description_length", "h1_count", "word_count",
                "products_found", "image_count", "images_missing_alt", "external_links_count",
                "canonical", "meta_robots", "lang"]
        with open(self.out / "pages.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols + ["h1", "top_keywords", "jsonld_types", "og_title",
                               "internal_links_count", "indexability", "security_headers_count",
                               "broken_links_count", "readability_flesch", "merchant_listing_ready"])
            for p in self.pages:
                top_kw = ", ".join(r["term"] for r in p["keywords"]["top_unigrams"][:8])
                sh = p.get("security_headers") or {}
                sh_count = sum(1 for v in sh.values() if isinstance(v, dict) and v.get("present"))
                es = p.get("ecommerce_summary") or {}
                w.writerow([p[c] for c in cols] + [
                    " | ".join(p["headings"]["h1"]), top_kw,
                    ", ".join(p["jsonld_types"]), p["og"]["title"], len(p["internal_links"]),
                    p.get("indexability"), sh_count, len(p.get("broken_links") or []),
                    p.get("readability_flesch"), es.get("merchant_listing_ready")])
        # producten
        if self.products:
            (self.out / "products.json").write_text(
                json.dumps(self.products, ensure_ascii=False, indent=2), encoding="utf-8")
            pcols = ["name", "price", "currency", "brand", "sku", "availability",
                     "rating", "rating_count", "description", "image", "url", "source"]
            with open(self.out / "products.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(pcols)
                for pr in self.products:
                    w.writerow([pr.get(c, "") for c in pcols])
        # keywords + anchors site-breed
        (self.out / "keywords.json").write_text(
            json.dumps(self._site_keywords(), ensure_ascii=False, indent=2), encoding="utf-8")
        (self.out / "anchors.json").write_text(
            json.dumps(self.anchor_counter.most_common(60), ensure_ascii=False, indent=2), encoding="utf-8")
        # SEO-score (per pagina + site) -> score.json
        try:
            page_scores = [{"url": p.get("url"), "score": p.get("seo_health_score"),
                            "grade": p.get("seo_grade"),
                            "issue_counts": p.get("seo_issue_counts"),
                            "breakdown": p.get("seo_breakdown"),
                            "issues": p.get("seo_issues")} for p in self.pages]
            (self.out / "score.json").write_text(json.dumps(
                {"domain": self.domain, "psi_enabled": self.psi,
                 "site": self.site_seo, "pages": page_scores},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.log.warning("score.json schrijven faalde: %s", e)
        # onderscheidende analyse -> analysis.json
        try:
            (self.out / "analysis.json").write_text(json.dumps(
                self.analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.log.warning("analysis.json schrijven faalde: %s", e)
        print(f"  opgeslagen in {self.out}"
              + (f"  [SEO-score {self.site_seo.get('site_score')}"
                 f" ({self.site_seo.get('site_grade')})]" if self.site_seo else ""))

    def summary(self):
        ps = self.pages
        if not ps:
            return {"domain": self.domain, "pages": 0}
        n = len(ps)
        home = ps[0]
        site_kw = self._site_keywords()
        return {
            "domain": self.domain, "pages": n,
            "https": all(p["https"] for p in ps),
            "rendered_js_pages": sum(1 for p in ps if p["rendered_js"]),
            "avg_response_ms": round(sum(p["response_ms"] for p in ps) / n),
            "avg_word_count": round(sum(p["word_count"] for p in ps) / n),
            "avg_title_len": round(sum(p["title_length"] for p in ps) / n),
            "avg_desc_len": round(sum(p["meta_description_length"] for p in ps) / n),
            "pct_with_description": round(100 * sum(1 for p in ps if p["meta_description"]) / n),
            "pct_with_canonical": round(100 * sum(1 for p in ps if p["canonical"]) / n),
            "pct_with_jsonld": round(100 * sum(1 for p in ps if p["jsonld"]) / n),
            "unique_descriptions": len({p["meta_description"] for p in ps if p["meta_description"]}),
            "pct_single_h1": round(100 * sum(1 for p in ps if p["h1_count"] == 1) / n),
            "total_images": sum(p["image_count"] for p in ps),
            "pct_images_missing_alt": round(100 * sum(p["images_missing_alt"] for p in ps)
                                            / max(1, sum(p["image_count"] for p in ps))),
            "jsonld_types": sorted({t for p in ps for t in p["jsonld_types"]}),
            "products_found": len(self.products),
            "top_keywords": [t for t, _ in site_kw["unigrams"][:15]],
            "top_bigrams": [t for t, _ in site_kw["bigrams"][:10]],
            "top_anchors": [a for a, _ in self.anchor_counter.most_common(10)],
            "pct_with_breadcrumbs": round(100 * sum(1 for p in ps if p["breadcrumbs"]) / n),
            "has_sitemap": bool(self.sitemap_urls), "sitemap_urls": len(self.sitemap_urls),
            "has_robots": bool(self.robots_txt),
            "homepage_title": home["title"], "homepage_description": home["meta_description"],
            "lang": home["lang"],
            "coverage": compute_coverage(self.pages, self.psi),
            "seo": self.site_seo,
            "screenshots_summary": {
                "enabled": bool(self.shots_manifest.get("enabled")),
                "ok": self.shots_manifest.get("ok", 0),
                "failed": len(self.shots_manifest.get("failed", []) or []),
            },
            "audits_summary": {
                k: {"score": v.get("score"),
                    "issues": len(v.get("issues", []) or []),
                    "error": v.get("error")}
                for k, v in (self.analysis.get("audits") or {}).items()
            },
            "analysis_summary": {
                "near_duplicate_clusters": len((self.analysis.get("near_duplicates") or {}).get("clusters", [])),
                "orphan_sitemap": (self.analysis.get("orphans") or {}).get("orphan_sitemap_count", 0),
                "orphan_crawled": (self.analysis.get("orphans") or {}).get("orphan_crawled_count", 0),
                "top_pagerank": (self.analysis.get("link_graph") or {}).get("top", [])[:5],
            },
        }


# ============================================================================ #
# DEKKINGSSCORE — meetlat = volledige lijst velden die een top-SEO-scraper hoort
# te produceren, in 9 categorieën. Twee cijfers: (a) capability-dekking = %
# meetlat-velden dat de scraper überhaupt produceert; (b) fill-rate per site =
# gem. % van de TOEPASSELIJKE velden dat een betekenisvolle waarde heeft.
# ============================================================================ #
COVERAGE_FIELDS = {
    "1_head_meta": [
        # bestaand
        "title", "title_length", "meta_description", "meta_description_length",
        "meta_keywords", "meta_robots", "viewport", "canonical", "hreflang",
        "og", "twitter", "lang",
        # nieuw (head_content)
        "charset", "theme_color", "msapplication_tilecolor", "apple_touch_icon",
        "doctype", "meta_author", "meta_refresh", "x_robots_tag", "robots_directives",
        "title_pixel_width", "title_truncation_risk", "description_pixel_width",
        "description_truncation_risk", "description_length_issue", "title_equals_h1",
    ],
    "2_content": [
        # bestaand
        "headings", "h1_count", "word_count", "keywords", "breadcrumbs",
        # nieuw (head_content)
        "heading_issues", "text_html_ratio", "avg_words_per_sentence",
        "readability_flesch", "readability_class", "language_detected",
        "language_matches_attr", "content_md5", "normalized_text_length",
        "thin_content", "placeholder_content", "publish_date", "modified_date",
    ],
    "3_technical": [
        # bestaand
        "status", "response_ms", "size_kb", "https", "rendered_js",
        # nieuw (technical_schema)
        "security_headers", "redirect_chain", "redirect_count", "redirect_type",
        "redirect_loop", "ttfb_ms", "indexability", "indexability_reason",
        "mixed_content", "mixed_content_count", "url_structure", "canonical_conflict",
        "server_header", "powered_by", "http_version", "cdn_detected", "tls_cert",
        "robots_txt_ai_bots",
    ],
    "4_links": [
        # bestaand
        "internal_links", "external_links_count",
        # nieuw (links_images)
        "external_links", "link_rel_summary", "unsafe_cross_origin", "anchor_quality",
        "broken_links", "redirect_links", "outlink_count", "links_checked",
    ],
    "5_images": [
        # bestaand
        "images", "image_count", "images_missing_alt", "logo_candidates",
        # nieuw (links_images)
        "image_details", "background_images", "broken_images", "images_summary",
        "images_checked",
    ],
    "6_structured_data": [
        # bestaand
        "jsonld", "jsonld_types", "products_found",
        # nieuw (technical_schema)
        "schema_types_detailed", "schema_validation", "rich_result_eligible",
        "fabricated_aggregaterating", "rdfa_present", "microdata_types", "schema_drift",
    ],
    "7_performance": [
        # nieuw (performance_ecom) — headers altijd, PSI alleen met --psi
        "compression", "caching_headers", "has_caching", "http_protocol", "keep_alive",
        # PSI veld-data (CrUX)
        "lcp_field", "inp_field", "cls_field", "fcp_field", "ttfb_field",
        # PSI lab-data (Lighthouse)
        "fcp", "lcp", "tbt", "cls", "speed_index", "tti", "total_byte_weight",
        "page_size_total_kb", "network_requests", "render_blocking_resources",
        "unused_css_kb", "unused_js_kb", "uses_text_compression", "dom_size",
        "server_response_time",
        # PSI scores
        "performance_score", "seo_score", "accessibility_score", "best_practices_score",
    ],
    "8_ecommerce": [
        # nieuw (performance_ecom)
        "gtin", "gtin13", "ean", "upc", "mpn", "sale_price", "compare_at_price",
        "price_valid_until", "condition", "reviews", "offer_shipping", "offer_return",
        "multiple_offers", "variants", "stock_count", "ecommerce_summary",
    ],
    "9_overig_geo": [
        # nieuw (overig_geo)
        "pagination", "amphtml", "pwa_manifest", "rss_feeds", "social_profiles",
        "nap", "well_known", "page_404_quality", "citability_signals",
        "brand_mention_signals",
    ],
}

# PSI-velden zijn alleen 'toepasselijk' wanneer --psi aanstaat (anders None).
PSI_FIELDS = {
    "lcp_field", "inp_field", "cls_field", "fcp_field", "ttfb_field",
    "fcp", "lcp", "tbt", "cls", "speed_index", "tti", "total_byte_weight",
    "page_size_total_kb", "network_requests", "render_blocking_resources",
    "unused_css_kb", "unused_js_kb", "uses_text_compression", "dom_size",
    "server_response_time", "performance_score", "seo_score",
    "accessibility_score", "best_practices_score",
}

CATEGORY_LABELS = {
    "1_head_meta": "1 Head/meta", "2_content": "2 Content",
    "3_technical": "3 Technisch", "4_links": "4 Links",
    "5_images": "5 Afbeeldingen", "6_structured_data": "6 Structured-data",
    "7_performance": "7 Performance", "8_ecommerce": "8 E-commerce",
    "9_overig_geo": "9 Overig/GEO",
}


def _is_meaningful(v):
    """BETEKENISVOLLE waarde? Niet None/""/[]/{}/0. Booleans tellen ALTIJD mee
    (een berekende True/False is een echt antwoord). Een dict met uitsluitend
    lege/None-waarden (bv. PSI-defaults) telt als leeg."""
    if v is None:
        return False
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, dict):
        return any(_is_meaningful(x) for x in v.values()) if v else False
    if isinstance(v, (list, tuple, set)):
        return len(v) > 0
    return True


def _applicable_fields(cat_key, record, psi_enabled):
    """Welke velden in deze categorie zijn TOEPASSELIJK voor deze pagina?
    - PSI-velden alleen bij --psi.
    - E-commerce-velden alleen op een product-pagina (anders n.v.t.)."""
    fields = COVERAGE_FIELDS[cat_key]
    if cat_key == "7_performance" and not psi_enabled:
        return [f for f in fields if f not in PSI_FIELDS]
    if cat_key == "8_ecommerce":
        es = record.get("ecommerce_summary") or {}
        is_product = bool(es.get("has_product_schema")) or bool(record.get("products_found"))
        return fields if is_product else []
    return fields


def compute_coverage(pages, psi_enabled=False):
    """Bereken capability-dekking (key geproduceerd) + fill-rate (betekenisvol gevuld)."""
    cats = list(COVERAGE_FIELDS.keys())
    all_fields = [(c, f) for c in cats for f in COVERAGE_FIELDS[c]]
    total_fields = len(all_fields)

    produced, ever_meaningful = set(), set()
    for p in pages:
        for c in cats:
            for f in COVERAGE_FIELDS[c]:
                if f in p:
                    produced.add((c, f))
                    if _is_meaningful(p.get(f)):
                        ever_meaningful.add((c, f))

    cap_per_cat = {}
    for c in cats:
        flds = COVERAGE_FIELDS[c]
        prod = sum(1 for f in flds if (c, f) in produced)
        cap_per_cat[c] = {"total": len(flds), "produced": prod,
                          "pct": round(100 * prod / len(flds), 1) if flds else 0.0}
    capability_pct = round(100 * len(produced) / total_fields, 1) if total_fields else 0.0
    never_filled = sorted(f"{c}:{f}" for (c, f) in all_fields if (c, f) not in ever_meaningful)

    fill_per_cat = {}
    for c in cats:
        ratios = []
        for p in pages:
            appl = _applicable_fields(c, p, psi_enabled)
            if not appl:
                continue
            filled = sum(1 for f in appl if _is_meaningful(p.get(f)))
            ratios.append(filled / len(appl))
        fill_per_cat[c] = round(100 * sum(ratios) / len(ratios), 1) if ratios else None

    overall = []
    for p in pages:
        appl_all = filled_all = 0
        for c in cats:
            appl = _applicable_fields(c, p, psi_enabled)
            appl_all += len(appl)
            filled_all += sum(1 for f in appl if _is_meaningful(p.get(f)))
        if appl_all:
            overall.append(filled_all / appl_all)
    overall_pct = round(100 * sum(overall) / len(overall), 1) if overall else None

    return {
        "pages": len(pages), "psi_enabled": bool(psi_enabled),
        "total_fields": total_fields, "produced_fields": len(produced),
        "capability_pct": capability_pct, "capability_per_category": cap_per_cat,
        "fill_rate_overall_pct": overall_pct, "fill_rate_per_category": fill_per_cat,
        "never_filled_fields": never_filled,
    }


def write_coverage(scrapers, summaries, out_root: Path, psi_enabled=False):
    """Schrijf COVERAGE.md (capability + fill-rate per categorie/site)."""
    cats = list(COVERAGE_FIELDS.keys())
    all_pages = [p for s in scrapers for p in getattr(s, "pages", [])]
    glob = compute_coverage(all_pages, psi_enabled)
    doms = [s.get("domain", "?") for s in summaries]

    L = ["# DEKKINGSSCORE — hoe dicht zit de scraper bij een top-SEO-scraper?", ""]
    L += [
        f"Meetlat: **{glob['total_fields']} veld-keys** in 9 categorieën die een top-SEO-"
        "scraper hoort te leveren (de bestaande velden + alle nieuwe module-velden).", "",
        "Twee cijfers:",
        "- **(a) Capability-dekking** = % van de meetlat-velden dat de scraper überhaupt "
        "PRODUCEERT (de key verschijnt in de output). Dit beantwoordt \"zitten we op 100%?\".",
        "- **(b) Fill-rate** = gemiddeld % van de TOEPASSELIJKE velden dat per pagina een "
        "betekenisvolle waarde heeft (niet None/\"\"/[]/{}/0; booleans tellen als ingevuld).", "",
        "Toepasselijkheid: PSI-velden tellen alleen mee als `--psi` aanstaat; e-commerce-"
        "velden alleen op product-pagina's. Daardoor is fill-rate < 100% volkomen normaal "
        "(een homepage hééft nu eenmaal geen GTIN of voorraad).", "",
        f"PSI bij deze run: **{'aan' if psi_enabled else 'uit'}**.", "",
        "---", "",
        f"## (a) Capability-dekking: {glob['capability_pct']}%  "
        f"({glob['produced_fields']}/{glob['total_fields']} velden geproduceerd)", "",
        "| Categorie | Meetlat-velden | Geproduceerd | Dekking |",
        "| --- | ---: | ---: | ---: |",
    ]
    for c in cats:
        cc = glob["capability_per_category"][c]
        L.append(f"| {CATEGORY_LABELS[c]} | {cc['total']} | {cc['produced']} | {cc['pct']}% |")
    L.append(f"| **Totaal** | **{glob['total_fields']}** | **{glob['produced_fields']}** "
             f"| **{glob['capability_pct']}%** |")

    L += ["", "---", "",
          f"## (b) Fill-rate per categorie per site", "",
          "Gemiddeld % betekenisvol gevulde toepasselijke velden over de gescande pagina's.", "",
          "| Categorie | " + " | ".join(doms) + " | ALLE |",
          "| --- | " + " | ".join("---:" for _ in doms) + " | ---: |"]
    for c in cats:
        cells = []
        for s in summaries:
            v = (s.get("coverage") or {}).get("fill_rate_per_category", {}).get(c)
            cells.append("n.v.t." if v is None else f"{v}%")
        gv = glob["fill_rate_per_category"].get(c)
        cells.append("n.v.t." if gv is None else f"{gv}%")
        L.append(f"| {CATEGORY_LABELS[c]} | " + " | ".join(cells) + " |")
    over_cells = []
    for s in summaries:
        v = (s.get("coverage") or {}).get("fill_rate_overall_pct")
        over_cells.append("n.v.t." if v is None else f"{v}%")
    gov = glob["fill_rate_overall_pct"]
    over_cells.append("n.v.t." if gov is None else f"{gov}%")
    L.append(f"| **Totaal (alle cat.)** | " + " | ".join(over_cells) + " |")

    nf = glob["never_filled_fields"]
    L += ["", "---", "",
          f"## Velden die op GEEN enkele pagina gevuld raakten ({len(nf)})", ""]
    if not nf:
        L.append("Geen — elk meetlat-veld kreeg op minstens één pagina een waarde.")
    else:
        L.append("Deze velden produceert de scraper wél (capability), maar geen testpagina "
                 "triggerde een waarde. Vaak terecht (PSI uit, geen reviews/varianten op deze "
                 "sites, of een afwezig signaal zoals AMP/RSS):")
        L.append("")
        for f in nf:
            L.append(f"- `{f}`")
    (out_root / "COVERAGE.md").write_text("\n".join(L), encoding="utf-8")
    print(f"Dekkingsrapport: {out_root / 'COVERAGE.md'}  "
          f"(capability {glob['capability_pct']}%, fill-rate {glob['fill_rate_overall_pct']}%)")


def write_compare(summaries, out_root: Path):
    if not summaries:
        return
    rows = [
        ("Pagina's gescand", "pages"), ("Via Playwright gerenderd", "rendered_js_pages"),
        ("HTTPS overal", "https"),
        ("Gem. responstijd (ms)", "avg_response_ms"), ("Gem. woordenaantal", "avg_word_count"),
        ("Gem. title-lengte", "avg_title_len"), ("Gem. description-lengte", "avg_desc_len"),
        ("% met meta description", "pct_with_description"),
        ("Unieke descriptions", "unique_descriptions"),
        ("% met canonical", "pct_with_canonical"), ("% met JSON-LD", "pct_with_jsonld"),
        ("% met precies 1 H1", "pct_single_h1"), ("% met breadcrumbs", "pct_with_breadcrumbs"),
        ("Producten gevonden", "products_found"),
        ("Totaal afbeeldingen", "total_images"),
        ("% afbeeldingen zonder alt", "pct_images_missing_alt"),
        ("Sitemap aanwezig", "has_sitemap"), ("URLs in sitemap", "sitemap_urls"),
        ("robots.txt aanwezig", "has_robots"), ("Taal (html lang)", "lang"),
    ]
    lines = ["# SEO-vergelijking (v2)", "",
             "| Signaal | " + " | ".join(s["domain"] for s in summaries) + " |",
             "| --- | " + " | ".join("---" for _ in summaries) + " |"]
    for label, key in rows:
        lines.append(f"| {label} | " + " | ".join(str(s.get(key, "-")) for s in summaries) + " |")
    lines += ["", "## Per site", ""]
    for s in summaries:
        lines += [f"### {s['domain']}",
                  f"- Title: {s.get('homepage_title','-')}",
                  f"- Description: {s.get('homepage_description','-') or '(ontbreekt)'}",
                  f"- Top-keywords: {', '.join(s.get('top_keywords', [])) or '-'}",
                  f"- Top-woordcombinaties: {', '.join(s.get('top_bigrams', [])) or '-'}",
                  f"- Meest gebruikte interne anchorteksten: {', '.join(s.get('top_anchors', [])) or '-'}",
                  f"- JSON-LD types: {', '.join(s.get('jsonld_types', [])) or '(geen)'}", ""]
    (out_root / "COMPARE.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nVergelijkingsrapport: {out_root / 'COMPARE.md'}")


def write_analyse(summaries, out_root: Path):
    """Rapport: welke verzamelde data telt waarvoor mee in ranking."""
    lines = ["# ANALYSE — welke data telt waarvoor mee bij Google-ranking", "",
             "Per datasoort: waar je het vindt in de output, en waarom het ertoe doet.", "",
             "| Data (output) | Ranking-relevantie |",
             "| --- | --- |",
             "| `title` + lengte (pages.csv) | Sterkste on-page signaal; keyword vooraan, 50-60 tekens. |",
             "| `meta_description` | Geen direct ranking-signaal, wel CTR in de SERP (indirect ranking). |",
             "| `headings` h1-h6 | Structuur + keyword-context; precies 1 H1 per pagina. |",
             "| `keywords` (keywords.json, per pagina in pages.json) | Toont waar de site over 'gaat'; densiteit ~0,5-2% is gezond, hoger oogt als keyword stuffing. `in: [title,h1,url]` = goed uitgelijnd. |",
             "| `word_count` + content/*.txt | Dunne content (<300 woorden) rankt zelden voor competitieve termen; diepgang wint. |",
             "| `jsonld` / `products.json` | Structured data geeft rich results (sterren, prijs, voorraad) -> hogere CTR; Product+Offer+AggregateRating is de e-commerce-kern. |",
             "| `breadcrumbs` | Sitestructuur-signaal + breadcrumb-weergave in SERP. |",
             "| anchors.json (interne anchorteksten) | Interne links met beschrijvende anchors verdelen autoriteit en vertellen Google waar de doelpagina over gaat. 'Lees meer' zegt niets. |",
             "| `images` + alt-teksten | Alt = ranking in Google Afbeeldingen + context voor de pagina; ontbrekende alts zijn quick wins. |",
             "| `canonical`, `meta_robots`, `hreflang` | Indexatie-hygiëne: voorkomt duplicate content en verkeerde landversies. |",
             "| `response_ms`, `size_kb` | Proxy voor Core Web Vitals (page experience); trage/zware pagina's verliezen bij gelijke content. |",
             "| `og`/`twitter` | Geen ranking, wel deelbaarheid/CTR via social. |",
             "| sitemap + robots.txt | Crawlbaarheid: alles wat Google niet vindt, rankt niet. |",
             "| `rendered_js` | True = content stond NIET in de eerste HTML; Google kan dat indexeren maar later/onbetrouwbaarder. Kritieke content hoort in de eerste HTML. |",
             "", "## Bevindingen per site", ""]
    for s in summaries:
        if not s.get("pages"):
            lines += [f"### {s['domain']}", "- Geen pagina's opgehaald.", ""]
            continue
        issues = []
        if s["pct_with_description"] < 100:
            issues.append(f"{100-s['pct_with_description']}% van pagina's mist meta description (CTR-verlies)")
        if s["unique_descriptions"] < s["pages"] * 0.8:
            issues.append("veel dubbele descriptions — schrijf per pagina uniek")
        if s["pct_single_h1"] < 100:
            issues.append(f"slechts {s['pct_single_h1']}% heeft precies 1 H1")
        if s["pct_with_jsonld"] < 50:
            issues.append("weinig structured data — rich results blijven liggen")
        if s["pct_images_missing_alt"] > 20:
            issues.append(f"{s['pct_images_missing_alt']}% afbeeldingen zonder alt-tekst")
        if s["avg_word_count"] < 300:
            issues.append(f"gemiddeld maar {s['avg_word_count']} woorden — dunne content")
        if not s["has_sitemap"]:
            issues.append("geen sitemap.xml gevonden")
        if s.get("rendered_js_pages"):
            issues.append(f"{s['rendered_js_pages']} pagina's alleen via JS-rendering leesbaar")
        lines += [f"### {s['domain']}",
                  f"- Rankt inhoudelijk op: {', '.join(s.get('top_keywords', [])[:10]) or '-'}",
                  f"- Belangrijkste combinaties: {', '.join(s.get('top_bigrams', [])[:6]) or '-'}",
                  "- Verbeterpunten: " + ("; ".join(issues) if issues else "geen grote on-page-issues gevonden"), ""]
    (out_root / "ANALYSE.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Analyserapport: {out_root / 'ANALYSE.md'}")


def main():
    ap = argparse.ArgumentParser(description="SEO-alles-scraper v%s (pro): keywords, producten, content, "
                                             "SEO-score 0-100, HTML-dashboard, robuust + snel, "
                                             "near-dup/orphan/PageRank" % __version__)
    ap.add_argument("--version", action="version", version="seo_scraper_v2 %s" % __version__)
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--delay", type=float, default=None,
                    help="pauze tussen (waves van) requests; default 1.0 (0.0 bij --fast)")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--no-render", action="store_true", help="geen Playwright-fallback")
    ap.add_argument("--out", default="output")
    ap.add_argument("--psi", action="store_true", default=False,
                    help="PageSpeed Insights aanzetten (traag; faalt zacht bij quota)")
    ap.add_argument("--psi-key", default=os.environ.get("PAGESPEED_API_KEY"),
                    help="PageSpeed API-key (default: env PAGESPEED_API_KEY)")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="aantal pagina's gelijktijdig fetchen (default 1; 8 bij --fast)")
    ap.add_argument("--fast", action="store_true",
                    help="sneller: gelijktijdig fetchen, geen images/render, lichtere link/image-checks")
    ap.add_argument("--compare", action="store_true",
                    help="concurrent-verslaan-modus: scoor meerdere sites + print wie-wint")
    ap.add_argument("--screenshots", action="store_true",
                    help="module 1.1: desktop 1440x900 + mobiel 390x844 fold/full-PNG's "
                         "+ gerenderde DOM per pagina (fase-2-nulmeting: aan)")
    ap.add_argument("--include", default=None,
                    help="alleen URL's die deze regex matchen crawlen (seed + links); "
                         "bv. NL-subset: \"site\\.nl/(?!(en|de|fr)/)\"")
    ap.add_argument("--dup-threshold", type=float, default=0.80,
                    help="Jaccard-drempel voor near-duplicate-clustering (0-1, default 0.80)")
    args = ap.parse_args()

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    log = robust.get_logger(out_root)

    # --fast / expliciete vlaggen oplossen
    concurrency = args.concurrency if args.concurrency is not None else (8 if args.fast else 1)
    delay = args.delay if args.delay is not None else (0.0 if args.fast else 1.0)
    no_images = args.no_images or args.fast
    no_render = args.no_render or args.fast
    mlc, mic, cdelay = (12, 10, 0.0) if args.fast else (40, 30, 0.25)

    renderer = None if no_render else Renderer()
    summaries, scrapers = [], []
    try:
        for url in args.urls:
            s = SiteScraper(url, max_pages=args.max_pages, delay=delay,
                            download_images=not no_images, out_root=out_root,
                            renderer=renderer, psi=args.psi, psi_key=args.psi_key,
                            concurrency=concurrency, fast=args.fast,
                            dup_threshold=args.dup_threshold, logger=log,
                            max_link_checks=mlc, max_image_checks=mic, check_delay=cdelay,
                            screenshots=args.screenshots, include=args.include)
            summaries.append(s.crawl())
            scrapers.append(s)
    finally:
        if renderer:
            renderer.close()

    (out_root / "summaries.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    write_compare(summaries, out_root)
    write_analyse(summaries, out_root)
    write_coverage(scrapers, summaries, out_root, psi_enabled=args.psi)

    # ---- concurrent-verslaan: wie wint per signaal? ------------------------
    live = [(sc, su) for sc, su in zip(scrapers, summaries) if su.get("pages")]
    compare = advanced_analysis.compare_sites(summaries)
    if len(live) >= 2:
        (out_root / "compare.json").write_text(
            json.dumps(compare, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.compare and compare.get("comparable"):
            print(f"\n=== Concurrent verslaan -> eindwinnaar: {compare.get('champion')} ===")
            for r in compare.get("ranking", []):
                print(f"   {r['domain']}: {r['signals_won']} signalen gewonnen")

    # ---- headline score.json (alle sites) ----------------------------------
    (out_root / "score.json").write_text(json.dumps({
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "psi_enabled": bool(args.psi),
        "site_scores": {su.get("domain"): (su.get("seo") or {}).get("site_score") for su in summaries},
        "sites": {su.get("domain"): su.get("seo") for su in summaries},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- HTML-dashboard ----------------------------------------------------
    report_data = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "psi": bool(args.psi),
        "compare": compare,
        "sites": [{"domain": sc.domain, "summary": su, "analysis": sc.analysis, "pages": sc.pages}
                  for sc, su in live],
    }
    if report_data["sites"]:
        try:
            report_mod.write_report(report_data, out_root / "report.html")
            print(f"\nHTML-dashboard: {out_root / 'report.html'}")
        except Exception as e:
            log.warning("report.html schrijven faalde: %s", e)
    print(f"SEO-scores: " + ", ".join(
        f"{su.get('domain')}={ (su.get('seo') or {}).get('site_score') }"
        f"({(su.get('seo') or {}).get('site_grade','?')})" for su in summaries))


if __name__ == "__main__":
    sys.exit(main())
