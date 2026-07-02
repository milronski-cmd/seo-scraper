# -*- coding: utf-8 -*-
"""
MODULE 1.6 — DOELGROEP-LENS (plan §4, KEY=audience, ORDER=60).

Beoordeelt een site NIET generiek, maar door de bril van ZIJN doelgroep. Welk
profiel geldt, komt uit een configureerbare bron (env -> shared -> ingebouwde
default -> generiek); zie audits/_wiring/audience.md voor het config-formaat.

Vier profielen, elk met een eigen checklist (bijlagen A-C in de wiring-notitie):
  * senioren    — rustig leesbaar, klikbaar telefoonnummer, geen nep-schaarste,
                  grote knoppen, korte formulieren, hulp/keuzehulp vindbaar.
  * forenzen    — mobiel-first met CTA in de fold, spec-diepte op PDP's,
                  vergelijken, kern-koopinfo in de mobiele fold, legaliteit.
  * sleutelaars — pasvorm/compatibiliteit op PDP, model-filter op PLP,
                  zoekfunctie, specs+levertijd, voorraad.
  * generiek    — CTA in de fold, body >=16px, zoek- of duidelijke navigatie,
                  contact vindbaar.

Elke check is pass / fail / ONMEETBAAR. Onmeetbaar telt NIET mee in de score,
maar wordt wél in data gerapporteerd. Score 0-100 = gewogen % gehaalde meetbare
checks (graduatie: een check die op een deel van de relevante pagina's slaagt,
telt naar rato mee). Databronnen: render_meta.json (module 1.1) + dom.html +
page-record-velden. Fail-soft overal, geen netwerk. Degradeert zonder
screenshots naar dom + page-record-velden + een expliciete note.

Contract + ctx: zie INTEGRATION.md. Wiring-notitie: audits/_wiring/audience.md.
"""
import html as _htmlmod

try:
    from . import _audience_helpers as H
except Exception:                                    # pragma: no cover
    import _audience_helpers as H

KEY = "audience"
LABEL = "Doelgroep-lens"
ORDER = 60


# =============================================================================
# Pagina-views (1x laden per pagina; hergebruikt door alle checks)
# =============================================================================
def _build_views(ctx):
    views = []
    for p in (ctx.get("pages") or []):
        soup, raw = H.load_dom(ctx, p)
        tags = H.classify_page(ctx, p, soup)
        awps = p.get("avg_words_per_sentence")
        flesch = p.get("readability_flesch")
        size_kb = p.get("size_kb")
        if size_kb is None:
            tbw = p.get("total_byte_weight")
            size_kb = (tbw / 1024.0) if isinstance(tbw, (int, float)) and tbw else None
        views.append({
            "page": p,
            "url": p.get("url") or "",
            "tags": tags,
            "soup": soup,
            "raw": raw,
            "render": H.load_render(ctx, p),
            "text": H.page_text(ctx, p),
            "awps": awps if isinstance(awps, (int, float)) else None,
            "flesch": flesch if isinstance(flesch, (int, float)) else None,
            "size_kb": size_kb if isinstance(size_kb, (int, float)) else None,
        })
    return views


def _content_pages(views):
    return [v for v in views if "checkout" not in v["tags"]]


def _scope_pages(views, scope):
    if scope == "site":
        return _content_pages(views)
    if scope == "home":
        return [v for v in views if "home" in v["tags"]] or _content_pages(views)[:1]
    return [v for v in _content_pages(views) if scope in v["tags"]]


def _result(measurable, frac, n, n_pass, meting, example_url="", detail=None):
    return {"measurable": bool(measurable),
            "frac": (None if not measurable else float(frac)),
            "n": int(n), "n_pass": int(n_pass), "meting": meting,
            "example_url": example_url or "", "detail": detail or {}}


def _pages_frac(pages, predicate):
    """Loop over pages; predicate -> True(pass)/False(fail)/None(niet meetbaar).
    Geef (measurable_pages, pass_pages, example_fail_url)."""
    n = npass = 0
    example = ""
    for v in pages:
        r = predicate(v)
        if r is None:
            continue
        n += 1
        if r:
            npass += 1
        elif not example:
            example = v["url"]
    return n, npass, example


# =============================================================================
# Evaluatoren  ev(views, params, ctx) -> _result(...)
# =============================================================================
def ev_body_font(views, params, ctx):
    minpx = float(params.get("min_body_px", 18))
    pages = [v for v in _content_pages(views) if v["render"]]
    if not pages:
        return _result(False, None, 0, 0, f"body-fontgrootte niet meetbaar zonder render (norm >={minpx:g}px)")
    n = npass = 0
    meds = []
    example = ""
    for v in pages:
        med, cnt = H.median_body_font(v["render"])
        if med is None:
            continue
        n += 1
        meds.append(med)
        if med >= minpx:
            npass += 1
        elif not example:
            example = v["url"]
    if n == 0:
        return _result(False, None, 0, 0, f"geen body-teksten in render (norm >={minpx:g}px)")
    site_med = round(sorted(meds)[len(meds) // 2], 1) if meds else None
    min_med = round(min(meds), 1) if meds else None
    n_fail = n - npass
    if n_fail:
        meting = (f"op {n_fail}/{n} pagina('s) is de mediane bodytekst < {minpx:g}px "
                  f"(kleinste pagina-mediaan {min_med:g}px, site-mediaan {site_med:g}px)")
    else:
        meting = (f"mediane bodytekst >= {minpx:g}px op alle {n} pagina('s) "
                  f"(kleinste pagina-mediaan {min_med:g}px)")
    return _result(True, npass / n, n, npass, meting, example,
                   {"site_median_px": site_med, "min_page_median_px": min_med,
                    "fail_pages": n_fail, "per_page_px": meds})


def ev_tel_header(views, params, ctx):
    def pred(v):
        ok, _n = H.tel_in_header(v["soup"], v["raw"])
        return ok  # None als geen dom
    pages = _content_pages(views)
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "geen DOM om telefoonnummer te toetsen")
    n_fail = n - npass
    if npass == 0:
        meting = f"geen klikbaar tel:-nummer in de header op alle {n} pagina('s)"
    elif n_fail:
        meting = (f"klikbaar tel:-nummer in de header op {npass}/{n} pagina('s) "
                  f"(ontbreekt op {n_fail})")
    else:
        meting = f"klikbaar tel:-nummer in de header op alle {n} pagina('s)"
    return _result(True, npass / n, n, npass, meting, example)


def ev_cta_min(views, params, ctx):
    minpx = float(params.get("min_cta_px", 44))
    heights = []
    for v in _content_pages(views):
        if v["render"]:
            heights.extend(H.cta_heights(v["render"]))
    if not heights:
        return _result(False, None, 0, 0, f"knop-/CTA-hoogte niet meetbaar zonder render (norm >={minpx:g}px)")
    ok = [h for h in heights if h >= minpx]
    heights.sort()
    med = heights[len(heights) // 2]
    return _result(True, len(ok) / len(heights), len(heights), len(ok),
                   f"{len(ok)}/{len(heights)} knoppen >={minpx:g}px hoog "
                   f"(mediaan {med:.0f}px, kleinste {min(heights):.0f}px)", "",
                   {"median_px": round(med, 1), "min_px": round(min(heights), 1)})


def ev_no_countdown(views, params, ctx):
    def pred(v):
        if not (v["text"] or v["raw"]):
            return None
        blob = v["text"]
        if H.RE_COUNTDOWN.search(blob):
            return False
        if v["raw"] and H.RE_COUNTDOWN.search(v["raw"][:20000]):
            return False
        return True
    pages = _content_pages(views)
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "geen tekst om nep-schaarste te toetsen")
    return _result(True, npass / n, n, npass,
                   f"geen countdown/nep-schaarste op {npass}/{n} pagina('s)", example)


def ev_sentences(views, params, ctx):
    maxw = float(params.get("max_awps", 22))
    def pred(v):
        if v["awps"] is not None:
            return v["awps"] <= maxw
        if v["flesch"] is not None:
            return v["flesch"] >= 50.0   # 'redelijk leesbaar' als awps ontbreekt
        return None
    pages = [v for v in _content_pages(views) if (v["page"].get("word_count") or 0) > 60]
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "geen leesbaarheidscijfers beschikbaar")
    return _result(True, npass / n, n, npass,
                   f"zinnen kort genoeg (<= {maxw:g} woorden) op {npass}/{n} pagina('s)", example)


def ev_help(views, params, ctx):
    def pred(v):
        if not (v["text"] or v["soup"]):
            return None
        return H.find_text_signal(H.RE_HELP, v["soup"], v["text"], nav_only=True)
    pages = _content_pages(views)
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "geen inhoud om hulp/keuzehulp te vinden")
    # 'findbaar' = ergens op de site; graad = dekking over pagina's
    return _result(True, npass / n, n, npass,
                   f"hulp/keuzehulp/FAQ vindbaar op {npass}/{n} pagina('s)",
                   example if npass < n else "")


def ev_forms(views, params, ctx):
    maxf = int(params.get("max_fields", 5))
    n = npass = 0
    example = ""
    worst = None
    for v in _content_pages(views):
        fields = H.count_visible_form_fields(v["soup"])
        if not fields:            # None (geen soup) of [] (geen formulier)
            continue
        n += 1
        biggest = max(fields)
        if worst is None or biggest > worst:
            worst = biggest
        if biggest <= maxf:
            npass += 1
        elif not example:
            example = v["url"]
    if n == 0:
        return _result(False, None, 0, 0, "geen formulieren gevonden (buiten checkout)")
    return _result(True, npass / n, n, npass,
                   f"{npass}/{n} formulier-pagina('s) met <= {maxf} velden "
                   f"(grootste formulier: {worst} velden)", example,
                   {"max_fields_seen": worst})


def ev_mobile_cta_fold(views, params, ctx):
    pages = [v for v in _content_pages(views) if v["render"]]
    if not pages:
        return _result(False, None, 0, 0, "mobiele render ontbreekt (geen --screenshots)")
    n = npass = 0
    example = ""
    for v in pages:
        found, has_vp = H.cta_in_fold(v["render"], "mobile", H.FOLD_MOBILE_PX)
        if not has_vp:
            continue
        n += 1
        if found:
            npass += 1
        elif not example:
            example = v["url"]
    if n == 0:
        return _result(False, None, 0, 0, "geen mobiele render-teksten om de fold te toetsen")
    return _result(True, npass / n, n, npass,
                   f"CTA in de mobiele fold (<= {H.FOLD_MOBILE_PX}px) op {npass}/{n} pagina('s)", example)


def ev_spec_block(views, params, ctx):
    pages = _scope_pages(views, "pdp")
    if not pages:
        return _result(False, None, 0, 0, "geen productpagina's (PDP) in deze run")
    def pred(v):
        return H.has_spec_block(v["soup"], v["text"])
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "PDP's zonder toetsbare inhoud")
    return _result(True, npass / n, n, npass,
                   f"spec-tabel/-blok op {npass}/{n} PDP('s)", example)


def ev_core_buy_fold(views, params, ctx):
    pages = [v for v in _scope_pages(views, "pdp") if v["render"]]
    if not pages:
        return _result(False, None, 0, 0, "geen PDP-render om de mobiele koop-fold te toetsen")
    n = npass = 0
    example = ""
    for v in pages:
        found, has_vp = H.price_and_spec_in_fold(v["render"], "mobile", H.FOLD_MOBILE_PX)
        if not has_vp:
            continue
        n += 1
        if found:
            npass += 1
        elif not example:
            example = v["url"]
    if n == 0:
        return _result(False, None, 0, 0, "geen mobiele PDP-render-teksten")
    return _result(True, npass / n, n, npass,
                   f"prijs + spec in de mobiele fold op {npass}/{n} PDP('s)", example)


def ev_compare(views, params, ctx):
    pages = _content_pages(views)
    def pred(v):
        if not (v["text"] or v["soup"]):
            return None
        return H.find_text_signal(H.RE_COMPARE, v["soup"], v["text"])
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "geen inhoud om vergelijk-functie te vinden")
    site_ok = 1.0 if npass > 0 else 0.0    # vergelijken = site-functie (binair)
    return _result(True, site_ok, n, npass,
                   f"vergelijk-functie {'gevonden' if npass else 'niet gevonden'} "
                   f"({npass}/{n} pagina's)", "" if npass else (pages[0]["url"] if pages else ""))


def ev_page_weight(views, params, ctx):
    maxkb = float(params.get("max_kb", 4000))
    def pred(v):
        if v["size_kb"] is None:
            return None
        return v["size_kb"] <= maxkb
    pages = _content_pages(views)
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "geen pagina-gewicht (size_kb) beschikbaar")
    sizes = [v["size_kb"] for v in pages if v["size_kb"] is not None]
    avg = sum(sizes) / len(sizes) if sizes else 0
    return _result(True, npass / n, n, npass,
                   f"HTML-gewicht gem. {avg:.0f}KB; {npass}/{n} pagina('s) onder {maxkb:g}KB", example)


def ev_legal(views, params, ctx):
    if not H.is_mobility_domain(ctx):
        return _result(False, None, 0, 0, "legaliteit-info niet van toepassing op dit domein")
    pages = _content_pages(views)
    def pred(v):
        if not v["text"]:
            return None
        return bool(H.RE_LEGAL.search(v["text"]))
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "geen tekst om legaliteit-info te vinden")
    site_ok = 1.0 if npass > 0 else 0.0
    return _result(True, site_ok, n, npass,
                   f"legaliteit/regelgeving {'benoemd' if npass else 'niet benoemd'} "
                   f"({npass}/{n} pagina's)", "" if npass else (pages[0]["url"] if pages else ""))


def ev_fit_signal(views, params, ctx):
    pages = _scope_pages(views, "pdp")
    if not pages:
        return _result(False, None, 0, 0, "geen productpagina's (PDP) in deze run")
    def pred(v):
        return H.find_text_signal(H.RE_FIT, v["soup"], v["text"])
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "PDP's zonder toetsbare inhoud")
    return _result(True, npass / n, n, npass,
                   f"pasvorm/compatibiliteit benoemd op {npass}/{n} PDP('s)", example)


def ev_model_filter(views, params, ctx):
    pages = _scope_pages(views, "plp")
    if not pages:
        return _result(False, None, 0, 0, "geen overzichts-/categoriepagina's (PLP) in deze run")
    def pred(v):
        return H.has_model_filter(v["soup"], v["text"])
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "PLP's zonder toetsbare inhoud")
    return _result(True, npass / n, n, npass,
                   f"model-/merkfilter op {npass}/{n} PLP('s)", example)


def ev_search(views, params, ctx):
    pages = _content_pages(views)
    def pred(v):
        r = H.has_search(v["soup"], v["raw"])
        return r  # None als geen dom
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "geen DOM om zoekfunctie te toetsen")
    site_ok = 1.0 if npass > 0 else 0.0
    return _result(True, site_ok, n, npass,
                   f"zoekfunctie {'aanwezig' if npass else 'niet aangetroffen'} "
                   f"({npass}/{n} pagina's)", "" if npass else (pages[0]["url"] if pages else ""))


def ev_specs_delivery(views, params, ctx):
    pages = _scope_pages(views, "pdp")
    if not pages:
        return _result(False, None, 0, 0, "geen productpagina's (PDP) in deze run")
    def pred(v):
        specs = H.has_spec_block(v["soup"], v["text"])
        deliv = bool(H.RE_DELIVERY.search(v["text"]) or H.RE_STOCK.search(v["text"])) if v["text"] else False
        return specs and deliv
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "PDP's zonder toetsbare inhoud")
    return _result(True, npass / n, n, npass,
                   f"specs + levertijd zichtbaar op {npass}/{n} PDP('s)", example)


def ev_stock(views, params, ctx):
    pages = _scope_pages(views, "pdp")
    if not pages:
        return _result(False, None, 0, 0, "geen productpagina's (PDP) in deze run")
    def pred(v):
        if not v["text"]:
            return None
        return bool(H.RE_STOCK.search(v["text"]))
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "PDP's zonder toetsbare inhoud")
    return _result(True, npass / n, n, npass,
                   f"voorraad-indicatie op {npass}/{n} PDP('s)", example)


def ev_body16(views, params, ctx):
    p = dict(params)
    p.setdefault("min_body_px", 16)
    return ev_body_font(views, p, ctx)


def ev_search_or_nav(views, params, ctx):
    pages = _content_pages(views)
    def pred(v):
        s = H.has_search(v["soup"], v["raw"])
        nav = H.has_clear_nav(v["soup"], v["page"])
        if s is None and not nav and not v["raw"]:
            return None
        return bool(s) or bool(nav)
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "geen DOM om zoek/navigatie te toetsen")
    return _result(True, npass / n, n, npass,
                   f"zoek- of duidelijke navigatie op {npass}/{n} pagina('s)", example)


def ev_contact(views, params, ctx):
    pages = _content_pages(views)
    def pred(v):
        if v["soup"] is None and not v["raw"]:
            # laatste redmiddel: contact-pagina in interne links
            il = v["page"].get("internal_links") or []
            return any("contact" in str(u).lower() for u in il) or None
        if H.has_clickable_tel(v["soup"], v["raw"]):
            return True
        blob = (v["raw"] or "")[:20000].lower()
        if "mailto:" in blob or "/contact" in blob or ">contact<" in blob:
            return True
        return bool(v["text"] and "contact" in v["text"])
    n, npass, example = _pages_frac(pages, pred)
    if n == 0:
        return _result(False, None, 0, 0, "geen inhoud om contact te vinden")
    site_ok = 1.0 if npass > 0 else 0.0
    return _result(True, site_ok, n, npass,
                   f"contact {'vindbaar' if npass else 'niet vindbaar'} ({npass}/{n} pagina's)",
                   "" if npass else (pages[0]["url"] if pages else ""))


# =============================================================================
# Profiel-checklists (bijlagen A-C). weight = impact op DIT profiel.
# =============================================================================
def _checks_for(profile):
    S = "senioren"; F = "forenzen"; L = "sleutelaars"; G = "generiek"
    C = {
        S: [
            dict(key="body_font_18", label="Rustige leesgrootte (>=18px)", weight=3,
                 severity="High", scope="site", needs_render=True, primary="min_body_px",
                 params={"min_body_px": 18}, ev=ev_body_font,
                 title="Lopende tekst te klein voor senioren ({meting})",
                 why="Senioren lezen 18px+ comfortabel; 15-16px verhoogt de afhaakkans en het aantal telefoontjes met vragen.",
                 fix="Zet de basis-fontgrootte-token (bijv. --font-size-base) op >=18px voor lopende tekst."),
            dict(key="tel_in_header", label="Klikbaar telefoonnummer in de header", weight=2,
                 severity="High", scope="site", needs_render=False, primary=None,
                 params={}, ev=ev_tel_header,
                 title="Klikbaar telefoonnummer niet op alle pagina's in de header ({meting})",
                 why="Senioren bellen liever dan formulieren invullen; een tel:-nummer bovenaan haalt drempel en twijfel weg.",
                 fix="Zet een klikbaar <a href=\"tel:...\"> nummer zichtbaar in de header/topbalk, op elke pagina."),
            dict(key="no_countdown", label="Geen countdown / nep-schaarste", weight=2,
                 severity="High", scope="site", needs_render=False, primary=None,
                 params={}, ev=ev_no_countdown,
                 title="Countdown/nep-schaarste ondermijnt het vertrouwen",
                 why="Senioren wantrouwen druk en aftel-tactieken; dat kost vertrouwen en dus conversie bij deze doelgroep.",
                 fix="Verwijder aftelklokken en 'nog maar X'-teksten; kies rust en eerlijke, blijvende informatie."),
            dict(key="cta_min_44", label="Grote knoppen (>=44px)", weight=2,
                 severity="Medium", scope="site", needs_render=True, primary="min_cta_px",
                 params={"min_cta_px": 44}, ev=ev_cta_min,
                 title="Knoppen te klein voor senioren ({meting})",
                 why="Kleine knoppen zijn lastig te raken met minder vaste handen of op een tablet; 44px is de comfortabele minimum-maat.",
                 fix="Geef knoppen/CTA's minimaal 44px hoogte (padding + regelhoogte) via de knop-tokens."),
            dict(key="sentences_short", label="Korte zinnen", weight=2,
                 severity="Medium", scope="site", needs_render=False, primary="max_awps",
                 params={"max_awps": 22}, ev=ev_sentences,
                 title="Te lange zinnen voor comfortabel lezen ({meting})",
                 why="Lange zinnen (>22 woorden gemiddeld) lezen zwaar; senioren haken dan eerder af of bellen met vragen.",
                 fix="Kort zinnen in tot gemiddeld <=22 woorden; splits opsommingen en gebruik tussenkopjes."),
            dict(key="help_findable", label="Hulp / keuzehulp vindbaar", weight=1,
                 severity="Medium", scope="site", needs_render=False, primary=None,
                 params={}, ev=ev_help,
                 title="Hulp/keuzehulp niet duidelijk vindbaar",
                 why="Twijfelende senioren zoeken geruststelling; een zichtbare keuzehulp/FAQ/adviesknop voorkomt afhaken.",
                 fix="Zet een 'Keuzehulp', 'Hulp bij de keuze' of 'Veelgestelde vragen' zichtbaar in het hoofdmenu."),
            dict(key="forms_short", label="Korte formulieren (<=5 velden)", weight=1,
                 severity="Medium", scope="form", needs_render=False, primary="max_fields",
                 params={"max_fields": 5}, ev=ev_forms,
                 title="Formulier te lang voor deze doelgroep ({meting})",
                 why="Lange formulieren schrikken senioren af; elk extra veld verhoogt de kans dat men stopt of belt.",
                 fix="Beperk het formulier tot maximaal 5 zichtbare velden; vraag de rest later of telefonisch uit."),
        ],
        F: [
            dict(key="mobile_cta_fold", label="CTA in de mobiele fold", weight=3,
                 severity="High", scope="site", needs_render=True, primary=None,
                 params={}, ev=ev_mobile_cta_fold,
                 title="Geen CTA in de mobiele fold",
                 why="Forenzen oriënteren mobiel en snel; zonder actieknop in de eerste 844px scrollen ze weg voor ze iets doen.",
                 fix="Plaats de primaire CTA (bekijk/plan/bestel) binnen de mobiele fold (<=844px) of maak een sticky CTA-balk."),
            dict(key="spec_block_pdp", label="Spec-diepte op PDP", weight=2,
                 severity="High", scope="pdp", needs_render=False, primary=None,
                 params={}, ev=ev_spec_block,
                 title="PDP mist een duidelijk spec-blok",
                 why="Forenzen vergelijken op specificaties (bereik, snelheid, gewicht); zonder spec-tabel kiezen ze de concurrent.",
                 fix="Zet een overzichtelijke spec-tabel (bereik, topsnelheid, gewicht, accu) prominent op elke PDP."),
            dict(key="core_buy_fold_pdp", label="Prijs + spec in mobiele fold (PDP)", weight=2,
                 severity="High", scope="pdp", needs_render=True, primary=None,
                 params={}, ev=ev_core_buy_fold,
                 title="Kern-koopinfo niet in de mobiele fold op de PDP",
                 why="Prijs en minstens één kern-spec horen mobiel direct zichtbaar; anders moet de forens zoeken en haakt af.",
                 fix="Toon prijs en minimaal één kern-spec binnen de mobiele fold van de PDP (boven de eerste scroll)."),
            dict(key="compare_feature", label="Vergelijk-functie", weight=1,
                 severity="Medium", scope="site", needs_render=False, primary=None,
                 params={}, ev=ev_compare,
                 title="Geen vergelijk-functie gevonden",
                 why="Forenzen willen modellen naast elkaar leggen; ontbreekt dat, dan vergelijken ze elders en kopen daar.",
                 fix="Voeg een 'Vergelijk'-functie toe waarmee 2-3 modellen op specs naast elkaar te zetten zijn."),
            dict(key="page_weight_ok", label="Redelijk pagina-gewicht", weight=1,
                 severity="Low", scope="site", needs_render=False, primary="max_kb",
                 params={"max_kb": 4000}, ev=ev_page_weight,
                 title="Zware pagina('s) voor mobiel gebruik ({meting})",
                 why="Forenzen zitten vaak op 4G/onderweg; zware pagina's laden traag en kosten bezoekers.",
                 fix="Comprimeer beelden en beperk scripts zodat pagina's ook op mobiel vlot laden."),
            dict(key="legal_info", label="Legaliteit/regelgeving benoemd", weight=1,
                 severity="Medium", scope="site", needs_render=False, primary=None,
                 params={}, ev=ev_legal,
                 title="Legaliteit/regelgeving onvoldoende benoemd",
                 why="Bij e-steps/scooters twijfelt de forens over wat legaal de weg op mag (RDW, verzekering); onduidelijkheid remt de koop.",
                 fix="Benoem helder wat legaal is (RDW/typegoedkeuring, verzekering, waar toegestaan) op relevante pagina's."),
        ],
        L: [
            dict(key="fit_signal_pdp", label="Pasvorm/compatibiliteit op PDP", weight=3,
                 severity="High", scope="pdp", needs_render=False, primary=None,
                 params={}, ev=ev_fit_signal,
                 title="PDP mist een pasvorm-/compatibiliteitssignaal",
                 why="Sleutelaars vrezen 'past dit op mijn model?'; zonder 'past op / geschikt voor'-info bestellen ze uit onzekerheid niet.",
                 fix="Zet expliciet 'past op / geschikt voor model X' (met modellijst) op elke onderdeel-PDP."),
            dict(key="model_filter_plp", label="Model-/merkfilter op PLP", weight=2,
                 severity="High", scope="plp", needs_render=False, primary=None,
                 params={}, ev=ev_model_filter,
                 title="Overzichtspagina zonder model-/merkfilter",
                 why="Sleutelaars zoeken een onderdeel voor een specifiek model; zonder model-filter vinden ze het juiste deel niet.",
                 fix="Voeg een filter of navigatie op merk/model toe aan de overzichts-/categoriepagina's."),
            dict(key="search_present", label="Zoekfunctie", weight=2,
                 severity="High", scope="site", needs_render=False, primary=None,
                 params={}, ev=ev_search,
                 title="Geen zoekfunctie aangetroffen",
                 why="Sleutelaars zoeken gericht op onderdeel + modelnummer; zonder zoekbalk is de catalogus niet doorzoekbaar.",
                 fix="Voeg een zichtbare zoekfunctie (input[type=search]) in de header toe, met zoeken op model/onderdeel."),
            dict(key="specs_delivery_pdp", label="Specs + levertijd op PDP", weight=2,
                 severity="Medium", scope="pdp", needs_render=False, primary=None,
                 params={}, ev=ev_specs_delivery,
                 title="PDP mist specs en/of levertijd",
                 why="Sleutelaars willen maten/specs én weten wanneer het geleverd wordt voordat ze bestellen.",
                 fix="Toon technische specs én een concrete levertijd/voorraadstatus op elke PDP."),
            dict(key="stock_indication", label="Voorraad-indicatie", weight=1,
                 severity="Medium", scope="pdp", needs_render=False, primary=None,
                 params={}, ev=ev_stock,
                 title="Geen voorraad-indicatie op de PDP",
                 why="Onderdelen zijn vaak model-gebonden; sleutelaars willen zien of het op voorraad is voor ze de reparatie plannen.",
                 fix="Toon een duidelijke voorraadstatus ('op voorraad' / levertermijn) op elke PDP."),
        ],
        G: [
            dict(key="mobile_cta_fold", label="CTA in de mobiele fold", weight=2,
                 severity="High", scope="site", needs_render=True, primary=None,
                 params={}, ev=ev_mobile_cta_fold,
                 title="Geen CTA in de mobiele fold",
                 why="Mobiel is het grootste deel van het verkeer; zonder actieknop in de eerste schermvulling haakt men af.",
                 fix="Plaats de primaire CTA binnen de mobiele fold (<=844px) of maak een sticky CTA-balk."),
            dict(key="body_16", label="Body >=16px", weight=2,
                 severity="Medium", scope="site", needs_render=True, primary="min_body_px",
                 params={"min_body_px": 16}, ev=ev_body16,
                 title="Lopende tekst kleiner dan 16px ({meting})",
                 why="Onder 16px leest lopende tekst zwaar op mobiel en veroorzaakt iOS-zoom bij formulieren.",
                 fix="Zet de basis-fontgrootte op >=16px via de type-schaal-token."),
            dict(key="search_or_nav", label="Zoek- of duidelijke navigatie", weight=1,
                 severity="Medium", scope="site", needs_render=False, primary=None,
                 params={}, ev=ev_search_or_nav,
                 title="Geen zoekfunctie of duidelijke navigatie",
                 why="Bezoekers moeten snel hun weg vinden; zonder zoek of helder menu verdwalen ze en vertrekken.",
                 fix="Zorg voor een duidelijk hoofdmenu (>=3 items) en/of een zoekfunctie in de header."),
            dict(key="contact_findable", label="Contact vindbaar", weight=1,
                 severity="Medium", scope="site", needs_render=False, primary=None,
                 params={}, ev=ev_contact,
                 title="Contact niet duidelijk vindbaar",
                 why="Vertrouwen vergt bereikbaarheid; zonder zichtbaar telefoon/e-mail/contactlink twijfelt de bezoeker.",
                 fix="Zet contactgegevens (tel/e-mail) of een duidelijke contactlink zichtbaar in header of footer."),
        ],
    }
    return C.get(profile, C[G])


# =============================================================================
# Overrides toepassen op één check
# =============================================================================
def _apply_override(chk, overrides):
    """Geef (enabled, params). overrides[key]: False=uit, dict=param-merge,
    scalar=primaire parameter zetten."""
    params = dict(chk.get("params") or {})
    if not isinstance(overrides, dict) or chk["key"] not in overrides:
        return True, params
    ov = overrides[chk["key"]]
    if ov is False:
        return False, params
    if isinstance(ov, dict):
        params.update(ov)
    elif isinstance(ov, (int, float)) and chk.get("primary"):
        params[chk["primary"]] = ov
    return True, params


# =============================================================================
# Hoofd-audit
# =============================================================================
def audit(ctx):
    try:
        return _audit(ctx)
    except Exception as e:                       # extra vangnet bovenop de runner
        return {"score": None,
                "summary": f"Doelgroep-lens kon niet draaien: {e}",
                "issues": [], "data": {"error": str(e)}}


_PROFIEL_NL = {
    "senioren": "senioren (rustig, vertrouwd, telefonisch)",
    "forenzen": "forenzen (mobiel-first, vergelijkt op specs)",
    "sleutelaars": "sleutelaars (onderdeel + model, 'past dit?')",
    "generiek": "generiek (geen specifiek doelgroepprofiel)",
}


def _audit(ctx):
    profile, bron, overrides, cfg_note = H.resolve_audience(ctx)
    views = _build_views(ctx)
    render_present = any(v["render"] for v in views)

    checks = _checks_for(profile)
    results = []          # (chk, enabled, res)
    for chk in checks:
        enabled, params = _apply_override(chk, overrides)
        if not enabled:
            results.append((chk, False, None))
            continue
        try:
            res = chk["ev"](views, params, ctx)
        except Exception as e:
            res = _result(False, None, 0, 0, f"meetfout: {e}")
        res["_params"] = params
        results.append((chk, True, res))

    # -------- score: gewogen % over MEETBARE checks --------------------------
    wsum = 0.0
    wscore = 0.0
    for chk, enabled, res in results:
        if not enabled or not res or not res["measurable"] or res["frac"] is None:
            continue
        w = float(chk["weight"])
        wsum += w
        wscore += w * res["frac"]
    score = round(100.0 * wscore / wsum, 1) if wsum > 0 else None

    # -------- issues per gefaalde meetbare check -----------------------------
    issues = []
    render_needed_unmeasured = False
    for chk, enabled, res in results:
        if not enabled or res is None:
            continue
        if not res["measurable"]:
            if chk.get("needs_render") and not render_present:
                render_needed_unmeasured = True
            continue
        if res["frac"] is not None and res["frac"] < 1.0:
            title = chk["title"].replace("{meting}", res["meting"])
            issues.append({
                "severity": chk["severity"],
                "category": "doelgroep",
                "title": title,
                "why": chk["why"],
                "fix": chk["fix"],
                "url": res["example_url"] or "",
            })

    # consolideerde degradatie-note (zoals contrast/1.7): render ontbreekt
    if render_needed_unmeasured:
        issues.append({
            "severity": "Low", "category": "doelgroep",
            "title": "Deel van de doelgroep-checks niet gemeten (geen render)",
            "why": ("Zonder gerenderde pagina (screenshots + render_meta.json van module 1.1) "
                    "kunnen fontgrootte-, knop- en fold-checks niet exact worden gemeten; "
                    "die zijn nu als 'onmeetbaar' gerapporteerd."),
            "fix": "Draai de scraper met --screenshots voor de volledige doelgroep-lens.",
            "url": "",
        })

    # sorteer op severity
    _sev = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    issues.sort(key=lambda it: _sev.get(it["severity"], 9))

    # -------- data -----------------------------------------------------------
    checks_data = []
    for chk, enabled, res in results:
        if not enabled:
            checks_data.append({"key": chk["key"], "status": "uit", "gewicht": chk["weight"],
                                "meting": "uitgezet via config-override"})
            continue
        if not res["measurable"]:
            status = "onmeetbaar"
        elif res["frac"] is None:
            status = "onmeetbaar"
        elif res["frac"] >= 0.999:
            status = "pass"
        else:
            status = "fail"
        entry = {"key": chk["key"], "status": status, "gewicht": chk["weight"],
                 "meting": res["meting"]}
        if res["measurable"]:
            entry["aandeel"] = round(res["frac"], 3)
            entry["n"] = res["n"]
            entry["n_pass"] = res["n_pass"]
        if res.get("detail"):
            entry["detail"] = res["detail"]
        checks_data.append(entry)

    per_page = []
    for v in views:
        pp = {"url": v["url"], "tags": sorted(v["tags"]), "render": bool(v["render"])}
        if v["awps"] is not None:
            pp["avg_words_per_sentence"] = v["awps"]
        ff = H.count_visible_form_fields(v["soup"])
        if ff:
            pp["form_fields"] = ff
        per_page.append(pp)

    data = {
        "profiel": profile,
        "config_bron": bron,
        "overrides": overrides or {},
        "render_present": render_present,
        "checks": checks_data,
        "per_page": per_page,
    }
    if cfg_note:
        data["config_note"] = cfg_note

    # -------- summary --------------------------------------------------------
    n_meas = sum(1 for c in checks_data if c["status"] in ("pass", "fail"))
    n_onmeet = sum(1 for c in checks_data if c["status"] == "onmeetbaar")
    if score is None:
        duiding = "geen meetbare checks (draai met --screenshots / meer pagina's)"
    elif score >= 85:
        duiding = "sterk afgestemd op deze doelgroep"
    elif score >= 65:
        duiding = "redelijk afgestemd, met concrete verbeterpunten"
    elif score >= 40:
        duiding = "matig afgestemd; belangrijke doelgroep-eisen missen"
    else:
        duiding = "slecht afgestemd op deze doelgroep"
    summary = (f"Profiel '{profile}' (bron: {bron}). "
               + (f"Score {score}/100 — {duiding}. " if score is not None else f"{duiding.capitalize()}. ")
               + f"{n_meas} meetbare check(s), {n_onmeet} onmeetbaar.")

    html = _render_html(profile, bron, score, checks_data, overrides)

    return {"score": score, "summary": summary, "issues": issues,
            "data": data, "html": html}


# =============================================================================
# Optionele HTML (checklist-tabel, inline styles, zelfstandig)
# =============================================================================
def _render_html(profile, bron, score, checks_data, overrides):
    try:
        pill = {"pass": ("#1a7f37", "#e6f4ea", "geslaagd"),
                "fail": ("#b42318", "#fde8e6", "gefaald"),
                "onmeetbaar": ("#6b7280", "#f1f3f5", "onmeetbaar"),
                "uit": ("#6b7280", "#f1f3f5", "uit")}
        rows = ""
        for c in checks_data:
            col, bg, lbl = pill.get(c["status"], pill["onmeetbaar"])
            meting = _htmlmod.escape(str(c.get("meting", "")))
            rows += (
                "<tr style='border-bottom:1px solid #eee'>"
                f"<td style='padding:5px 10px'>{_htmlmod.escape(c['key'])}</td>"
                f"<td style='padding:5px 10px;text-align:center'>"
                f"<span style='background:{bg};color:{col};padding:1px 8px;border-radius:10px;"
                f"font-size:12px;font-weight:600'>{lbl}</span></td>"
                f"<td style='padding:5px 10px;text-align:center;color:#555'>{c.get('gewicht','')}</td>"
                f"<td style='padding:5px 10px;color:#333'>{meting}</td></tr>")
        prof_lbl = _htmlmod.escape(_PROFIEL_NL.get(profile, profile))
        score_s = "n.v.t." if score is None else f"{score:g}/100"
        return (
            "<div style='margin-top:8px;font-size:13px;color:#333'>"
            f"<p style='margin:0 0 6px'>Doelgroepprofiel: <strong>{prof_lbl}</strong> "
            f"(config-bron: <code>{_htmlmod.escape(bron)}</code>) &middot; score {score_s}</p>"
            "<table style='border-collapse:collapse;font-size:13px;width:100%'>"
            "<thead><tr style='border-bottom:1px solid #ddd;text-align:left'>"
            "<th style='padding:5px 10px'>Check</th>"
            "<th style='padding:5px 10px;text-align:center'>Status</th>"
            "<th style='padding:5px 10px;text-align:center'>Gewicht</th>"
            "<th style='padding:5px 10px'>Meting</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            "<p style='margin:6px 0 0;color:#666'>Onmeetbare checks tellen niet mee in de score.</p>"
            "</div>")
    except Exception:
        return ""
