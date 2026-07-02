# -*- coding: utf-8 -*-
"""
MODULE 1.2 — Conversie-audit (CRO) — plan §4.

Meet per pagina op de ECHTE render (render_meta.json + dom.html) als
p["screenshots"]["ok"], anders degradatie naar page-record-velden + page_texts.
Zeven conversie-principes:

  1. CTA boven de vouw (desktop EN mobiel; mobiel weegt zwaarst)
  2. Een boodschap per viewport (drukte in de heldensectie)
  3. Trust op het beslismoment (reviews / retour-garantie / betaalmethode nabij de CTA) — PDP
  4. Checkout-frictie (veldenaantal, verplicht-account, stappen-indicator) — checkout
  5. Prijs-zichtbaarheid nabij de koopknop — PDP
  6. Urgentie-eerlijkheid (countdown/schaarste -> verifieer echtheid, veroordeel niet)
  7. Formulierlengte buiten checkout

Databron-lagen (tiers):
  A render_meta  – echte pixel-vouw uit computed styles (indien module 1.1 die levert)
  B rendered_dom – dom.html (gerenderde DOM na JS); vouw benaderd via DOM-volgorde
  C bare_html    – geen screenshots -> page-record-velden + page_texts (gemeld in summary)

Fail-soft: audit(ctx) laat nooit een exception ontsnappen (dubbel vangnet met de runner).
Geen netwerk. Imports: stdlib + bs4 (via helpers) + Pillow strikt optioneel (PNG-leegtecheck).
"""
from collections import Counter

try:
    from . import _cro_helpers as H
except Exception:  # directe (niet-package) import als vangnet
    import _cro_helpers as H

KEY = "cro"
LABEL = "Conversie-audit (CRO)"
ORDER = 20

# Gewichten per check (per toepasselijke pagina-instantie). PDP-pagina's tellen 2x
# (page-multiplier hieronder); binnen CTA-vouw weegt mobiel zwaarder dan desktop.
WEIGHTS = {
    "cta_fold": 3.0,
    "one_message": 1.5,
    "trust_at_decision": 2.0,
    "checkout_friction": 3.0,
    "price_visibility": 3.0,
    "urgency": 1.0,
    "form_length": 0.5,
}


# ===========================================================================
# Publieke entry — laatste vangnet
# ===========================================================================
def audit(ctx):
    try:
        return _run(ctx)
    except Exception as e:  # mag NOOIT ontsnappen
        try:
            ctx["log"].warning("cro-audit faalde zacht: %s", e)
        except Exception:
            pass
        return {"score": None,
                "summary": "Conversie-audit kon niet draaien (zachte fout, zie run.log).",
                "issues": [], "data": {"error": str(e)}}


def _warn(log, msg, *a):
    try:
        log.warning(msg, *a)
    except Exception:
        pass


# ===========================================================================
# Orkestratie
# ===========================================================================
def _run(ctx):
    pages = ctx.get("pages") or []
    out = ctx.get("out")
    page_texts = ctx.get("page_texts") or {}
    log = ctx.get("log")

    if not pages:
        return {"score": None, "summary": "Geen pagina's om te auditen.",
                "issues": [], "data": {"pages": 0}}

    per_page = []
    tiers = Counter()
    types = Counter()
    for idx, p in enumerate(pages):
        try:
            rec = _audit_page(p, out, page_texts, log, allow_png=(idx < H.MAX_PNG_SAMPLE))
        except Exception as e:  # per-pagina vangnet: één rare pagina mag de audit niet breken
            url = p.get("url", "?") if isinstance(p, dict) else "?"
            _warn(log, "cro: pagina-audit faalde zacht (%s): %s", url, e)
            rec = {"url": url if isinstance(url, str) else "", "page_type": "other",
                   "tier": "bare_html", "checks": {}, "png_mobile": None}
        per_page.append(rec)
        tiers[rec.get("tier", "bare_html")] += 1
        types[rec.get("page_type", "other")] += 1

    # ---- scoring: gewogen checklist over pagina's ----
    possible = 0.0
    achieved = 0.0
    for rec in per_page:
        mult = 2.0 if rec["page_type"] == "pdp" else 1.0
        for key, chk in rec["checks"].items():
            if not chk or not chk.get("applicable"):
                continue
            pf = chk.get("pass")
            if pf is None:  # toepasselijk maar niet meetbaar (kale HTML) -> telt niet mee in score
                continue
            w = WEIGHTS.get(key, 1.0) * mult
            possible += w
            achieved += w * max(0.0, min(1.0, float(pf)))
    score = round(100.0 * achieved / possible, 1) if possible > 0 else None

    # ---- issues + notes ----
    issues = _build_issues(per_page, log)

    dom_tier = "render_meta" if tiers.get("render_meta") else (
        "rendered_dom" if tiers.get("rendered_dom") else "bare_html")
    notes = _build_notes(per_page, types, tiers)

    data = {
        "tier_overall": dom_tier,
        "tier_counts": dict(tiers),
        "counts_by_type": dict(types),
        "weights": WEIGHTS,
        "score_basis": {"possible_weight": round(possible, 2), "achieved_weight": round(achieved, 2)},
        "pages": [_slim(rec) for rec in per_page],
        "note": " ".join(notes) if notes else "",
    }
    if len(pages) > H.MAX_PNG_SAMPLE:
        data["capped"] = {
            "png_mobile_fold_samples": H.MAX_PNG_SAMPLE,
            "reason": (f"crawl > {H.MAX_PNG_SAMPLE} pagina's; de (optionele) PNG-leegtecheck van de "
                       f"mobiele vouw draaide alleen op de eerste {H.MAX_PNG_SAMPLE} pagina's "
                       f"(DOM/tekst-checks draaiden op alle pagina's)."),
        }

    summary = _build_summary(score, issues, per_page, dom_tier, types)
    html = _build_html(per_page, score)

    result = {"score": score, "summary": summary, "issues": issues, "data": data}
    if html:
        result["html"] = html
    return result


# ===========================================================================
# Per-pagina audit
# ===========================================================================
def _audit_page(p, out, page_texts, log, allow_png):
    if not isinstance(p, dict):
        p = {}
    url = p.get("url") or ""
    ptype = H.page_type(p)
    sc = p.get("screenshots")
    if not isinstance(sc, dict):
        sc = {}
    ok = bool(sc.get("ok"))
    rec = {"url": url, "page_type": ptype, "tier": "bare_html", "checks": {},
           "png_mobile": None}

    soup = None
    vtext = ""
    render_meta = None
    class_blob = ""

    if ok:
        try:
            render_meta = H.load_render_meta(out, sc)
        except Exception as e:
            _warn(log, "cro: render_meta laden faalde (%s): %s", url, e)
            render_meta = None
        dom_rel = sc.get("dom")
        if dom_rel and out is not None:
            try:
                dpath = out / dom_rel
                if dpath.exists():
                    html = dpath.read_text(encoding="utf-8", errors="replace")
                    soup, vtext = H.make_soup(html)
            except Exception as e:
                _warn(log, "cro: dom.html laden faalde (%s): %s", url, e)
        if render_meta is not None:
            rec["tier"] = "render_meta"
        elif soup is not None:
            rec["tier"] = "rendered_dom"
        else:
            rec["tier"] = "bare_html"

    if not vtext:
        vtext = H.norm(page_texts.get(url) or "")

    # PNG-leegtecheck van de mobiele vouw voor ELKE pagina met screenshots (niet enkel home/pdp),
    # zodat een render-artefact site-breed wordt opgemerkt. Fail-soft; None als geen Pillow/opname.
    if ok and allow_png:
        try:
            rec["png_mobile"] = H.mobile_fold_state(out, sc)
        except Exception as e:
            _warn(log, "cro: mobiele fold-PNG check faalde (%s): %s", url, e)

    if soup is not None:
        try:
            parts = []
            for el in (soup.body or soup).find_all(True):
                c = el.get("class")
                if c:
                    parts.append(" ".join(c))
            class_blob = " ".join(parts)
        except Exception:
            class_blob = ""

    C = rec["checks"]
    # 1. CTA boven de vouw (home/pdp)
    if ptype in ("home", "pdp"):
        C["cta_fold"] = _safe(_check_cta_fold, log, ptype, soup, render_meta, rec)
    # 2. een boodschap per viewport (alle)
    C["one_message"] = _safe(_check_one_message, log, soup, render_meta, p)
    # 3. trust op beslismoment (pdp)
    if ptype == "pdp":
        C["trust_at_decision"] = _safe(_check_trust, log, soup, vtext, p)
    # 4. checkout-frictie (checkout)
    if ptype == "checkout":
        C["checkout_friction"] = _safe(_check_checkout, log, soup, vtext, class_blob)
    # 5. prijs-zichtbaarheid (pdp)
    if ptype == "pdp":
        C["price_visibility"] = _safe(_check_price, log, soup, vtext, render_meta, p)
    # 6. urgentie-eerlijkheid (alle)
    C["urgency"] = _safe(_check_urgency, log, soup, vtext, class_blob)
    # 7. formulierlengte buiten checkout
    if ptype != "checkout":
        C["form_length"] = _safe(_check_form_length, log, soup)

    return rec


def _safe(fn, log, *args):
    """Draai een check fail-soft; bij fout -> niet-meetbaar (telt niet in score)."""
    try:
        return fn(*args)
    except Exception as e:
        _warn(log, "cro: check %s faalde zacht: %s", getattr(fn, "__name__", "?"), e)
        return {"applicable": True, "measurable": False, "pass": None,
                "detail": "check faalde zacht (zie run.log)"}


# ===========================================================================
# De checks
# ===========================================================================
def _check_cta_fold(ptype, soup, render_meta, rec):
    if render_meta is not None:
        d_ok = H.rm_cta_in_fold(render_meta, "desktop")
        m_ok = H.rm_cta_in_fold(render_meta, "mobile")
        method = "render_meta (echte pixel-vouw)"
    elif soup is not None:
        top, det = H.cta_in_top_zone(soup)
        d_ok = m_ok = top
        method = "DOM-volgorde-benadering (geen computed-style-coordinaten in deze render)"
    else:
        return {"applicable": True, "measurable": False, "pass": None,
                "detail": "Geen render/DOM; CTA-vouwpositie niet meetbaar op kale HTML."}

    png = rec.get("png_mobile")  # al gesampled in _audit_page (voor elke pagina)
    mobile_note = ""
    if png and png.get("blank"):
        if png.get("artifact"):
            mobile_note = ("mobiele fold-opname was leeg terwijl de mobiele full-page WEL content toont "
                           "=> capture-artefact; mobiele zichtbaarheid onbevestigd")
        else:
            m_ok = False
            mobile_note = ("mobiele fold-opname (vrijwel) leeg EN de full-page-top toont ook geen content "
                           "=> mogelijk lege/te trage mobiele hero")

    d = 1.0 if d_ok else 0.0
    m = 1.0 if m_ok else 0.0
    if m_ok and png and png.get("blank") and png.get("artifact"):
        m = 0.5  # onbevestigd (capture-artefact): niet vol tellen, maar de site niet verwijten
    passf = (1.0 * d + 2.0 * m) / 3.0  # mobiel weegt zwaarder

    detail = f"desktop-CTA={'ja' if d_ok else 'NEE'}, mobiel-CTA={'ja' if m_ok else 'NEE'} [{method}]"
    if mobile_note:
        detail += f"; {mobile_note}"
    return {"applicable": True, "measurable": True, "pass": passf,
            "desktop_ok": bool(d_ok), "mobile_ok": bool(m_ok), "method": method,
            "mobile_png_blank": bool(png and png.get("blank")),
            "mobile_png_artifact": bool(png and png.get("artifact")),
            "detail": detail}


def _check_one_message(soup, render_meta, pdata):
    if render_meta is not None:
        cnt = H.rm_hero_message_count(render_meta, "mobile") or H.rm_hero_message_count(render_meta, "desktop")
        examples, method, degraded = [], "render_meta (boodschappen in de vouw)", False
    elif soup is not None:
        cnt, examples, bounded = H.hero_messages(soup)
        method = "DOM-heldensectie (tot de eerste h2)"
        degraded = False
        if bounded:
            method += " [geen h2 gevonden; scan begrensd]"
    elif pdata is not None and pdata.get("h1_count") is not None:
        cnt = int(pdata.get("h1_count") or 0)
        examples, method, degraded = [], "page-record h1_count (kale HTML)", True
        over = max(0, cnt - 1)  # >1 h1 = concurrerende hoofdboodschappen
        passf = 1.0 if over == 0 else max(0.0, 1.0 - over * 0.34)
        return {"applicable": True, "measurable": True, "pass": passf, "count": cnt,
                "examples": [], "degraded": True,
                "detail": f"{cnt} h1 op de pagina [{method}]"}
    else:
        return {"applicable": True, "measurable": False, "pass": None,
                "detail": "Geen render/DOM/h1-data; boodschap-dichtheid niet meetbaar."}

    over = max(0, cnt - H.HERO_MSG_LIMIT)
    passf = 1.0 if over == 0 else max(0.0, 1.0 - over * 0.25)
    return {"applicable": True, "measurable": True, "pass": passf, "count": cnt,
            "examples": examples, "degraded": degraded,
            "detail": f"{cnt} boodschap-elementen in de heldensectie [{method}]"}


def _check_trust(soup, vtext, pdata):
    signals = {"reviews": False, "retour_garantie": False, "betaalmethode": False}
    if soup is not None:
        cta = H.primary_buy_cta(soup)
        if cta is not None:
            ctx_text = H.neighbourhood_text(cta)
            method = "nabij de primaire CTA (DOM-omgeving)"
        else:
            ctx_text = vtext
            method = "paginabreed (geen primaire koop-CTA gevonden)"
    else:
        ctx_text = vtext
        method = "paginabreed op kale HTML (nabijheid tot CTA niet meetbaar)"

    signals["reviews"] = bool(H.TRUST_REVIEW_RE.search(ctx_text))
    signals["retour_garantie"] = bool(H.TRUST_RETURN_RE.search(ctx_text))
    signals["betaalmethode"] = bool(H.TRUST_PAY_RE.search(ctx_text))
    # page-record 'reviews' als extra bron voor het reviews-signaal
    try:
        if not signals["reviews"] and (pdata.get("reviews") or []):
            signals["reviews"] = True
    except Exception:
        pass

    present = sum(1 for v in signals.values() if v)
    passf = present / 3.0
    missing = [k for k, v in signals.items() if not v]
    return {"applicable": True, "measurable": True, "pass": passf, "signals": signals,
            "missing": missing, "method": method,
            "detail": f"{present}/3 trust-signalen {method}; ontbreekt: {', '.join(missing) or 'niets'}"}


def _check_checkout(soup, vtext, class_blob):
    account_required = bool(H.ACCOUNT_REQ_RE.search(vtext))
    if soup is not None:
        counts = H.forms_with_fieldcounts(soup)
        fields = sum(counts) if counts else H.count_form_fields(soup)
        steps = bool(H.STEP_RE.search(class_blob or ""))
        measurable = True
    else:
        fields = None
        steps = None
        measurable = False

    passf = 1.0
    if account_required:
        passf -= 0.7
    if fields is not None and fields > H.CHECKOUT_FIELDS_MAX:
        passf -= 0.4
    passf = max(0.0, passf)

    detail = []
    if fields is not None:
        detail.append(f"{fields} zichtbare velden")
        detail.append("stappen-indicator: " + ("ja" if steps else "nee"))
    else:
        detail.append("velden niet meetbaar (kale HTML)")
    if account_required:
        detail.append("verplicht account gedetecteerd")

    return {"applicable": True, "measurable": True, "pass": passf,
            "fields": fields, "steps": steps, "account_required": account_required,
            "detail": "; ".join(detail)}


def _check_price(soup, vtext, render_meta, pdata):
    if render_meta is not None:
        fold_txt = " ".join(
            (t.get("text") or "") for t in
            (H.rm_fold_elems(render_meta, "mobile", "texts") + H.rm_fold_elems(render_meta, "desktop", "texts")))
        in_fold = bool(H.PRICE_RE.search(fold_txt))
        passf = 1.0 if in_fold else 0.0
        return {"applicable": True, "measurable": True, "pass": passf, "near_cta": in_fold,
                "method": "render_meta (prijs in de vouw)",
                "detail": f"prijs in de vouw: {'ja' if in_fold else 'NEE'} [render_meta]"}
    if soup is not None:
        cta = H.primary_buy_cta(soup)
        ctx_text = H.neighbourhood_text(cta) if cta is not None else vtext
        near = bool(H.PRICE_RE.search(ctx_text))
        anywhere = bool(H.PRICE_RE.search(vtext))
        method = "nabij de primaire CTA (DOM-omgeving)" if cta is not None else "paginabreed (geen CTA gevonden)"
        passf = 1.0 if near else (0.4 if anywhere else 0.0)
        return {"applicable": True, "measurable": True, "pass": passf, "near_cta": near,
                "price_on_page": anywhere, "method": method,
                "detail": f"prijs {'bij CTA' if near else ('elders op de pagina' if anywhere else 'NIET gevonden')} [{method}]"}
    # tier C
    anywhere = bool(H.PRICE_RE.search(vtext))
    # page-record prijsvelden als extra bron
    try:
        if not anywhere and (pdata.get("sale_price") or pdata.get("compare_at_price")):
            anywhere = True
    except Exception:
        pass
    passf = 0.8 if anywhere else 0.0
    return {"applicable": True, "measurable": True, "pass": passf, "near_cta": None,
            "price_on_page": anywhere, "method": "kale HTML (plaatsing niet meetbaar)",
            "detail": f"prijs op de pagina: {'ja' if anywhere else 'NEE'} (plaatsing t.o.v. CTA niet meetbaar)"}


def _check_urgency(soup, vtext, class_blob):
    text_hits = H.URGENCY_TEXT_RE.findall(vtext or "")
    class_hit = bool(H.URGENCY_TIMER_CLASS_RE.search(class_blob or ""))
    found = bool(text_hits) or class_hit
    snippet = ""
    m = H.URGENCY_TEXT_RE.search(vtext or "")
    if m:
        i = max(0, m.start() - 30)
        snippet = H.norm((vtext or "")[i:m.end() + 30])
    elif class_hit:
        snippet = "timer/countdown-class in de opmaak"
    passf = 1.0 if not found else 0.6  # caution: verifieer echtheid, niet automatisch veroordelen
    return {"applicable": True, "measurable": True, "pass": passf, "found": found,
            "timer_class": class_hit, "n_text_hits": len(text_hits), "snippet": snippet[:120],
            "detail": ("geen urgentie/schaarste-taal gevonden" if not found
                       else f"urgentie gevonden (verifieer echtheid): {snippet[:80]}")}


def _check_form_length(soup):
    if soup is None:
        return {"applicable": True, "measurable": False, "pass": None,
                "detail": "Geen DOM; formulierlengte niet meetbaar op kale HTML."}
    counts = H.forms_with_fieldcounts(soup)
    longest = max(counts) if counts else 0
    over = [c for c in counts if c > H.FORM_FIELDS_MAX]
    passf = 1.0 if not over else 0.5
    return {"applicable": True, "measurable": True, "pass": passf,
            "form_field_counts": counts, "longest": longest,
            "detail": f"{len(counts)} formulier(en); langste heeft {longest} velden"}


# ===========================================================================
# Issues (consolideren over pagina's) — NL, concreet, uitvoerbaar
# ===========================================================================
def _urls(recs, limit=6):
    us = [r["url"] for r in recs if r.get("url")]
    shown = us[:limit]
    extra = len(us) - len(shown)
    s = ", ".join(shown)
    if extra > 0:
        s += f" (+{extra} meer)"
    return s, (us[0] if us else "")


def _build_issues(per_page, log):
    issues = []
    try:
        # -- 1a. Geen enkele CTA in de vouw (echt probleem) --
        no_cta = [r for r in per_page
                  if (c := r["checks"].get("cta_fold")) and c.get("measurable")
                  and not c.get("desktop_ok") and not c.get("mobile_ok")
                  and not (r.get("png_mobile") or {}).get("artifact")]
        if no_cta:
            urls, first = _urls(no_cta)
            issues.append({
                "severity": "High", "category": "cro",
                "title": f"Geen koop-CTA boven de vouw op {len(no_cta)} PDP/home-pagina('s)",
                "why": ("Bezoekers (met name mobiel, >60% van het verkeer) zien geen call-to-action "
                        "zonder te scrollen; dat kost direct conversie. Pagina's: " + urls),
                "fix": ("Zet een primaire CTA (bijv. 'In winkelwagen'/'Vraag offerte aan') binnen de "
                        "eerste 844px op mobiel — kort de hero in of voeg een sticky CTA-balk toe."),
                "url": first,
            })
        # -- 1b. Mobiele hero lijkt echt leeg (blank, geen artefact) --
        empty_hero = [r for r in per_page
                      if (pm := r.get("png_mobile")) and pm.get("blank") and not pm.get("artifact")]
        if empty_hero:
            urls, first = _urls(empty_hero)
            issues.append({
                "severity": "High", "category": "cro",
                "title": f"Mobiele vouw (vrijwel) leeg op {len(empty_hero)} pagina('s)",
                "why": ("De mobiele above-the-fold-opname is nagenoeg wit en ook de mobiele full-page-top "
                        "toont geen content: de eerste indruk op mobiel is een lege pagina. Pagina's: " + urls),
                "fix": ("Laad de hero direct (geen te grote header/spacer, geen hero-afbeelding lazy-loaden "
                        "boven de vouw) zodat mobiel meteen inhoud + CTA ziet."),
                "url": first,
            })
        # -- 1c. Mobiele fold-opname leeg door capture-artefact (data-kwaliteit + verifieer) --
        artifact = [r for r in per_page
                    if (pm := r.get("png_mobile")) and pm.get("blank") and pm.get("artifact")]
        if artifact:
            urls, first = _urls(artifact)
            issues.append({
                "severity": "Medium", "category": "cro",
                "title": f"Mobiele vouw-opname onbetrouwbaar (leeg) op {len(artifact)} pagina('s)",
                "why": ("De mobiele fold-screenshot is volledig wit terwijl de mobiele full-page WEL content "
                        "bevat — dat wijst op een render-timing-artefact van de screenshot-stap (module 1.1), "
                        "niet per se op een lege site. Hierdoor kon de mobiele CTA-zichtbaarheid niet visueel "
                        "worden bevestigd. Pagina's: " + urls),
                "fix": ("Verifieer de mobiele vouw op een echt toestel/emulator. Structureel: laat module 1.1 "
                        "de mobiele fold-opname pas na 'first paint'/networkidle maken (zie wiring-notitie cro.md)."),
                "url": first,
            })

        # -- 2. Te veel boodschappen in de heldensectie --
        busy = [r for r in per_page
                if (c := r["checks"].get("one_message")) and c.get("measurable")
                and (c.get("count") or 0) > H.HERO_MSG_LIMIT and not c.get("degraded")]
        if busy:
            urls, first = _urls(busy)
            mx = max((r["checks"]["one_message"].get("count") or 0) for r in busy)
            issues.append({
                "severity": "Medium", "category": "cro",
                "title": f"Te veel concurrerende boodschappen in de vouw ({len(busy)} pagina('s), tot {mx})",
                "why": ("Conversie-principe: 1 boodschap per viewport. Meerdere grote koppen/badges tegelijk "
                        "verdelen de aandacht en verlagen de klik op de primaire CTA. Pagina's: " + urls),
                "fix": ("Behoud 1 hoofdboodschap + 1 primaire CTA boven de vouw; verplaats secundaire "
                        "badges/claims naar lager op de pagina."),
                "url": first,
            })

        # -- 3. Trust ontbreekt bij het beslismoment (PDP) --
        weak_trust = [r for r in per_page
                      if (c := r["checks"].get("trust_at_decision")) and c.get("measurable")
                      and c.get("missing")]
        if weak_trust:
            urls, first = _urls(weak_trust)
            allmiss = Counter()
            for r in weak_trust:
                for m in r["checks"]["trust_at_decision"].get("missing", []):
                    allmiss[m] += 1
            miss_txt = ", ".join(f"{k} ({v}x)" for k, v in allmiss.most_common())
            issues.append({
                "severity": "Medium", "category": "cro",
                "title": f"Trust-signalen ontbreken bij de koopknop op {len(weak_trust)} PDP('s)",
                "why": ("Op het beslismoment (nabij de CTA) ontbreken geruststellende signalen. "
                        f"Meest gemist: {miss_txt}. Pagina's: " + urls),
                "fix": ("Toon dicht bij de koopknop: sterren/reviews, retour-/garantietekst en betaalmethoden "
                        "(iDEAL/Klarna/veilig betalen)."),
                "url": first,
            })

        # -- 4. Checkout-frictie --
        acc = [r for r in per_page
               if (c := r["checks"].get("checkout_friction")) and c.get("account_required")]
        if acc:
            urls, first = _urls(acc)
            issues.append({
                "severity": "High", "category": "cro",
                "title": f"Verplicht account om te bestellen ({len(acc)} checkout-pagina('s))",
                "why": ("Een gedwongen accountaanmaak is een klassieke conversiekiller in de checkout. "
                        "Pagina's: " + urls),
                "fix": "Bied gast-checkout aan; maak account-aanmaken optioneel (ná de bestelling).",
                "url": first,
            })
        many_fields = [r for r in per_page
                       if (c := r["checks"].get("checkout_friction")) and c.get("fields") is not None
                       and c["fields"] > H.CHECKOUT_FIELDS_MAX]
        if many_fields:
            urls, first = _urls(many_fields)
            mx = max(r["checks"]["checkout_friction"]["fields"] for r in many_fields)
            issues.append({
                "severity": "Medium", "category": "cro",
                "title": f"Veel invulvelden in de checkout (tot {mx}, drempel {H.CHECKOUT_FIELDS_MAX})",
                "why": ("Elk extra veld verhoogt de afhaakkans. Pagina's: " + urls),
                "fix": ("Beperk tot het strikt noodzakelijke; gebruik autofill/postcode-lookup en splits "
                        "over duidelijke stappen."),
                "url": first,
            })

        # -- 5. Prijs niet zichtbaar bij de CTA (PDP) --
        price_far = [r for r in per_page
                     if (c := r["checks"].get("price_visibility")) and c.get("measurable")
                     and (c.get("pass") or 0) < 1.0]
        if price_far:
            absent = [r for r in price_far if not r["checks"]["price_visibility"].get("price_on_page")]
            urls, first = _urls(price_far)
            sev = "High" if absent else "Medium"
            issues.append({
                "severity": sev, "category": "cro",
                "title": f"Prijs niet in beeld bij de koopknop op {len(price_far)} PDP('s)",
                "why": ("Conversie-principe: de prijs is nooit meer dan één blik van de koopknop verwijderd. "
                        + (f"{len(absent)} pagina('s) tonen zelfs helemaal geen prijs. " if absent else "")
                        + "Pagina's: " + urls),
                "fix": ("Plaats de prijs (en eventuele vanaf-prijs) direct naast/boven de 'In winkelwagen'-knop, "
                        "ook zichtbaar in de mobiele vouw."),
                "url": first,
            })

        # -- 6. Urgentie/schaarste — verifieer echtheid (niet veroordelen) --
        urg = [r for r in per_page
               if (c := r["checks"].get("urgency")) and c.get("found")]
        if urg:
            urls, first = _urls(urg)
            sample = next((r["checks"]["urgency"].get("snippet") for r in urg
                           if r["checks"]["urgency"].get("snippet")), "")
            issues.append({
                "severity": "Medium", "category": "cro",
                "title": f"Urgentie/schaarste aangetroffen op {len(urg)} pagina('s) — verifieer echtheid",
                "why": ("Countdown-timers of schaarste-taal ('nog maar X', 'op=op', 'laatste kans') verhogen "
                        "conversie alleen als ze ECHT zijn; nep-urgentie schaadt vertrouwen en is in strijd met "
                        "de ACM-regels. Voorbeeld: \"" + (sample or "timer-class in de opmaak") + "\". Pagina's: " + urls),
                "fix": ("Onderbouw elke urgentie met echte data (echte voorraad/echte einddatum) of verwijder "
                        "'m. Laat een timer niet resetten bij herladen."),
                "url": first,
            })

        # -- 7. Lang formulier buiten checkout --
        longform = [r for r in per_page
                    if (c := r["checks"].get("form_length")) and c.get("measurable")
                    and (c.get("longest") or 0) > H.FORM_FIELDS_MAX]
        if longform:
            urls, first = _urls(longform)
            mx = max(r["checks"]["form_length"]["longest"] for r in longform)
            issues.append({
                "severity": "Low", "category": "cro",
                "title": f"Lang formulier buiten de checkout (tot {mx} velden)",
                "why": ("Lange formulieren (bijv. offerte/contact) drukken de invulratio. Pagina's: " + urls),
                "fix": ("Vraag alleen het hoognodige uit; maak overige velden optioneel of verplaats ze naar "
                        "een vervolgstap."),
                "url": first,
            })
    except Exception as e:
        _warn(log, "cro: issue-opbouw faalde zacht: %s", e)
    return issues


# ===========================================================================
# Notes, summary, slim data, html
# ===========================================================================
def _build_notes(per_page, types, tiers):
    notes = []
    if not types.get("pdp"):
        notes.append("Geen productpagina's (PDP) in deze crawl herkend; prijs- en trust-op-beslismoment-"
                     "checks zijn niet uitgevoerd (geen issue).")
    if not types.get("checkout"):
        notes.append("Geen winkelwagen/checkout-pagina's gecrawld; checkout-frictie is niet gemeten "
                     "(geen issue).")
    if tiers.get("render_meta"):
        pass
    elif tiers.get("rendered_dom"):
        notes.append("Gemeten op de gerenderde DOM (dom.html); de vouwpositie is benaderd via DOM-volgorde "
                     "omdat deze render geen computed-style-coordinaten (render_meta) bevat.")
    else:
        notes.append("Meting op kale HTML (geen bruikbare screenshots/DOM in deze run) — visuele vouw-checks "
                     "konden niet draaien; alleen tekst/veld-gebaseerde signalen zijn beoordeeld.")
    return notes


def _build_summary(score, issues, per_page, dom_tier, types):
    n = len(per_page)
    hi = sum(1 for i in issues if i.get("severity") in ("Critical", "High"))
    tierword = {"render_meta": "echte render", "rendered_dom": "gerenderde DOM",
                "bare_html": "KALE HTML (gedegradeerd)"}.get(dom_tier, dom_tier)
    sc = "n.v.t." if score is None else str(score)
    base = f"CRO-score {sc}/100 over {n} pagina('s) (meting: {tierword}); {len(issues)} verbeterpunt(en)"
    if hi:
        base += f", waarvan {hi} hoog/kritiek"
    return base + "."


def _slim(rec):
    """Machine-leesbare, JSON-veilige per-pagina samenvatting."""
    out = {"url": rec["url"], "page_type": rec["page_type"], "tier": rec["tier"], "checks": {}}
    for k, c in rec["checks"].items():
        if not isinstance(c, dict):
            continue
        slim = {kk: vv for kk, vv in c.items()
                if kk in ("applicable", "measurable", "pass", "detail", "count", "signals",
                          "missing", "near_cta", "price_on_page", "fields", "steps",
                          "account_required", "desktop_ok", "mobile_ok", "found", "timer_class",
                          "longest", "form_field_counts", "degraded", "mobile_png_blank",
                          "mobile_png_artifact", "method", "examples")}
        if isinstance(slim.get("pass"), float):
            slim["pass"] = round(slim["pass"], 3)
        out["checks"][k] = slim
    if rec.get("png_mobile"):
        out["png_mobile"] = rec["png_mobile"]
    return out


def _cell(rec, key):
    c = rec["checks"].get(key)
    if not c:
        return '<td style="padding:4px 8px;color:#9aa;">n.v.t.</td>'
    if not c.get("measurable"):
        return '<td style="padding:4px 8px;color:#c90;">? </td>'
    pf = c.get("pass")
    if pf is None:
        return '<td style="padding:4px 8px;color:#9aa;">–</td>'
    if pf >= 0.999:
        col, mark = "#137333", "&#10003;"
    elif pf >= 0.5:
        col, mark = "#b26b00", "&#9679;"
    else:
        col, mark = "#c5221f", "&#10007;"
    return f'<td style="padding:4px 8px;color:{col};font-weight:600;text-align:center;">{mark}</td>'


def _build_html(per_page, score):
    try:
        rows = []
        cap = 40
        shown = per_page[:cap]
        for r in shown:
            path = r["url"]
            try:
                from urllib.parse import urlparse
                path = urlparse(r["url"]).path or "/"
            except Exception:
                pass
            tier = {"render_meta": "render", "rendered_dom": "DOM", "bare_html": "kale HTML"}.get(r["tier"], r["tier"])
            rows.append(
                '<tr style="border-top:1px solid #eee;">'
                f'<td style="padding:4px 8px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{path}</td>'
                f'<td style="padding:4px 8px;color:#555;">{r["page_type"]}</td>'
                f'<td style="padding:4px 8px;color:#555;">{tier}</td>'
                + _cell(r, "cta_fold") + _cell(r, "one_message") + _cell(r, "price_visibility")
                + _cell(r, "trust_at_decision") + _cell(r, "checkout_friction")
                + _cell(r, "urgency") + _cell(r, "form_length")
                + '</tr>')
        extra = len(per_page) - len(shown)
        cap_note = (f'<div style="color:#888;font-size:12px;margin-top:6px;">Tabel toont de eerste {cap} '
                    f'van {len(per_page)} pagina\'s (+{extra} niet getoond).</div>') if extra > 0 else ""
        head = ("".join(f'<th style="padding:4px 8px;text-align:left;font-weight:600;color:#333;">{h}</th>'
                        for h in ["Pagina", "Type", "Bron", "CTA-vouw", "1 boodschap", "Prijs",
                                  "Trust", "Checkout", "Urgentie", "Formulier"]))
        return (
            '<div style="font-family:system-ui,Arial,sans-serif;font-size:13px;overflow-x:auto;">'
            '<table style="border-collapse:collapse;min-width:720px;">'
            f'<thead><tr style="background:#fafafa;">{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
            '<div style="color:#888;font-size:12px;margin-top:6px;">'
            '&#10003; = op orde &nbsp; &#9679; = deels/aandacht &nbsp; &#10007; = probleem &nbsp; '
            '&ndash; = niet meetbaar (kale HTML) &nbsp; n.v.t. = niet van toepassing voor dit paginatype</div>'
            f'{cap_note}</div>')
    except Exception:
        return ""
