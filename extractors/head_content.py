#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extractor-module: HEAD/META (categorie 1) + CONTENT/leesbaarheid (categorie 2).

Standalone "plug-in" voor seo_scraper_v2.py. Een finalizer wired deze later in
de hoofd-scraper; dit bestand wijzigt de scraper NIET en draait ook los.

CONTRACT
--------
    def extract(ctx) -> dict

`ctx` is een dict met:
    ctx['url']       eind-URL (str, na redirects)
    ctx['html']      rauwe HTML (str)
    ctx['soup']      BeautifulSoup (lxml-geparsed) — READ-ONLY gebruiken
    ctx['resp']      requests.Response (.status_code/.headers/.elapsed/.history/.url)
    ctx['base_url']  scheme://host
    ctx['session']   requests.Session (voor eventuele extra calls)
    ctx['rendered']  bool (kwam de HTML uit Playwright?)

extract() geeft een PLATTE dict met UITSLUITEND NIEUWE velden terug. Elk veld is
afgeschermd met try/except: bij een fout wordt None/[]/{} teruggegeven en de module
crasht NOOIT. Velden die de hoofd-scraper al levert (title, meta_description,
meta_keywords, meta_robots, viewport, canonical, hreflang, og, twitter, lang,
headings, h1_count, word_count, keywords/densiteit, breadcrumbs) worden NIET
gedupliceerd.

Afhankelijkheden: alleen stdlib + bs4 + requests. `textstat`/`langdetect` worden
ALLEEN gebruikt als ze al geïnstalleerd zijn; anders draait een eigen, simpele
implementatie (er wordt niets ge-pip-installeerd).
"""
import hashlib
import json
import re
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Optionele libs: alleen benutten als ze al aanwezig zijn (nooit installeren).
try:
    import langdetect as _langdetect          # noqa: F401
    _HAS_LANGDETECT = True
except Exception:
    _HAS_LANGDETECT = False
try:
    import textstat as _textstat               # noqa: F401
    _HAS_TEXTSTAT = True
except Exception:
    _HAS_TEXTSTAT = False


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "nl,en;q=0.8",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# Woord-token: letter/cijfer-start (incl. accenten), daarna letters/cijfers/'/-.
WORD_RE = re.compile(r"[a-zà-ÿ0-9][a-zà-ÿ0-9'\-]{1,}", re.I)

# Klinkergroepen voor de syllabe-heuristiek (NL+EN+DE-accenten meegerekend).
_VOWELS = set("aeiouyàáâãäåèéêëìíîïòóôõöøùúûüýÿ")

# Lorem-ipsum / blindtekst-detectie.
_PLACEHOLDER_MARKERS = (
    "lorem ipsum", "lorem", "dolor sit amet", "consectetur adipiscing",
    "your text here", "your text", "insert text here", "enter text here",
    "dummy text", "sample text", "placeholder text", "voorbeeldtekst hier",
)

# Compacte stopwoord-sets per taal voor de eigen taal-heuristiek.
_LANG_STOP = {
    "nl": {"de", "het", "een", "en", "van", "ik", "je", "dat", "die", "niet", "met",
           "is", "op", "te", "voor", "aan", "er", "om", "ook", "maar", "naar", "zijn",
           "was", "heeft", "wordt", "deze", "bij", "of", "als", "uit", "over", "wij",
           "hebben", "kan", "worden", "geen", "meer", "onze", "ze", "dit"},
    "en": {"the", "and", "of", "to", "a", "in", "is", "that", "it", "for", "was",
           "on", "are", "with", "as", "be", "at", "this", "have", "from", "or", "an",
           "they", "you", "not", "but", "what", "all", "were", "when", "we", "your",
           "can", "more", "has", "our", "their", "which"},
    "de": {"der", "die", "und", "den", "von", "zu", "das", "mit", "sich", "des",
           "auf", "für", "ist", "im", "dem", "nicht", "ein", "eine", "als", "auch",
           "es", "an", "werden", "aus", "er", "hat", "dass", "sie", "nach", "wird",
           "bei", "einen", "wir", "oder", "haben", "sind", "über", "einer"},
}

# Helvetica/Arial-tekenbreedtes (AFM, eenheden per 1000 em) voor SERP-pixelschatting.
# Arial is metrisch ~gelijk aan Helvetica; onbekende tekens => 556 (gem. kleine letter).
_AFM = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191,
    "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556, "7": 556,
    "8": 556, "9": 556, ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556,
    "@": 1015, "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722, "O": 778,
    "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944,
    "X": 667, "Y": 667, "Z": 611, "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556,
    "`": 333, "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556,
    "h": 556, "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556,
    "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556, "v": 500, "w": 722,
    "x": 500, "y": 500, "z": 500, "{": 334, "|": 260, "}": 334, "~": 584,
}

# Directives die zelf een ':' bevatten (mogen NIET als bot-prefix worden gestript).
_KV_DIRECTIVES = ("max-snippet", "max-image-preview", "max-video-preview",
                  "unavailable_after", "unavailable-after")

# Drempels voor SERP-truncatie (px). Title ~20px Arial, description ~14px Arial.
_TITLE_PX_FONT = 20
_TITLE_PX_LIMIT = 580
_DESC_PX_FONT = 14
_DESC_PX_LIMIT = 920

# Lege standaardwaarden — vooraf ingevuld zodat een veld nooit ontbreekt bij fout.
_DEFAULTS = {
    # --- categorie 1: head / meta ---
    "charset": None,
    "theme_color": None,
    "msapplication_tilecolor": None,
    "apple_touch_icon": None,
    "doctype": None,                      # bool: is het een HTML5-doctype?
    "meta_author": None,
    "meta_refresh": None,
    "x_robots_tag": None,
    "robots_directives": {},
    "title_pixel_width": None,
    "title_truncation_risk": None,
    "description_pixel_width": None,
    "description_truncation_risk": None,
    "description_length_issue": None,
    "title_equals_h1": None,
    # --- categorie 2: content / leesbaarheid ---
    "heading_issues": {},
    "text_html_ratio": None,
    "avg_words_per_sentence": None,
    "readability_flesch": None,
    "readability_class": None,
    "language_detected": None,
    "language_matches_attr": None,
    "content_md5": None,
    "normalized_text_length": None,
    "thin_content": None,
    "placeholder_content": None,
    "publish_date": None,
    "modified_date": None,
}


# --------------------------------------------------------------------------- #
# Kleine, herbruikbare helpers
# --------------------------------------------------------------------------- #
def _norm(s):
    """Lowercase + whitespace genormaliseerd + getrimd."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _meta_name(soup, name):
    """content van <meta name="..."> (case-insensitief) of None."""
    try:
        m = soup.find("meta", attrs={"name": re.compile(rf"^{re.escape(name)}$", re.I)})
        if m is not None:
            c = m.get("content")
            if c is not None:
                c = c.strip()
                return c or None
    except Exception:
        pass
    return None


def _meta_prop(soup, prop):
    """content van <meta property="..."> of (fallback) <meta name="..."> of None."""
    try:
        for attr in ("property", "name"):
            m = soup.find("meta", attrs={attr: re.compile(rf"^{re.escape(prop)}$", re.I)})
            if m is not None:
                c = m.get("content")
                if c and c.strip():
                    return c.strip()
    except Exception:
        pass
    return None


def _pixel_width(text, font_px):
    """Schat de gerenderde breedte (px) van `text` in Arial op `font_px`."""
    if not text:
        return 0
    total = 0
    for ch in text:
        total += _AFM.get(ch, 556)
    return int(round(total / 1000.0 * font_px))


def _count_syllables(word):
    """Heuristische syllabe-telling: aantal klinkergroepen, minimaal 1."""
    count, prev_vowel = 0, False
    for ch in word.lower():
        is_v = ch in _VOWELS
        if is_v and not prev_vowel:
            count += 1
        prev_vowel = is_v
    return max(1, count)


def _readability_class(score):
    """Map Flesch Reading Ease -> Nederlandstalige klasse."""
    if score is None:
        return None
    if score >= 90:
        return "zeer makkelijk"
    if score >= 80:
        return "makkelijk"
    if score >= 70:
        return "vrij makkelijk"
    if score >= 60:
        return "standaard"
    if score >= 50:
        return "vrij moeilijk"
    if score >= 30:
        return "moeilijk"
    return "zeer moeilijk"


def _readability(text, lang):
    """Flesch Reading Ease. NL -> Flesch-Douma, anders -> klassieke Flesch.
    Geeft (score, klasse); (None, None) bij te weinig tekst."""
    words = WORD_RE.findall(text or "")
    n_words = len(words)
    if n_words < 30:
        return None, None
    # textstat alleen voor niet-NL gebruiken (NL heeft de Douma-variant nodig).
    if lang != "nl" and _HAS_TEXTSTAT:
        try:
            score = round(float(_textstat.flesch_reading_ease(text)), 1)
            return score, _readability_class(score)
        except Exception:
            pass
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    n_sent = max(1, len(sentences))
    syllables = sum(_count_syllables(w) for w in words)
    asl = n_words / n_sent          # gemiddelde zinslengte (woorden/zin)
    asw = syllables / n_words       # gemiddelde syllaben per woord
    if lang == "nl":
        # Flesch-Douma (Douma, 1960), Nederlandse ijking.
        score = 206.84 - 0.93 * asl - 77.0 * asw
    else:
        # Klassieke Flesch Reading Ease (Engels).
        score = 206.835 - 1.015 * asl - 84.6 * asw
    score = round(score, 1)
    return score, _readability_class(score)


def _detect_language(text):
    """Taaldetectie uit de tekst zelf. langdetect indien aanwezig, anders een
    NL/EN/DE-stopwoord-heuristiek. Geeft 'nl'/'en'/'de'/<iso> of None."""
    if not text:
        return None
    if _HAS_LANGDETECT:
        try:
            from langdetect import detect, DetectorFactory
            DetectorFactory.seed = 0
            code = detect(text[:4000])
            return (code or "").lower()[:2] or None
        except Exception:
            pass
    tokens = [t.lower() for t in WORD_RE.findall(text)]
    if len(tokens) < 20:
        return None
    sample = tokens[:2500]
    scores = {lang: sum(1 for t in sample if t in sw) for lang, sw in _LANG_STOP.items()}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return None
    return best


def _parse_robots_directives(meta_robots, x_robots):
    """Combineer meta-robots + X-Robots-Tag tot een dict met booleans/waarden.
    Bot-prefixes (bv. 'googlebot: noindex') worden afgepeld; KV-directives met
    een ':' (max-snippet etc.) blijven intact."""
    out = {
        "index": True, "follow": True, "noarchive": False, "nosnippet": False,
        "max_snippet": None, "max_image_preview": None, "noimageindex": False,
        "notranslate": False, "unavailable_after": None,
    }
    parts = []
    for src in (meta_robots, x_robots):
        if src:
            parts.extend(t.strip().lower() for t in str(src).split(",") if t.strip())
    for tok in parts:
        # Bot-prefix afpellen ('googlebot: noindex' -> 'noindex'), maar KV-keys ontzien.
        m = re.match(r"^([a-z0-9_-]+)\s*:\s*(.+)$", tok)
        if m and m.group(1) not in _KV_DIRECTIVES:
            tok = m.group(2).strip()
        if tok in ("noindex", "none"):
            out["index"] = False
        if tok in ("nofollow", "none"):
            out["follow"] = False
        if tok == "noarchive":
            out["noarchive"] = True
        if tok == "nosnippet":
            out["nosnippet"] = True
        if tok == "noimageindex":
            out["noimageindex"] = True
        if tok == "notranslate":
            out["notranslate"] = True
        if tok.startswith("max-snippet"):
            mm = re.search(r"(-?\d+)", tok)
            if mm:
                out["max_snippet"] = int(mm.group(1))
        if tok.startswith("max-image-preview"):
            mm = re.search(r"(none|standard|large)", tok)
            if mm:
                out["max_image_preview"] = mm.group(1)
        if tok.startswith("unavailable_after") or tok.startswith("unavailable-after"):
            out["unavailable_after"] = tok.split(":", 1)[1].strip() if ":" in tok else True
    return out


def _parse_jsonld(soup):
    """Alle <script type=...ld+json> ontleden tot Python-objecten."""
    docs = []
    try:
        for s in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
            raw = (s.string or s.get_text() or "").strip()
            if not raw:
                continue
            try:
                docs.append(json.loads(raw))
            except Exception:
                try:
                    docs.append(json.loads(raw.rstrip(";").strip()))
                except Exception:
                    pass
    except Exception:
        pass
    return docs


def _walk_jsonld(node):
    """Yield alle dicts in een (genest) JSON-LD-object, incl. @graph en lijsten."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_jsonld(v)
    elif isinstance(node, list):
        for it in node:
            yield from _walk_jsonld(it)


def _jsonld_date(docs, key):
    """Eerste niet-lege waarde van `key` (datePublished/dateModified) in de JSON-LD."""
    for d in docs:
        for n in _walk_jsonld(d):
            if isinstance(n, dict):
                val = n.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
                if isinstance(val, list) and val and isinstance(val[0], str):
                    return val[0].strip()
    return None


def _time_date(soup, kind):
    """Datum uit <time datetime="...">; `kind` is 'published' of 'modified'."""
    try:
        for t in soup.find_all("time"):
            dt = t.get("datetime")
            if not dt:
                continue
            itemprop = " ".join(t.get("itemprop") or []) if isinstance(t.get("itemprop"), list) \
                else (t.get("itemprop") or "")
            ip = itemprop.lower()
            if kind == "modified" and "modif" in ip:
                return dt.strip()
            if kind == "published" and ("publish" in ip or "datepublished" in ip or t.has_attr("pubdate")):
                return dt.strip()
        if kind == "published":
            t = soup.find("time", attrs={"datetime": True})
            if t:
                return (t.get("datetime") or "").strip() or None
    except Exception:
        pass
    return None


def _extract_texts(html):
    """Bouw (zichtbare_tekst, genormaliseerde_tekst) uit een EIGEN parse, zodat
    ctx['soup'] niet gemuteerd wordt.
      - zichtbaar    : script/style/noscript/svg/template verwijderd
      - genormaliseerd: bovendien nav/header/footer/aside weg, lowercased,
                        whitespace-genormaliseerd (voor near-duplicate / md5)."""
    try:
        vs = BeautifulSoup(html or "", "lxml")
    except Exception:
        return "", ""
    try:
        for t in vs(["script", "style", "noscript", "svg", "template"]):
            t.extract()
        visible = vs.get_text(" ", strip=True)
    except Exception:
        visible = ""
    try:
        for t in vs(["nav", "header", "footer", "aside"]):
            t.extract()
        normalized = re.sub(r"\s+", " ", vs.get_text(" ", strip=True)).strip().lower()
    except Exception:
        normalized = re.sub(r"\s+", " ", visible).strip().lower()
    return visible, normalized


def _apple_touch_icon(soup, base):
    """Absolute href van <link rel="apple-touch-icon"> (eerste treffer)."""
    try:
        for l in soup.find_all("link", href=True):
            rel = l.get("rel")
            rel_s = " ".join(rel).lower() if isinstance(rel, list) else str(rel or "").lower()
            if "apple-touch-icon" in rel_s:
                return urljoin(base, l["href"])
    except Exception:
        pass
    return None


def _is_html5_doctype(html):
    """True als de pagina exact een HTML5-doctype declareert (<!doctype html>)."""
    try:
        m = re.search(r"<!doctype\s+([^>]*)>", html or "", re.I)
        if not m:
            return False
        return m.group(1).strip().lower() == "html"
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Secties (elk levert een platte sub-dict; intern afgeschermd)
# --------------------------------------------------------------------------- #
def _section_head(soup, html, base, resp):
    out = {}
    # charset: <meta charset> -> http-equiv -> response Content-Type
    try:
        charset = None
        m = soup.find("meta", attrs={"charset": True})
        if m and m.get("charset"):
            charset = m.get("charset").strip()
        if not charset:
            m2 = soup.find("meta", attrs={"http-equiv": re.compile(r"^content-type$", re.I)})
            if m2 and m2.get("content"):
                mm = re.search(r"charset=([\w-]+)", m2["content"], re.I)
                if mm:
                    charset = mm.group(1)
        if not charset and resp is not None:
            mm = re.search(r"charset=([\w-]+)", resp.headers.get("Content-Type", ""), re.I)
            if mm:
                charset = mm.group(1)
        out["charset"] = (charset or None)
    except Exception:
        out["charset"] = None

    try:
        out["theme_color"] = _meta_name(soup, "theme-color")
    except Exception:
        out["theme_color"] = None
    try:
        out["msapplication_tilecolor"] = _meta_name(soup, "msapplication-TileColor")
    except Exception:
        out["msapplication_tilecolor"] = None
    try:
        out["meta_author"] = _meta_name(soup, "author")
    except Exception:
        out["meta_author"] = None
    try:
        m = soup.find("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)})
        out["meta_refresh"] = (m.get("content").strip() if m and m.get("content") else None)
    except Exception:
        out["meta_refresh"] = None
    try:
        out["apple_touch_icon"] = _apple_touch_icon(soup, base)
    except Exception:
        out["apple_touch_icon"] = None
    try:
        out["doctype"] = _is_html5_doctype(html)
    except Exception:
        out["doctype"] = None
    return out


def _section_robots(soup, resp):
    out = {"x_robots_tag": None, "robots_directives": {}}
    try:
        meta_robots = _meta_name(soup, "robots") or ""
    except Exception:
        meta_robots = ""
    x_robots = None
    try:
        if resp is not None:
            x_robots = resp.headers.get("X-Robots-Tag")
    except Exception:
        x_robots = None
    out["x_robots_tag"] = x_robots or None
    try:
        out["robots_directives"] = _parse_robots_directives(meta_robots, x_robots or "")
    except Exception:
        out["robots_directives"] = {}
    return out


def _section_serp(soup):
    out = {}
    try:
        title = soup.title.get_text(strip=True) if soup.title else ""
    except Exception:
        title = ""
    try:
        dm = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        desc = (dm.get("content") or "").strip() if dm and dm.get("content") else ""
    except Exception:
        desc = ""
    try:
        tw = _pixel_width(title, _TITLE_PX_FONT)
        out["title_pixel_width"] = tw
        out["title_truncation_risk"] = bool(tw > _TITLE_PX_LIMIT) if title else None
    except Exception:
        out["title_pixel_width"], out["title_truncation_risk"] = None, None
    try:
        dw = _pixel_width(desc, _DESC_PX_FONT)
        out["description_pixel_width"] = dw
        out["description_truncation_risk"] = bool(dw > _DESC_PX_LIMIT) if desc else None
        if not desc:
            out["description_length_issue"] = "ontbreekt"
        elif len(desc) < 70:
            out["description_length_issue"] = "te_kort"
        elif len(desc) > 160 or dw > _DESC_PX_LIMIT:
            out["description_length_issue"] = "te_lang"
        else:
            out["description_length_issue"] = None
    except Exception:
        out["description_pixel_width"] = None
        out["description_truncation_risk"] = None
        out["description_length_issue"] = None
    try:
        h1el = soup.find("h1")
        h1 = h1el.get_text(" ", strip=True) if h1el else ""
        out["title_equals_h1"] = bool(title and h1 and _norm(title) == _norm(h1))
    except Exception:
        out["title_equals_h1"] = None
    return out


def _section_headings(soup):
    try:
        hs = soup.find_all(re.compile(r"^h[1-6]$"))
        seq, empty, h1_texts = [], 0, []
        for h in hs:
            try:
                lvl = int(h.name[1])
            except Exception:
                continue
            txt = h.get_text(" ", strip=True)
            seq.append(lvl)
            if not txt:
                empty += 1
            if lvl == 1:
                h1_texts.append(txt)
        non_sequential, prev = False, None
        for lvl in seq:
            if prev is not None and lvl > prev + 1:
                non_sequential = True
                break
            prev = lvl
        return {"heading_issues": {
            "missing_h1": len(h1_texts) == 0,
            "multiple_h1": len(h1_texts) > 1,
            "h1_over_70": any(len(t) > 70 for t in h1_texts),
            "empty_headings_count": empty,
            "non_sequential": non_sequential,
        }}
    except Exception:
        return {"heading_issues": {}}


def _section_content(soup, html, visible_text, normalized_text):
    out = {}
    # text/HTML-ratio (zichtbare-tekst-bytes / HTML-bytes, %)
    try:
        html_bytes = len((html or "").encode("utf-8", "ignore")) or 1
        vis_bytes = len((visible_text or "").encode("utf-8", "ignore"))
        out["text_html_ratio"] = round(100.0 * vis_bytes / html_bytes, 1)
    except Exception:
        out["text_html_ratio"] = None
    # woord-/zinsstatistiek
    try:
        words = WORD_RE.findall(visible_text or "")
        wc = len(words)
        sentences = [s for s in re.split(r"[.!?]+", visible_text or "") if s.strip()]
        out["avg_words_per_sentence"] = round(wc / max(1, len(sentences)), 1) if wc else 0.0
        out["thin_content"] = bool(wc < 200)
    except Exception:
        out["avg_words_per_sentence"] = None
        out["thin_content"] = None
    # placeholder / blindtekst
    try:
        low = (visible_text or "").lower()
        out["placeholder_content"] = any(mk in low for mk in _PLACEHOLDER_MARKERS)
    except Exception:
        out["placeholder_content"] = None
    # genormaliseerde-tekst-hash voor near-duplicate-detectie
    try:
        out["normalized_text_length"] = len(normalized_text or "")
        out["content_md5"] = (hashlib.md5(normalized_text.encode("utf-8", "ignore")).hexdigest()
                              if normalized_text else None)
    except Exception:
        out["normalized_text_length"] = None
        out["content_md5"] = None
    # taaldetectie + match met lang-attribuut
    try:
        detected = _detect_language(visible_text)
        out["language_detected"] = detected
    except Exception:
        detected = None
        out["language_detected"] = None
    try:
        lang_attr = ((soup.html.get("lang") if soup.html else "") or "").strip().lower()[:2] or None
        out["language_matches_attr"] = (lang_attr == detected) if (lang_attr and detected) else None
    except Exception:
        out["language_matches_attr"] = None
    # leesbaarheid (Flesch / Flesch-Douma)
    try:
        score, klasse = _readability(visible_text, detected or "nl")
        out["readability_flesch"] = score
        out["readability_class"] = klasse
    except Exception:
        out["readability_flesch"] = None
        out["readability_class"] = None
    return out


def _section_dates(soup):
    out = {"publish_date": None, "modified_date": None}
    try:
        docs = _parse_jsonld(soup)
    except Exception:
        docs = []
    try:
        pub = (_meta_prop(soup, "article:published_time")
               or _meta_name(soup, "datePublished")
               or _meta_name(soup, "publish-date")
               or _meta_name(soup, "date")
               or _meta_name(soup, "dc.date")
               or _meta_name(soup, "dc.date.issued")
               or _jsonld_date(docs, "datePublished")
               or _time_date(soup, "published"))
        out["publish_date"] = pub or None
    except Exception:
        out["publish_date"] = None
    try:
        mod = (_meta_prop(soup, "article:modified_time")
               or _meta_prop(soup, "og:updated_time")
               or _meta_name(soup, "dateModified")
               or _meta_name(soup, "last-modified")
               or _meta_name(soup, "revised")
               or _jsonld_date(docs, "dateModified")
               or _time_date(soup, "modified"))
        out["modified_date"] = mod or None
    except Exception:
        out["modified_date"] = None
    return out


# --------------------------------------------------------------------------- #
# Publieke entry-point
# --------------------------------------------------------------------------- #
def extract(ctx):
    """Zie module-docstring. Geeft een platte dict met nieuwe velden; crasht nooit."""
    out = dict(_DEFAULTS)
    try:
        ctx = ctx or {}
        html = ctx.get("html") or ""
        soup = ctx.get("soup")
        if soup is None:
            try:
                soup = BeautifulSoup(html, "lxml")
            except Exception:
                soup = BeautifulSoup("", "lxml")
        base = ctx.get("url") or ctx.get("base_url") or ""
        resp = ctx.get("resp")

        # Tekst-varianten uit een eigen parse (muteert ctx['soup'] niet).
        visible_text, normalized_text = _extract_texts(html)

        for section in (
            lambda: _section_head(soup, html, base, resp),
            lambda: _section_robots(soup, resp),
            lambda: _section_serp(soup),
            lambda: _section_headings(soup),
            lambda: _section_content(soup, html, visible_text, normalized_text),
            lambda: _section_dates(soup),
        ):
            try:
                out.update(section() or {})
            except Exception:
                pass  # defaults blijven staan
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# Zelf-test: haal 2 echte sites op, bouw ctx, draai extract(), print JSON.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    test_urls = ["https://zekermobiel.nl/", "https://www.coolblue.nl/"]
    for u in test_urls:
        print("\n" + "=" * 72)
        print(u)
        print("=" * 72)
        try:
            sess = requests.Session()
            sess.headers.update(HEADERS)
            r = sess.get(u, timeout=10, allow_redirects=True)
            html = r.text
            soup = BeautifulSoup(html, "lxml")
            p = urlparse(r.url)
            ctx = {
                "url": r.url,
                "html": html,
                "soup": soup,
                "resp": r,
                "base_url": f"{p.scheme}://{p.netloc}",
                "session": sess,
                "rendered": False,
            }
            data = extract(ctx)
            print(f"[HTTP {r.status_code}] {len(html)} bytes HTML, "
                  f"{len(data)} velden geëxtraheerd")
            print(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"FOUT bij ophalen/extractie van {u}: {e}", file=sys.stderr)
