# -*- coding: utf-8 -*-
"""
Helpers voor de Conversie-audit (CRO) — module 1.2 (plan §4).

Bevat alleen PURE, fail-soft bouwstenen (regex-detectoren, paginatype-detectie,
fold-model, DOM-extractie, optionele PNG-leegte-check). Geen netwerk, geen
gedeelde-bestand-afhankelijkheid; imports blijven binnen stdlib + bs4 (+ Pillow
strikt optioneel en fail-soft). De orkestratie/scoring/issue-opbouw staat in cro.py.

Databron-lagen (tiers), van rijk naar arm:
  A "render_meta"  - p["screenshots"]["render_meta"] met viewports.*.texts[] die
                     x/y/w/h + fontSize/fontWeight hebben -> echte pixel-fold.
  B "rendered_dom" - p["screenshots"]["ok"] + dom.html (gerenderde DOM na JS),
                     geen computed-style-coordinaten -> fold via DOM-volgorde-benadering.
  C "bare_html"    - geen screenshots -> alleen page-record-velden + page_texts.
"""
import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constanten / drempels (geen stille caps: alles hier is expliciet en gedocumenteerd)
# ---------------------------------------------------------------------------
DESKTOP_FOLD_H = 900          # px (viewport 1440x900)
MOBILE_FOLD_H = 844           # px (viewport 390x844)
FOLD_DOM_INTERACTIVE = 14     # tier-B: CTA geldt als "boven de vouw" als hij bij de eerste N interactieve elementen zit
HERO_MSG_LIMIT = 3            # >3 concurrerende boodschappen in de heldensectie = te druk
CHECKOUT_FIELDS_MAX = 10      # >10 zichtbare velden op checkout = frictie
FORM_FIELDS_MAX = 7           # >7 velden in een formulier buiten checkout = lang
NEIGHBOURHOOD_MAX_CHARS = 1600  # tier-B: hoeveel omringende tekst rond een CTA als "beslismoment" telt
HERO_SCAN_MAX_TAGS = 160     # bovengrens heldensectie-scan als er geen h2 is (bounded, gemeld)
MAX_PNG_SAMPLE = 120         # bovengrens PNG-leegte-samples (grote crawls); overschrijding -> data.capped

# ---------------------------------------------------------------------------
# Regex-detectoren (Nederlands + gangbare betaal/merk-termen)
# ---------------------------------------------------------------------------
# Koopintentie in CTA-tekst. 'koop' met lichte guard tegen 'verkoop'/'inkoop'.
INTENT_RE = re.compile(
    r"(in\s?winkelwagen|winkelwagen|in\s?mand(?:je)?|mandje|bestel(?:len)?|(?<![a-z])koop|kopen|"
    r"afreken(?:en)?|kassa|offerte|aanvraag|aanvragen|aanvraagformulier|proefrit|reserveer|reserveren|"
    r"boek(?:en)?|toevoeg(?:en)?|bekijk|(?<![a-z])shop(?![a-z])|contact|plan(?:\s|$)|abonneer|"
    r"word\s+lid|schrijf\s+je\s+in|vraag\s+(?:aan|offerte)|neem\s+contact)",
    re.I)
# Sterke koop-CTA voor het "primaire" beslismoment op een PDP.
STRONG_BUY_RE = re.compile(
    r"(in\s?winkelwagen|winkelwagen|in\s?mand(?:je)?|mandje|bestel(?:len)?|(?<![a-z])koop|kopen|"
    r"afreken(?:en)?|toevoeg(?:en)?|reserveer|nu\s+bestellen|direct\s+bestellen)",
    re.I)
# CTA herkenbaar aan class/selector.
CTA_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(btn|button|cta|knop|buy|order|add[-_]?to[-_]?cart|addtocart|js[-_]?add|"
    r"c-button|is-primary|primary-action|action-button)(?:$|[\s_-])", re.I)

TRUST_REVIEW_RE = re.compile(
    r"(review(?:s)?|beoordeel(?:ing(?:en)?)?|sterren|★|⭐|waardering|trustpilot|kiyoh|"
    r"feedbackcompany|klanten\s+(?:geven|beoordelen|waarderen)|\d[.,]?\d?\s*/\s*(?:5|10)\b)", re.I)
TRUST_RETURN_RE = re.compile(
    r"(retour(?:neren|beleid)?|niet[-\s]?goed[,\s]+geld\s+terug|geld[-\s]?terug|garantie|"
    r"bedenktijd|\d+\s?dagen\s+(?:retour|bedenktijd|op\s+proef)|omruil(?:en|garantie)?|"
    r"niet\s+tevreden)", re.I)
TRUST_PAY_RE = re.compile(
    r"(ideal|klarna|bancontact|paypal|riverty|afterpay|achteraf\s+betalen|veilig\s+betalen|"
    r"in\s+termijnen|gespreid\s+betalen|creditcard|master\s?card|(?<![a-z])visa(?![a-z])|"
    r"betaalmethoden?|apple\s?pay)", re.I)

URGENCY_TIMER_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(countdown|count-down|timer|deal-clock|sale-end|salecountdown|scarcity|"
    r"urgency|js-timer|flashsale|flash-sale|deal-timer)(?:$|[\s_-])", re.I)
URGENCY_TEXT_RE = re.compile(
    r"(op\s?=\s?op|op\s+is\s+op|laatste\s+kans|bijna\s+uitverkocht|wees\s+er\s+snel\s+bij|"
    r"mis\s+het\s+niet|nu\s+of\s+nooit|alleen\s+(?:vandaag|nu|deze\s+week)|"
    r"verloopt\s+(?:over|binnen|vandaag)|nog\s+\d+\s+(?:uur|min(?:uten)?|dag(?:en)?)\b|"
    r"\d+\s+(?:anderen|mensen|klanten|bezoekers)\s+(?:bekijken|kijken|bekeken|hebben\s+dit)|"
    r"nog\s+(?:maar\s+)?\d+\s+(?:stuks|exemplaren|op\s+voorraad|beschikbaar|leverbaar|over)\b)",
    re.I)

ACCOUNT_REQ_RE = re.compile(
    r"(account\s+aanmaken\s+om\s+(?:te|verder)|maak\s+(?:eerst\s+)?een\s+account\s+(?:aan\s+)?om|"
    r"registreer(?:t|en)?\s+om\s+te\s+bestellen|account\s+(?:is\s+)?(?:verplicht|vereist)|"
    r"inloggen\s+(?:verplicht|vereist|nodig)\s+om|verplicht\s+(?:een\s+)?account)", re.I)

STEP_RE = re.compile(
    r"(?:^|[\s_-])(step|stepper|steps|progress-?bar|wizard|checkout-?steps|stap(?:pen)?-?indicator)"
    r"(?:$|[\s_-])", re.I)

# Prijs: EUR/euroteken gevolgd door een bedrag (of "vanaf EUR").
PRICE_RE = re.compile(r"(?:€|\bEUR\b)\s?\d[\d. \s]*(?:[.,]\d{1,2})?|\bvanaf\s+€", re.I)

BADGE_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(badge|pill|chip|eyebrow|kicker|ribbon)(?:$|[\s_-])", re.I)
# Ondersteunende voordeel-/USP-stroken: WEL nuttig, maar tellen NIET als concurrerende
# hoofdboodschap (anders vuurt "1 boodschap per viewport" op elke commerciele USP-balk).
USP_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(usp|usp-band|highlight|banner|benefit|feature-?bar|trust-?bar|"
    r"pluspunt(?:en)?|voordelen|reassurance)(?:$|[\s_-])", re.I)
BIGHEADING_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(hero__title|hero-title|display|headline|hero__heading|title--xl|title-xl|"
    r"hero__lead|hero-lead|hero__sub|hero-sub|subtitle|super-?title)(?:$|[\s_-])", re.I)
HEADER_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(header|nav|navbar|topbar|masthead|site-header|main-nav|menu-bar)(?:$|[\s_-])", re.I)
# Strengere variant voor voorouder-detectie (zonder losse 'nav', dat matcht ook 'product-nav').
HEADER_STRICT_RE = re.compile(
    r"(?:^|[\s_-])(header|navbar|topbar|masthead|site-header|main-nav|menu-bar)(?:$|[\s_-])", re.I)

_WS_RE = re.compile(r"\s+")


def norm(s):
    """Witruimte normaliseren; None -> ''."""
    if not s:
        return ""
    return _WS_RE.sub(" ", str(s)).strip()


# ---------------------------------------------------------------------------
# Paginatype
# ---------------------------------------------------------------------------
def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


def has_product_schema(p):
    try:
        blob = " ".join(str(x) for x in
                        _as_list(p.get("jsonld_types")) + _as_list(p.get("microdata_types"))).lower()
        return "product" in blob  # dekt 'Product' en 'ProductGroup'
    except Exception:
        return False


def page_type(p):
    """home | pdp | checkout | other. Checkout wint van pdp (URL-gestuurd, per spec).
    Volledig fail-soft: valt terug op 'other' bij rare input."""
    try:
        url = p.get("url") or ""
        if not url:
            return "other"
        try:
            path = urlparse(url).path or "/"
        except Exception:
            path = "/"
        low = url.lower()
        if re.search(r"(?:^|[/?&#=_-])(cart|checkout|winkel-?wagen|afreken(?:en)?|kassa)(?:$|[/?&#=_./-])", low):
            return "checkout"
        try:
            prod = (int(p.get("products_found") or 0) > 0)
        except Exception:
            prod = False
        if prod or has_product_schema(p):
            return "pdp"
        if path in ("", "/"):
            return "home"
        return "other"
    except Exception:
        return "other"


# ---------------------------------------------------------------------------
# DOM-extractie (bs4). Alle functies fail-soft: nooit raisen op rare input.
# ---------------------------------------------------------------------------
def make_soup(html):
    """BeautifulSoup met stdlib-parser; script/style/noscript/template verwijderd.
    Retour: (soup, visible_text) of (None, '') bij falen."""
    if not html:
        return None, ""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None, ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None, ""
    try:
        body = soup.body or soup
        for t in body.find_all(["script", "style", "noscript", "template"]):
            t.extract()
        text = norm(body.get_text(" ", strip=True))
    except Exception:
        text = ""
    return soup, text


def _classes(el):
    try:
        return " ".join(el.get("class") or [])
    except Exception:
        return ""


def _txt(el):
    try:
        return norm(el.get_text(" ", strip=True))
    except Exception:
        return ""


def is_cta(text, cls):
    return bool(INTENT_RE.search(text or "")) or bool(CTA_CLASS_RE.search(cls or ""))


def iter_ctas(soup):
    """Alle CTA-achtige elementen in documentvolgorde: a, button, input[submit/button], role=button.
    Retour: list van dicts {el, text, cls, intent(bool)}."""
    out = []
    if soup is None:
        return out
    body = soup.body or soup
    try:
        for el in body.find_all(["a", "button"]):
            t, c = _txt(el), _classes(el)
            out.append({"el": el, "text": t, "cls": c, "intent": is_cta(t, c)})
        for el in body.find_all("input"):
            if (el.get("type") or "").lower() in ("submit", "button"):
                t = norm(el.get("value") or "")
                c = _classes(el)
                out.append({"el": el, "text": t, "cls": c, "intent": is_cta(t, c)})
        for el in body.find_all(attrs={"role": "button"}):
            if el.name not in ("a", "button"):
                t, c = _txt(el), _classes(el)
                out.append({"el": el, "text": t, "cls": c, "intent": is_cta(t, c)})
    except Exception:
        pass
    return out


def cta_in_top_zone(soup):
    """Tier-B benadering van 'CTA boven de vouw': een koopintentie-CTA in de header/nav
    (sticky chrome, vrijwel altijd in de vouw) OF bij de eerste N interactieve elementen.
    Retour: (bool, detail-dict)."""
    ctas = iter_ctas(soup)
    if not ctas:
        return False, {"reason": "geen enkele CTA in de DOM", "header_cta": False, "top_n_cta": False}
    header_cta = False
    example = ""
    try:
        body = soup.body or soup
        headers = list(body.find_all("header"))
        headers += [e for e in body.find_all(True)
                    if e.name == "nav" or HEADER_CLASS_RE.search(_classes(e))]
        for h in headers:
            for el in h.find_all(["a", "button"]):
                t, c = _txt(el), _classes(el)
                if is_cta(t, c):
                    header_cta = True
                    example = t or c
                    break
            if header_cta:
                break
    except Exception:
        pass
    first_n = ctas[:FOLD_DOM_INTERACTIVE]
    top_n_cta = any(c["intent"] for c in first_n)
    if not example:
        for c in first_n:
            if c["intent"]:
                example = c["text"] or c["cls"]
                break
    return (header_cta or top_n_cta), {
        "header_cta": header_cta, "top_n_cta": top_n_cta,
        "example": example[:60], "total_ctas": len(ctas),
    }


def _find_hero(body):
    """Vind het hero-blok: de dichtstbijzijnde section/header rond de eerste h1
    (of een div waarvan de class 'hero/masthead/banner/intro' bevat)."""
    try:
        h1 = body.find("h1")
    except Exception:
        h1 = None
    if h1 is None:
        return None
    node = h1
    hero_div = None
    for _ in range(6):
        parent = getattr(node, "parent", None)
        if parent is None or getattr(parent, "name", None) in ("body", "html", "[document]", None):
            break
        node = parent
        cls = _classes(node)
        if node.name in ("section", "header"):
            return node
        if hero_div is None and re.search(r"(?:^|[\s_-])(hero|masthead|banner|intro)(?:$|[\s_-])", cls, re.I):
            hero_div = node
    return hero_div or getattr(h1, "parent", None)


def hero_messages(soup):
    """Aantal 'concurrerende hoofdboodschappen' binnen de heldensectie rond de h1:
    unieke h1 + attentie-badges (eyebrow/kicker/...) + grote hero-koppen. USP-/voordeel-
    stroken tellen NIET mee (die zijn ondersteunend, geen concurrerende kop). Ontdubbelt
    op tekst (wrapper+kind met dezelfde tekst = 1). Valt terug op een begrensde
    body-scan tot de eerste h2 als er geen h1/hero is.
    Retour: (count, list voorbeeldteksten, bounded_bool)."""
    if soup is None:
        return 0, [], False
    body = soup.body or soup
    msgs = []
    seen = set()
    bounded = False
    try:
        hero = _find_hero(body)
        if hero is not None:
            elems = hero.find_all(True)
            # tweede h1 elders op de pagina = extra concurrerende hoofdboodschap
            extra_h1 = max(0, len(body.find_all("h1")) - len(hero.find_all("h1")))
            for el in elems:
                cls = _classes(el)
                if USP_CLASS_RE.search(cls):
                    continue
                if el.name == "h1" or BADGE_CLASS_RE.search(cls) or BIGHEADING_CLASS_RE.search(cls):
                    t = _txt(el)[:80]
                    if t and t not in seen:
                        seen.add(t)
                        msgs.append(t)
            return len(msgs) + extra_h1, msgs[:8], False
        # --- fallback: geen h1/hero -> begrensde body-scan tot eerste h2 ---
        n = 0
        for el in body.find_all(True):
            n += 1
            if el.name == "h2":
                break
            if n > HERO_SCAN_MAX_TAGS:
                bounded = True
                break
            cls = _classes(el)
            if USP_CLASS_RE.search(cls):
                continue
            if el.name == "h1" or BADGE_CLASS_RE.search(cls) or BIGHEADING_CLASS_RE.search(cls):
                t = _txt(el)[:80]
                if t and t not in seen:
                    seen.add(t)
                    msgs.append(t)
    except Exception:
        pass
    return len(msgs), msgs[:8], bounded


def neighbourhood_text(el):
    """Omringende 'beslismoment'-tekst rond een element: klim op tot een blok met
    genoeg context (max NEIGHBOURHOOD_MAX_CHARS), retour die tekst."""
    try:
        node = el
        for _ in range(6):
            parent = getattr(node, "parent", None)
            if parent is None or getattr(parent, "name", None) in ("body", "html", "[document]", None):
                break
            node = parent
            if len(node.get_text(" ", strip=True)) >= NEIGHBOURHOOD_MAX_CHARS:
                break
        return norm(node.get_text(" ", strip=True))[:NEIGHBOURHOOD_MAX_CHARS * 2]
    except Exception:
        return ""


def _in_site_header(el):
    """True als het element in de sticky chrome (<header>/<nav> of header-class) zit —
    zodat we de nav-winkelwagenlink NIET als 'primaire koop-CTA' op een PDP kiezen."""
    try:
        for anc in el.parents:
            nm = getattr(anc, "name", None)
            if nm in ("header", "nav"):
                return True
            if HEADER_STRICT_RE.search(_classes(anc)):
                return True
            if nm in ("main", "article", "body", "[document]", None):
                return False
    except Exception:
        pass
    return False


def primary_buy_cta(soup):
    """Eerste sterke koop-CTA in de CONTENT (niet de nav-winkelwagenlink), voor prijs/trust-
    nabijheid op PDP's. Fallback: eerste intent-CTA (evt. toch uit de header als er niets anders is)."""
    ctas = iter_ctas(soup)
    body_ctas = [c for c in ctas if not _in_site_header(c["el"])]
    pool = body_ctas or ctas
    for c in pool:
        if STRONG_BUY_RE.search(c["text"] or "") or CTA_CLASS_RE.search(c["cls"] or ""):
            return c["el"]
    for c in pool:
        if c["intent"]:
            return c["el"]
    return None


def count_form_fields(form_or_soup):
    """Zichtbare formuliervelden: input/select/textarea, excl. type=hidden/submit/button/reset/image
    en excl. inline display:none / hidden / aria-hidden. Retour int."""
    n = 0
    if form_or_soup is None:
        return 0
    try:
        for el in form_or_soup.find_all(["input", "select", "textarea"]):
            if el.name == "input":
                t = (el.get("type") or "text").lower()
                if t in ("hidden", "submit", "button", "reset", "image"):
                    continue
            style = (el.get("style") or "").replace(" ", "").lower()
            if "display:none" in style or el.has_attr("hidden") or el.get("aria-hidden") == "true":
                continue
            n += 1
    except Exception:
        pass
    return n


def forms_with_fieldcounts(soup):
    """Per <form> het aantal zichtbare velden. Retour list[int]."""
    if soup is None:
        return []
    try:
        return [count_form_fields(f) for f in (soup.body or soup).find_all("form")]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Tier-A: fold uit render_meta (computed styles). Best-effort; leeg als afwezig.
# ---------------------------------------------------------------------------
def load_render_meta(out, sc):
    """Laad p['screenshots']['render_meta'] (relatief aan out). None als afwezig/onleesbaar
    of als er geen echte tekstcoordinaten in zitten (dan is tier-A niet bruikbaar)."""
    rel = (sc or {}).get("render_meta")
    if not rel or out is None:
        return None
    try:
        import json
        p = out / rel
        if not p.exists():
            return None
        meta = json.loads(p.read_text(encoding="utf-8"))
        vps = (meta.get("viewports") or {})
        for vp in ("desktop", "mobile"):
            for t in (vps.get(vp) or {}).get("texts") or []:
                if isinstance(t.get("y"), (int, float)):
                    return meta
        return None
    except Exception:
        return None


def rm_fold_elems(render_meta, viewport, key="texts"):
    vps = (render_meta.get("viewports") or {})
    elems = (vps.get(viewport) or {}).get(key) or []
    foldh = DESKTOP_FOLD_H if viewport == "desktop" else MOBILE_FOLD_H
    out = []
    for t in elems:
        y = t.get("y")
        if isinstance(y, (int, float)) and y < foldh:
            out.append(t)
    return out


def rm_cta_in_fold(render_meta, viewport):
    """Tier-A: zit er een koopintentie-CTA in de vouw van dit viewport?"""
    for t in rm_fold_elems(render_meta, viewport, "texts"):
        tag = (t.get("tag") or "").lower()
        sel = t.get("selector") or ""
        txt = t.get("text") or ""
        if tag in ("a", "button") or "button" in sel.lower():
            if is_cta(txt, sel):
                return True
    return False


def rm_hero_message_count(render_meta, viewport):
    """Tier-A: grote/vette boodschappen in de vouw (fontSize>=20 & fontWeight>=600) + h1/h2."""
    c = 0
    for t in rm_fold_elems(render_meta, viewport, "texts"):
        tag = (t.get("tag") or "").lower()
        fs = t.get("fontSize") or 0
        fw = t.get("fontWeight") or 0
        try:
            fs = float(fs)
            fw = float(fw)
        except Exception:
            fs, fw = 0, 0
        if tag in ("h1", "h2") or (fs >= 20 and fw >= 600) or BADGE_CLASS_RE.search(t.get("selector") or ""):
            c += 1
    return c


# ---------------------------------------------------------------------------
# Optionele PNG-leegte-check (Pillow strikt optioneel + fail-soft)
# ---------------------------------------------------------------------------
def mobile_fold_state(out, sc):
    """Kijk of de mobiele fold-opname (vrijwel) leeg/wit is. Kruiscontroleer met de
    mobiele full-page top-strook om een render-artefact te onderscheiden van een
    echt lege mobiele hero.

    Retour None (onbekend/geen Pillow) of dict:
      {blank: bool, artifact: bool, white_pct: float, full_has_content: bool|None}
      - blank=True & artifact=True  -> capture-artefact (full-top heeft wel content) => NIET de site verwijten
      - blank=True & artifact=False -> mobiele hero lijkt echt leeg => reeel probleem
    """
    try:
        from PIL import Image, ImageStat
    except Exception:
        return None
    mf = (sc or {}).get("mobile_fold")
    if not mf or out is None:
        return None
    try:
        p = out / mf
        if not p.exists():
            return None
        im = Image.open(p).convert("L")
        lo, hi = im.getextrema()
        thumb = im.resize((32, 64))
        st = ImageStat.Stat(thumb)
        mean = st.mean[0]
        std = st.stddev[0]
        blank = (lo >= 250 and hi >= 250) or (mean > 252 and std < 2.0 and (hi - lo) <= 8)
        white_pct = round(100.0 * (mean / 255.0), 1)
        if not blank:
            return {"blank": False, "artifact": False, "white_pct": white_pct, "full_has_content": None}
        # kruiscontrole met mobiele full-page top (zelfde fold-regio)
        full_has_content = None
        artifact = False
        mfull = (sc or {}).get("mobile_full")
        if mfull and (out / mfull).exists():
            try:
                fim = Image.open(out / mfull).convert("L")
                w, h = fim.size
                strip = fim.crop((0, 0, w, min(MOBILE_FOLD_H * 2, h))).resize((32, 64))
                s2 = ImageStat.Stat(strip)
                full_has_content = bool(s2.stddev[0] > 8)  # variatie => er staat content
                artifact = full_has_content
            except Exception:
                full_has_content = None
        return {"blank": True, "artifact": artifact, "white_pct": white_pct,
                "full_has_content": full_has_content}
    except Exception:
        return None
