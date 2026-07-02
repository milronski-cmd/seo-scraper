# -*- coding: utf-8 -*-
"""
Helpers voor module 1.6 — Doelgroep-lens (audits/audience.py).

Alles hier is PUUR en FAIL-SOFT: geen netwerk, geen exceptions naar buiten,
geen import van andere audit-modules. Twee soorten helpers:

  1. config-resolutie  -> resolve_audience(ctx)  (env -> shared -> builtin -> generiek)
  2. detectoren        -> lezen render_meta.json (module 1.1) en/of dom.html
                          (BeautifulSoup) en/of page-record-velden, en geven een
                          gestandaardiseerd meet-resultaat terug.

Meet-resultaat (dict) dat elke detector-wrapper teruggeeft:
    {"measurable": bool,   # False => check is 'onmeetbaar' (telt niet in de score)
     "frac": float|None,   # 0..1 aandeel geslaagd (elementen of pagina's)
     "n": int,             # noemer (aantal beoordeelde pagina's/elementen)
     "n_pass": int,        # teller
     "meting": str,        # korte, menselijke meting-omschrijving (NL)
     "example_url": str}   # voorbeeld van een gefaalde/relevante pagina
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

# ---- optionele dependency: BeautifulSoup (fail-soft) ------------------------
try:
    from bs4 import BeautifulSoup as _BS
except Exception:                                    # pragma: no cover
    _BS = None

# =============================================================================
# Constanten
# =============================================================================
FOLD_DESKTOP_PX = 900          # 1440x900 viewport (module 1.1)
FOLD_MOBILE_PX = 844           # 390x844 viewport (module 1.1)
HEADER_TEL_Y_MAX = 160         # 'header-regio' desktop (render-pad)

_BODY_TAGS = {"p", "li", "dd", "dt", "td", "blockquote", "figcaption"}
_CTA_TAGS = {"a", "button"}
_CTA_CLASS_RE = re.compile(r"(?:^|[-_ ])(?:btn|button|cta|knop|buy|order|bestel|koop|"
                           r"add-to-cart|toevoegen|checkout|afrekenen)", re.I)
_HEADER_HINT_RE = re.compile(r"header|nav|topbar|top-bar|utilbar|masthead|menu|"
                             r"site-head|siteheader|kop", re.I)

# tekst-signalen (NL, doelgroep-lens)
RE_COUNTDOWN = re.compile(
    r"countdown|aftell|afteller|nog\s+\d+\s*(?:uur|uren|min|minuten|seconden|dagen)"
    r"|laatste kans|op\s*=\s*op|bijna uitverkocht|nog maar \d+|verloopt (?:over|in)"
    r"|eindigt (?:over|in)|only \d+ left|\d+\s*left|actie (?:eindigt|verloopt)"
    r"|tijdelijk aanbod eindigt", re.I)
RE_HELP = re.compile(
    r"keuzehulp|keuze hulp|keuzewijzer|veelgestelde|veel gestelde|\bfaq\b|hulp bij"
    r"|adviesgesprek|persoonlijk advies|gratis advies|hulp nodig|welke .{0,30}past"
    r"|vind (?:uw|je|jouw) |stel uw vraag|bel voor advies", re.I)
RE_COMPARE = re.compile(r"vergelijk|vergelijken|compare|naast elkaar|specs? vergelijk"
                        r"|zet .{0,20}naast elkaar", re.I)
RE_FIT = re.compile(
    r"past op|past bij|geschikt voor|compatibel|compatibiliteit|welke .{0,25}past"
    r"|kies (?:uw|je|jouw) model|selecteer (?:uw|je|jouw) model|vind (?:uw|je|jouw) model"
    r"|zoek op model|onderdelen voor|reserveonderdelen voor", re.I)
RE_STOCK = re.compile(
    r"op voorraad|niet op voorraad|uitverkocht|leverbaar|levertijd|direct leverbaar"
    r"|voorraad\s*[:=]|\d+\s*op voorraad|nog \d+ (?:stuks|beschikbaar)|"
    r"binnen \d+ (?:werk)?dagen (?:in huis|geleverd|bezorgd)", re.I)
RE_DELIVERY = re.compile(r"levertijd|verzend|bezorg|geleverd|verzonden|vandaag besteld"
                         r"|morgen (?:in huis|bezorgd)|binnen \d+ (?:werk)?dagen", re.I)
RE_LEGAL = re.compile(r"\brdw\b|legaal|wettelijk toegestaan|verzeker|kenteken|helmplicht"
                      r"|wegenverkeer|toegestaan op de (?:weg|openbare)|25\s?km|"
                      r"typegoedkeuring|rijbewijs", re.I)
RE_PRICE = re.compile(r"€\s?\d|\bvanaf\s*€|\b\d{1,4}[.,]\d{2}\b|\b\d{1,3}\.\d{3}\b")
RE_SPEC_UNIT = re.compile(
    r"\b\d+([.,]\d+)?\s?(km/?h|km|kmu|kg|kilo|watt|\bw\b|\bv\b|volt|ah|wh|cm|mm|nm|"
    r"°|graden|pk|liter|\bl\b|inch|\"|kmh|newton)", re.I)
RE_SEARCH_HINT = re.compile(r"zoek|search|find", re.I)
RE_MODEL_HINT = re.compile(r"\bmerk\b|\bmodel\b|\btype\b|\bmerken\b|kies (?:je|uw) "
                           r"|filter op|verfijn", re.I)

# domein-relevantie voor 'legaliteit' (e-step / scooter / (fat)bike-achtig)
_MOBILITY_TOKENS = ("step", "scooter", "escooter", "e-step", "estep", "bike", "fiets",
                    "fatbike", "elektrische", "brommer", "moped", "scootmobiel", "wheel")


# =============================================================================
# Domein-normalisatie
# =============================================================================
def normalize_domain(s):
    """'https://www.Foo.nl/pad' -> 'foo.nl'. Fail-soft: geeft altijd een str."""
    if not s:
        return ""
    s = str(s).strip().lower()
    if "://" in s:
        try:
            s = urlparse(s).netloc or s.split("://", 1)[1]
        except Exception:
            s = s.split("://", 1)[1]
    s = s.split("/")[0].strip()
    if s.startswith("www."):
        s = s[4:]
    return s.strip(". ")


# =============================================================================
# 1. Config-resolutie:  env -> shared -> builtin -> generiek
# =============================================================================
SHARED_TARGETS_PATH = r"C:\ClaudeAgents\shared\scraper-targets.json"

# ingebouwde defaults op domein-match (volgorde = eerste hit wint)
_BUILTIN_RULES = [
    (("zekermobiel",), "senioren"),
    (("movevolt", "voltway"), "forenzen"),
    (("rideparts", "fatparts"), "sleutelaars"),
    (("gofatbike", "fatbikeskopen", "fatbikesbrabant"), "sleutelaars"),
    (("mangomobility", "medipoint", "fastfuriousscooters"), "senioren"),
    (("nr1elektrischestep", "2wheels", "easy-ride", "easyride"), "forenzen"),
]
_VALID_PROFILES = {"senioren", "forenzen", "sleutelaars", "generiek"}


def _load_targets_json(path):
    """Lees een targets-JSON fail-soft; geef list-of-properties terug of None."""
    try:
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        props = data.get("properties")
    elif isinstance(data, list):
        props = data
    else:
        props = None
    return props if isinstance(props, list) else None


def _audience_block_for(props, domain):
    """Zoek in een properties-list het 'audience'-blok voor dit domein.
    Geeft (profiel, overrides) of None als er geen bruikbaar blok is."""
    if not props:
        return None
    for entry in props:
        if not isinstance(entry, dict):
            continue
        dom = normalize_domain(entry.get("domein") or entry.get("domain") or "")
        if dom and dom == domain:
            aud = entry.get("audience")
            if isinstance(aud, dict):
                prof = str(aud.get("profiel") or aud.get("profile") or "").strip().lower()
                ov = aud.get("overrides")
                ov = ov if isinstance(ov, dict) else {}
                if prof in _VALID_PROFILES:
                    return prof, ov
            # property gevonden maar geen (geldig) audience-blok -> door naar volgende bron
            return None
    return None


def _builtin_profile(domain):
    """Domein-match op ingebouwde defaults, incl. 'scootmobiel*'-wildcard."""
    for tokens, prof in _BUILTIN_RULES:
        if any(tok in domain for tok in tokens):
            return prof
    if "scootmobiel" in domain:          # 'scootmobiel*'
        return "senioren"
    return None


def resolve_audience(ctx):
    """Bepaal (profiel, config_bron, overrides, note) via de vaste volgorde:

        1. env SCRAPER_TARGETS_PATH  (JSON met 'properties' + audience-blok)
        2. vast pad shared\\scraper-targets.json (audience-blok mag ontbreken)
        3. ingebouwde defaults op domein-match
        4. 'generiek'

    Alles fail-soft: een kapotte/ontbrekende bron degradeert naar de volgende.
    config_bron in {"env", "shared", "builtin", "generiek"}.
    """
    domain = normalize_domain(ctx.get("domain") or "")
    notes = []

    # 1. env-pad
    env_path = os.environ.get("SCRAPER_TARGETS_PATH")
    if env_path:
        props = _load_targets_json(env_path)
        if props is None:
            notes.append("SCRAPER_TARGETS_PATH gezet maar niet leesbaar/geldig; genegeerd.")
        else:
            hit = _audience_block_for(props, domain)
            if hit:
                return hit[0], "env", hit[1], "; ".join(notes)

    # 2. vast shared-pad
    props = _load_targets_json(SHARED_TARGETS_PATH)
    if props is not None:
        hit = _audience_block_for(props, domain)
        if hit:
            return hit[0], "shared", hit[1], "; ".join(notes)

    # 3. ingebouwde defaults
    prof = _builtin_profile(domain)
    if prof:
        return prof, "builtin", {}, "; ".join(notes)

    # 4. fallback
    return "generiek", "generiek", {}, "; ".join(notes)


# =============================================================================
# 2a. Laders (render_meta / dom.html / paginatekst)
# =============================================================================
def load_render(ctx, page):
    """Lees render_meta.json van deze pagina (module 1.1). None als afwezig/kapot."""
    sc = page.get("screenshots") or {}
    rel = sc.get("render_meta")
    out = ctx.get("out")
    if not rel or not out:
        return None
    try:
        p = Path(out) / rel
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_dom(ctx, page):
    """Lees dom.html en geef (soup_or_None, raw_html_or_None). Fail-soft.
    soup is None als BeautifulSoup ontbreekt; raw blijft dan bruikbaar voor regex."""
    sc = page.get("screenshots") or {}
    rel = sc.get("dom")
    out = ctx.get("out")
    raw = None
    if rel and out:
        try:
            p = Path(out) / rel
            if p.exists():
                raw = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            raw = None
    soup = None
    if raw and _BS is not None:
        for parser in ("lxml", "html.parser"):
            try:
                soup = _BS(raw, parser)
                break
            except Exception:
                soup = None
    return soup, raw


def page_text(ctx, page):
    """Volledige (lowercased) paginatekst uit ctx['page_texts']; '' als leeg."""
    try:
        t = (ctx.get("page_texts") or {}).get(page.get("url"))
        if t:
            return t.lower()
    except Exception:
        pass
    # fallback: titel + meta + headings uit de page-record
    bits = [page.get("title") or "", page.get("meta_description") or ""]
    h = page.get("headings") or {}
    if isinstance(h, dict):
        for v in h.values():
            if isinstance(v, list):
                bits.extend(str(x) for x in v)
    return " ".join(bits).lower()


# =============================================================================
# 2b. Pagina-classificatie
# =============================================================================
_RE_PDP = re.compile(r"/(?:product|producten/[^/]+|p|products|model|item|artikel)"
                     r"(?:/|-|$)", re.I)
_RE_PLP = re.compile(r"/(?:collectie|collections?|categorie|category|producten/?$|"
                     r"webshop|shop|assortiment|merk|merken|catalog|aanbod)(?:/|$)", re.I)
_RE_CHECKOUT = re.compile(r"/(?:checkout|cart|winkelwagen|winkelmand|afrekenen|kassa|"
                          r"bestellen|bestelling)(?:/|$)", re.I)
_RE_CONTACT = re.compile(r"/(?:contact|klantenservice)(?:\.|/|$)", re.I)
_RE_GUIDE = re.compile(r"/(?:gids|blog|advies|kennisbank|uitleg|inspiratie|nieuws|"
                       r"handleiding|magazine)(?:/|-|$)", re.I)


def classify_page(ctx, page, soup):
    """Geef een set tags: {'home','pdp','plp','checkout','contact','guide','other'}."""
    tags = set()
    url = page.get("url") or ""
    try:
        path = urlparse(url).path or "/"
    except Exception:
        path = "/"

    # PDP-signalen (JSON-LD / producten / url)
    jt = page.get("jsonld_types") or []
    jt_low = {str(x).lower() for x in jt} if isinstance(jt, list) else set()
    is_pdp = False
    if "product" in jt_low:
        is_pdp = True
    if (page.get("products_found") or 0) >= 1 and ("offer" in jt_low or "product" in jt_low):
        is_pdp = True
    if _RE_PDP.search(path):
        is_pdp = True

    if path in ("", "/"):
        tags.add("home")
    if _RE_CHECKOUT.search(path):
        tags.add("checkout")
    if _RE_CONTACT.search(url) or _RE_CONTACT.search(path):
        tags.add("contact")
    if _RE_GUIDE.search(path):
        tags.add("guide")
    if _RE_PLP.search(path):
        tags.add("plp")
    if is_pdp and "checkout" not in tags:
        tags.add("pdp")
    if not tags:
        tags.add("other")
    return tags


# =============================================================================
# 2c. DOM-detectoren (presence / structuur)
# =============================================================================
def _ancestors_hint(el, pattern):
    """True als een voorouder (tag header/nav of class/id op pattern) matcht."""
    node = el
    depth = 0
    while node is not None and depth < 8:
        try:
            name = getattr(node, "name", None)
            if name in ("header", "nav"):
                return True
            if hasattr(node, "get"):
                cls = " ".join(node.get("class", []) or [])
                nid = node.get("id", "") or ""
                if cls and pattern.search(cls):
                    return True
                if nid and pattern.search(nid):
                    return True
        except Exception:
            pass
        node = getattr(node, "parent", None)
        depth += 1
    return False


def tel_in_header(soup, raw):
    """(bool_or_None, aantal_tel_links): klikbaar tel:-nummer in header/nav-regio?"""
    if soup is not None:
        try:
            links = soup.select('a[href^="tel:"]')
            for a in links:
                if _ancestors_hint(a, _HEADER_HINT_RE):
                    return True, len(links)
            return False, len(links)
        except Exception:
            pass
    if raw:
        head = raw[:4000]
        n = len(re.findall(r'href="tel:', raw, re.I))
        if re.search(r'href="tel:', head, re.I):
            return True, n
        return False, n
    return None, 0


def has_clickable_tel(soup, raw):
    """True als er ergens een klikbaar tel:-nummer op de pagina staat."""
    if soup is not None:
        try:
            return len(soup.select('a[href^="tel:"]')) > 0
        except Exception:
            pass
    return bool(raw and re.search(r'href="tel:', raw, re.I))


_SUBMITTY = {"submit", "button", "hidden", "image", "reset"}


def count_visible_form_fields(soup):
    """Aantal zichtbare invulvelden per <form> (list[int]). None als geen soup.
    Sluit submit/hidden/reset uit; select/textarea tellen mee."""
    if soup is None:
        return None
    out = []
    try:
        for form in soup.find_all("form"):
            n = 0
            for inp in form.find_all("input"):
                t = (inp.get("type") or "text").lower()
                if t in _SUBMITTY or inp.has_attr("hidden"):
                    continue
                style = (inp.get("style") or "").lower().replace(" ", "")
                if "display:none" in style or "visibility:hidden" in style:
                    continue
                n += 1
            n += len(form.find_all("select"))
            n += len(form.find_all("textarea"))
            out.append(n)
    except Exception:
        return None
    return out


def has_search(soup, raw):
    """Zoekfunctie aanwezig (input[type=search] / role=search / zoek-class)."""
    if soup is not None:
        try:
            if soup.select('input[type="search"]') or soup.select('[role="search"]'):
                return True
            for inp in soup.find_all("input"):
                blob = " ".join([inp.get("name", ""), inp.get("id", ""),
                                 " ".join(inp.get("class", []) or []),
                                 inp.get("placeholder", "")]).lower()
                if RE_SEARCH_HINT.search(blob):
                    return True
            for el in soup.find_all(class_=True):
                cls = " ".join(el.get("class", []) or [])
                if re.search(r"(?:^|[-_ ])search|zoek(?:balk|form|veld)?", cls, re.I):
                    return True
            return False
        except Exception:
            pass
    if raw:
        return bool(re.search(r'type="search"|role="search"|class="[^"]*(?:search|zoek)', raw, re.I))
    return None


def has_clear_nav(soup, page):
    """Duidelijke navigatie: <nav> met >=3 links, of >=5 interne links (page-record)."""
    if soup is not None:
        try:
            for nav in soup.find_all("nav"):
                if len(nav.find_all("a")) >= 3:
                    return True
        except Exception:
            pass
    return len(page.get("internal_links") or []) >= 5


def find_text_signal(regex, soup, text, nav_only=False):
    """True als het regex-signaal in de (nav-)tekst of in class/id voorkomt."""
    if nav_only and soup is not None:
        try:
            navblob = " ".join(n.get_text(" ", strip=True)
                               for n in soup.find_all(["nav", "header"]))
            if navblob and regex.search(navblob):
                return True
        except Exception:
            pass
    if text and regex.search(text):
        return True
    if soup is not None:
        try:
            for el in soup.find_all(class_=True):
                if regex.search(" ".join(el.get("class", []) or [])):
                    return True
        except Exception:
            pass
    return False


def has_spec_block(soup, text):
    """Spec-tabel/-blok: <table>/<dl> met >=3 rijen, of >=4 spec-achtige regels."""
    if soup is not None:
        try:
            for tbl in soup.find_all("table"):
                if len(tbl.find_all("tr")) >= 3:
                    return True
            for dl in soup.find_all("dl"):
                if len(dl.find_all("dt")) >= 3:
                    return True
            for el in soup.find_all(class_=True):
                if re.search(r"spec(?:s|ificat)", " ".join(el.get("class", []) or []), re.I):
                    return True
        except Exception:
            pass
    return bool(text and len(RE_SPEC_UNIT.findall(text)) >= 4)


def has_model_filter(soup, text):
    """Model-/merk-filter of -navigatie op een PLP."""
    if soup is not None:
        try:
            for el in soup.find_all(class_=True):
                if re.search(r"filter|facet|refine|verfijn", " ".join(el.get("class", []) or []), re.I):
                    return True
            for sel in soup.find_all("select"):
                blob = " ".join([sel.get("name", ""), sel.get("id", ""),
                                 " ".join(sel.get("class", []) or [])]).lower()
                if RE_MODEL_HINT.search(blob):
                    return True
        except Exception:
            pass
    return bool(text and RE_MODEL_HINT.search(text) and ("filter" in text or "verfijn" in text))


_MOBILITY_CONTENT_TERMS = (
    "elektrische step", "e-step", "e step", "escooter", "e-scooter", "elektrische scooter",
    "scootmobiel", "fatbike", "fat bike", "e-bike", "elektrische fiets", "speed pedelec",
    "snorfiets", "bromfiets", "brommer", "step", "scooter", "moped")


def is_mobility_domain(ctx):
    """True als de site e-step/scooter/(fat)bike-achtig is (voor de legaliteit-check).
    Kijkt eerst naar het domein (merknamen zoals 'movevolt' bevatten geen categorie-
    woord) en anders naar de site-inhoud: paginateksten, producten en headings."""
    dom = normalize_domain(ctx.get("domain") or "")
    if any(tok in dom for tok in _MOBILITY_TOKENS):
        return True
    hay = []
    try:
        for i, t in enumerate((ctx.get("page_texts") or {}).values()):
            if i >= 8:
                break
            if t:
                hay.append(str(t).lower()[:5000])
    except Exception:
        pass
    try:
        for pr in (ctx.get("products") or [])[:25]:
            if isinstance(pr, dict):
                hay.append(str(pr.get("name", "")).lower())
                hay.append(str(pr.get("category", "") or pr.get("type", "")).lower())
    except Exception:
        pass
    blob = " ".join(hay)
    if not blob:
        return False
    hits = sum(1 for term in _MOBILITY_CONTENT_TERMS if term in blob)
    return hits >= 2


# =============================================================================
# 2d. RENDER-detectoren (render_meta.json)
# =============================================================================
def _fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _viewport(render, vp):
    return ((render or {}).get("viewports") or {}).get(vp) or {}


def median_body_font(render):
    """Mediane fontSize (px) van body-achtige teksten (len>=40). (val,n) of (None,0).
    Voorkeur desktop; anders mobiel."""
    for vp in ("desktop", "mobile"):
        texts = _viewport(render, vp).get("texts") or []
        sizes = []
        for t in texts:
            tag = (t.get("tag") or "").lower()
            txt = (t.get("text") or "")
            fs = _fnum(t.get("fontSize"))
            w = _fnum(t.get("w")); h = _fnum(t.get("h"))
            if fs <= 0 or w <= 1 or h <= 1:
                continue
            if tag in _BODY_TAGS and len(txt) >= 40:
                sizes.append(fs)
        if sizes:
            sizes.sort()
            n = len(sizes)
            med = sizes[n // 2] if n % 2 else (sizes[n // 2 - 1] + sizes[n // 2]) / 2.0
            return round(med, 1), n
    return None, 0


def _is_cta(t):
    tag = (t.get("tag") or "").lower()
    sel = t.get("selector") or ""
    if tag == "button":
        return True
    if tag in _CTA_TAGS and _CTA_CLASS_RE.search(sel):
        return True
    return False


def cta_heights(render):
    """Hoogtes (px) van CTA-achtige elementen over beide viewports. list[float]."""
    hs = []
    for vp in ("desktop", "mobile"):
        for t in _viewport(render, vp).get("texts") or []:
            if _is_cta(t):
                h = _fnum(t.get("h"))
                if h > 1:
                    hs.append(h)
    return hs


def cta_in_fold(render, vp, fold_px):
    """(found_bool, has_viewport_bool): staat er >=1 CTA-element in de fold?"""
    v = _viewport(render, vp)
    texts = v.get("texts") or []
    if not texts:
        return False, False
    for t in texts:
        if _is_cta(t):
            y = _fnum(t.get("y"), 1e9)
            h = _fnum(t.get("h"))
            if 0 <= y < fold_px and h > 1:
                return True, True
    return False, True


def price_and_spec_in_fold(render, vp, fold_px):
    """Op een PDP: staan prijs EN >=1 spec in de fold? (bool, has_viewport)."""
    v = _viewport(render, vp)
    texts = v.get("texts") or []
    if not texts:
        return False, False
    price = spec = False
    for t in texts:
        y = _fnum(t.get("y"), 1e9)
        if not (0 <= y < fold_px):
            continue
        txt = t.get("text") or ""
        if RE_PRICE.search(txt):
            price = True
        if RE_SPEC_UNIT.search(txt):
            spec = True
    return (price and spec), True
