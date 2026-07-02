#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEO-SCORE 0-100 per pagina + per site.

Een gewogen, transparante meetlat over de gevulde/correcte velden die de scraper
al produceert (title/meta/headings/canonical/indexability/schema/CWV/links/images/
content/technisch/e-commerce). Per pagina komt er naast het cijfer een lijst
CONCRETE fixes (Critical/High/Medium/Low) met telkens WAAROM het meetelt.

Ontwerp:
- 100% fail-soft: elke veld-lookup heeft een default; nooit een exception naar buiten.
- Categorieen hebben een gewicht. Niet-toepasselijke categorieen (CWV zonder PSI,
  e-commerce op niet-product-pagina's, afbeeldingen op een pagina zonder afbeeldingen)
  vallen uit teller EN noemer -> het cijfer blijft eerlijk 0-100.
- Het cijfer is dezelfde meetlat waarmee je een site naar 100% brengt: elke afgetrokken
  punt heeft een bijbehorende, uitvoerbare fix.

Geen externe deps (alleen stdlib).
"""
from __future__ import annotations

# ---- severity-volgorde (voor sorteren/aggregeren) ---------------------------
SEV_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


def _num(v, default=0.0):
    try:
        if isinstance(v, bool):
            return float(v)
        return float(v)
    except Exception:
        return default


def _g(rec, key, default=None):
    try:
        v = rec.get(key, default)
        return default if v is None else v
    except Exception:
        return default


def _clamp01(x):
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


class _Cat:
    """Een scorecategorie verzamelt deel-checks (elk met een lokaal gewicht 0..1)."""
    __slots__ = ("key", "label", "weight", "applicable", "_checks", "_issues")

    def __init__(self, key, label, weight, applicable=True):
        self.key = key
        self.label = label
        self.weight = float(weight)
        self.applicable = bool(applicable)
        self._checks = []          # (local_weight, ratio0_1)
        self._issues = []          # dicts

    def check(self, local_weight, ratio, *, sev=None, title="", why="", fix="", field=""):
        """Registreer een deel-check. ratio<1 met sev != None -> issue."""
        ratio = _clamp01(_num(ratio))
        self._checks.append((float(local_weight), ratio))
        if sev and ratio < 0.999 and title:
            self._issues.append({
                "severity": sev, "category": self.label, "title": title,
                "why": why, "fix": fix, "field": field,
                "impact_points": None,  # ingevuld bij finalize
            })

    def finalize(self):
        tw = sum(w for w, _ in self._checks) or 1.0
        ratio = sum(w * r for w, r in self._checks) / tw
        earned = round(ratio * self.weight, 2)
        # verdeel het puntverlies van de categorie over de open issues (indicatief)
        lost = self.weight - earned
        if self._issues and lost > 0:
            per = round(lost / len(self._issues), 2)
            for it in self._issues:
                it["impact_points"] = per
        return {
            "key": self.key, "label": self.label, "weight": round(self.weight, 2),
            "applicable": self.applicable, "ratio": round(ratio, 3),
            "earned": earned, "lost": round(lost, 2),
        }, self._issues


def grade_for(score):
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 70: return "C"
    if score >= 60: return "D"
    if score >= 50: return "E"
    return "F"


# ============================================================================ #
def score_page(rec, dup_titles=frozenset(), dup_descs=frozenset(), psi_enabled=False):
    """Score 1 pagina. Returnt een dict met score/grade/breakdown/issues.
    `dup_titles`/`dup_descs` = (lowercased) titles/descriptions die >1x voorkomen."""
    try:
        return _score_page_inner(rec, dup_titles, dup_descs, psi_enabled)
    except Exception as e:  # absolute vangnet — score mag NOOIT de crawl breken
        return {"score": None, "grade": "?", "error": str(e)[:200],
                "breakdown": [], "issues": []}


def _score_page_inner(rec, dup_titles, dup_descs, psi_enabled):
    cats = []

    # ---- 1. TITLE (14) ------------------------------------------------------
    c = _Cat("title", "Title", 14)
    title = str(_g(rec, "title", "")).strip()
    tlen = int(_num(_g(rec, "title_length", len(title))))
    has = 1.0 if title else 0.0
    c.check(0.45, has, sev="Critical" if not title else None,
            title="Title ontbreekt",
            why="De <title> is het sterkste on-page rankingsignaal en de klikbare kop in Google.",
            fix="Geef de pagina een unieke title van 30-60 tekens met het primaire keyword vooraan.",
            field="title")
    if title:
        if 30 <= tlen <= 60: lr = 1.0
        elif 15 <= tlen <= 70: lr = 0.5
        else: lr = 0.0
        sev = None
        if tlen < 30:
            sev, msg, fix = "Medium", f"Title te kort ({tlen} tekens)", "Maak de title 30-60 tekens en benut de ruimte voor keyword + propositie."
        elif _g(rec, "title_truncation_risk") or tlen > 60:
            sev, msg, fix = "Medium", f"Title wordt afgekapt in de SERP ({tlen} tekens)", "Kort de title in tot ~60 tekens zodat Google hem niet afkapt."
        else:
            msg = fix = ""
        c.check(0.30, lr, sev=sev, title=msg, why="Titles buiten 30-60 tekens worden afgekapt of benutten de SERP-ruimte niet.", fix=fix, field="title_length")
        uniq = 0.0 if title.lower() in dup_titles else 1.0
        c.check(0.15, uniq, sev="High" if uniq < 1 else None,
                title="Title is niet uniek (komt op meerdere pagina's voor)",
                why="Dubbele titles laten Google de verkeerde pagina kiezen en kannibaliseren rankings.",
                fix="Schrijf per pagina een unieke title.", field="title")
        c.check(0.10, 0.0 if _g(rec, "title_truncation_risk") else 1.0)
    cats.append(c)

    # ---- 2. META DESCRIPTION (9) -------------------------------------------
    c = _Cat("meta_description", "Meta description", 9)
    desc = str(_g(rec, "meta_description", "")).strip()
    dlen = int(_num(_g(rec, "meta_description_length", len(desc))))
    c.check(0.50, 1.0 if desc else 0.0, sev="High" if not desc else None,
            title="Meta description ontbreekt",
            why="Geen direct rankingsignaal, maar bepaalt de SERP-snippet en dus de CTR (indirecte ranking).",
            fix="Schrijf een wervende, unieke description van 70-160 tekens met het keyword en een CTA.",
            field="meta_description")
    if desc:
        if 70 <= dlen <= 160: lr = 1.0
        elif 50 <= dlen <= 175: lr = 0.5
        else: lr = 0.0
        sev = None; msg = fix = ""
        if dlen < 70:
            sev, msg, fix = "Medium", f"Description te kort ({dlen} tekens)", "Verleng tot 70-160 tekens; benut de snippet-ruimte voor propositie + CTA."
        elif _g(rec, "description_truncation_risk") or dlen > 160:
            sev, msg, fix = "Medium", f"Description wordt afgekapt ({dlen} tekens)", "Kort in tot ~155 tekens zodat de snippet compleet getoond wordt."
        c.check(0.30, lr, sev=sev, title=msg, why="Te korte of afgekapte descriptions verlagen de klikkans.", fix=fix, field="meta_description_length")
        uniq = 0.0 if desc.lower() in dup_descs else 1.0
        c.check(0.20, uniq, sev="Medium" if uniq < 1 else None,
                title="Description is niet uniek", why="Dubbele descriptions verzwakken de SERP-presentatie van elke betrokken pagina.",
                fix="Geef elke pagina een eigen description.", field="meta_description")
    cats.append(c)

    # ---- 3. HEADINGS (9) ----------------------------------------------------
    c = _Cat("headings", "Headings", 9)
    h1c = int(_num(_g(rec, "h1_count", 0)))
    hi = _g(rec, "heading_issues", {}) or {}
    if h1c == 1: r = 1.0; sev = None; msg = fix = ""
    elif h1c == 0: r = 0.0; sev = "Critical"; msg = "Geen H1 op de pagina"; fix = "Voeg precies 1 H1 toe met het hoofdonderwerp/keyword."
    else: r = 0.3; sev = "High"; msg = f"{h1c} H1's op de pagina"; fix = "Houd precies 1 H1 aan; maak de rest H2/H3."
    c.check(0.50, r, sev=sev, title=msg, why="Google verwacht 1 duidelijke H1 als hoofdonderwerp van de pagina.", fix=fix, field="h1_count")
    empty = int(_num(hi.get("empty_headings_count", 0)))
    c.check(0.25, 1.0 if empty == 0 else 0.4, sev="Low" if empty else None,
            title=f"{empty} lege heading(s)" if empty else "",
            why="Lege headings verstoren de documentstructuur en de toegankelijkheid.",
            fix="Verwijder lege heading-tags of vul ze met betekenisvolle tekst.", field="heading_issues")
    nonseq = bool(hi.get("non_sequential"))
    c.check(0.25, 0.0 if nonseq else 1.0, sev="Low" if nonseq else None,
            title="Heading-niveaus springen (niet sequentieel)" if nonseq else "",
            why="Een nette H1>H2>H3-hierarchie helpt Google en screenreaders de structuur te volgen.",
            fix="Sla geen niveaus over (geen H3 zonder voorafgaande H2).", field="heading_issues")
    cats.append(c)

    # ---- 4. INDEXABILITY / CANONICAL / HTTPS (12) ---------------------------
    c = _Cat("indexability", "Indexeerbaarheid", 12)
    idx = _g(rec, "indexability", True)
    idx_ok = 1.0 if (idx is True or idx is None) else 0.0
    reason = str(_g(rec, "indexability_reason", "") or "")
    c.check(0.40, idx_ok, sev="Critical" if idx_ok < 1 else None,
            title=f"Pagina is niet-indexeerbaar ({reason or 'noindex/canonical/robots'})",
            why="Een niet-indexeerbare pagina kan per definitie NIET ranken — dit is de hoogste prioriteit.",
            fix="Verwijder de noindex/blokkade als de pagina hoort te ranken (check meta robots, X-Robots-Tag, robots.txt, canonical).",
            field="indexability")
    https = bool(_g(rec, "https", str(_g(rec, "url", "")).startswith("https")))
    c.check(0.20, 1.0 if https else 0.0, sev="Critical" if not https else None,
            title="Pagina niet over HTTPS", why="HTTPS is een (licht) rankingsignaal en een vertrouwens-/beveiligingseis.",
            fix="Serveer de pagina over HTTPS en redirect HTTP 301 naar HTTPS.", field="https")
    canon = str(_g(rec, "canonical", "") or "")
    c.check(0.20, 1.0 if canon else 0.0, sev="Medium" if not canon else None,
            title="Geen canonical-tag", why="Een canonical voorkomt duplicate-content-verwarring en bundelt linkwaarde.",
            fix="Voeg <link rel=canonical> toe die naar de voorkeurs-URL wijst.", field="canonical")
    cc = _g(rec, "canonical_conflict", {}) or {}
    conflict = bool(cc.get("conflict"))
    c.check(0.20, 0.0 if conflict else 1.0, sev="High" if conflict else None,
            title="Canonical conflicteert (met og:url / zelf-referentie)" if conflict else "",
            why="Een tegenstrijdige canonical kan Google de verkeerde URL laten indexeren.",
            fix="Laat canonical en og:url naar dezelfde, juiste URL wijzen.", field="canonical_conflict")
    cats.append(c)

    # ---- 5. CONTENT (12) ----------------------------------------------------
    c = _Cat("content", "Content", 12)
    wc = int(_num(_g(rec, "word_count", 0)))
    thin = bool(_g(rec, "thin_content", wc < 200))
    c.check(0.40, 0.0 if (thin or wc < 200) else 1.0, sev="High" if (thin or wc < 300) else None,
            title=f"Dunne content ({wc} woorden)" if (thin or wc < 300) else "",
            why="Dunne pagina's (<300 woorden) ranken zelden voor competitieve termen; diepgang wint.",
            fix="Breid uit naar betekenisvolle, unieke content (richtlijn 600+ woorden voor commerciele pagina's).",
            field="word_count")
    if wc >= 800: depth = 1.0
    elif wc >= 300: depth = 0.6 + 0.4 * (wc - 300) / 500.0
    else: depth = wc / 300.0
    c.check(0.30, depth)
    ph = bool(_g(rec, "placeholder_content", False))
    c.check(0.15, 0.0 if ph else 1.0, sev="High" if ph else None,
            title="Placeholder-/lorem-ipsum-achtige content gevonden" if ph else "",
            why="Placeholdertekst signaleert een onafgewerkte pagina; slecht voor kwaliteit en vertrouwen.",
            fix="Vervang placeholdertekst door echte, waardevolle content.", field="placeholder_content")
    fl = _g(rec, "readability_flesch", None)
    if fl is None:
        c.check(0.15, 0.7)
    else:
        flv = _num(fl)
        rr = 1.0 if 30 <= flv <= 80 else (0.5 if 20 <= flv <= 90 else 0.2)
        c.check(0.15, rr, sev="Low" if rr < 1 else None,
                title=f"Leesbaarheid buiten comfortzone (Flesch {round(flv,1)})" if rr < 1 else "",
                why="Te complexe of juist te simpele tekst kan engagement en begrip schaden.",
                fix="Mik op Flesch ~30-70 (vlot leesbaar Nederlands): kortere zinnen, actieve taal.",
                field="readability_flesch")
    cats.append(c)

    # ---- 6. STRUCTURED DATA (11) -------------------------------------------
    c = _Cat("structured_data", "Structured data", 11)
    types = _g(rec, "jsonld_types", []) or []
    has_ld = 1.0 if types else 0.0
    c.check(0.35, has_ld, sev="Medium" if not types else None,
            title="Geen JSON-LD structured data", why="Schema geeft rich results (sterren, prijs, FAQ) -> hogere CTR.",
            fix="Voeg passende Schema.org JSON-LD toe (Product/Offer, FAQPage, Organization, Breadcrumb).",
            field="jsonld_types")
    sv = _g(rec, "schema_validation", []) or []
    bad = [s for s in sv if isinstance(s, dict) and str(s.get("status", "")).lower() in ("invalid", "error", "incomplete")]
    if sv:
        r = 1.0 - min(1.0, len(bad) / max(1, len(sv)))
    else:
        r = 1.0
    c.check(0.30, r, sev="High" if bad else None,
            title=f"{len(bad)} schema-type(s) onvolledig/ongeldig" if bad else "",
            why="Onvolledige schema mist verplichte velden -> geen rich result en mogelijke waarschuwingen in Search Console.",
            fix="Vul de ontbrekende verplichte velden aan (zie schema_validation.missing_required).",
            field="schema_validation")
    fab = _g(rec, "fabricated_aggregaterating", {}) or {}
    fab_flag = bool(fab.get("flag"))
    c.check(0.20, 0.0 if fab_flag else 1.0, sev="Critical" if fab_flag else None,
            title="Mogelijk verzonnen AggregateRating (sterren zonder zichtbare reviews)" if fab_flag else "",
            why="Sterren-schema zonder echte, zichtbare reviews is in strijd met Google's richtlijnen en kan een handmatige maatregel opleveren.",
            fix="Toon echte reviews op de pagina of verwijder het AggregateRating-schema.", field="fabricated_aggregaterating")
    rre = _g(rec, "rich_result_eligible", []) or []
    c.check(0.15, 1.0 if rre else (0.5 if types else 0.0))
    cats.append(c)

    # ---- 7. IMAGES (8) — alleen toepasselijk met afbeeldingen --------------
    img_count = int(_num(_g(rec, "image_count", 0)))
    isum = _g(rec, "images_summary", {}) or {}
    c = _Cat("images", "Afbeeldingen", 8, applicable=img_count > 0)
    if img_count > 0:
        miss = int(_num(_g(rec, "images_missing_alt", isum.get("missing_alt", 0))))
        alt_ratio = 1.0 - min(1.0, miss / max(1, img_count))
        c.check(0.40, alt_ratio, sev="Medium" if miss else None,
                title=f"{miss} van {img_count} afbeeldingen zonder alt-tekst" if miss else "",
                why="Alt-tekst rankt in Google Afbeeldingen en geeft context + toegankelijkheid; ontbrekende alts zijn quick wins.",
                fix="Geef elke inhoudelijke afbeelding een beschrijvende alt-tekst.", field="images_missing_alt")
        nd = int(_num(isum.get("missing_dimensions", 0)))
        c.check(0.20, 1.0 - min(1.0, nd / max(1, img_count)), sev="Low" if nd else None,
                title=f"{nd} afbeeldingen zonder width/height" if nd else "",
                why="Vaste afmetingen voorkomen layout-shift (CLS), een Core Web Vital.",
                fix="Zet expliciete width/height (of aspect-ratio) op afbeeldingen.", field="images_summary")
        ng = int(_num(isum.get("non_next_gen_count", 0)))
        c.check(0.20, 1.0 - min(1.0, ng / max(1, img_count)), sev="Low" if ng else None,
                title=f"{ng} afbeeldingen niet in next-gen formaat (WebP/AVIF)" if ng else "",
                why="WebP/AVIF zijn fors lichter -> snellere LCP en lagere bandbreedte.",
                fix="Converteer zware JPEG/PNG naar WebP of AVIF.", field="images_summary")
        br = int(_num(isum.get("broken_count", len(_g(rec, "broken_images", []) or []))))
        c.check(0.20, 0.0 if br else 1.0, sev="High" if br else None,
                title=f"{br} kapotte afbeelding(en)" if br else "",
                why="Kapotte afbeeldingen schaden gebruikservaring en signaleren onderhoudsachterstand.",
                fix="Herstel of verwijder de kapotte afbeeldings-URLs.", field="broken_images")
    cats.append(c)

    # ---- 8. LINKS (8) -------------------------------------------------------
    c = _Cat("links", "Links", 8)
    broken = _g(rec, "broken_links", []) or []
    c.check(0.40, 0.0 if broken else 1.0, sev="High" if broken else None,
            title=f"{len(broken)} kapotte link(s) (4xx/5xx)" if broken else "",
            why="Dode links verspillen crawlbudget en frustreren bezoekers; ze lekken linkwaarde weg.",
            fix="Herstel of verwijder de kapotte links (zie broken_links in pages.json).", field="broken_links")
    nint = len(_g(rec, "internal_links", []) or [])
    c.check(0.25, 1.0 if nint >= 3 else nint / 3.0, sev="Medium" if nint < 3 else None,
            title=f"Weinig interne links ({nint})" if nint < 3 else "",
            why="Interne links verdelen autoriteit en helpen Google de site te crawlen en context te begrijpen.",
            fix="Voeg relevante interne links met beschrijvende anchors toe.", field="internal_links")
    unsafe = _g(rec, "unsafe_cross_origin", []) or []
    c.check(0.20, 0.0 if unsafe else 1.0, sev="Low" if unsafe else None,
            title=f"{len(unsafe)} externe target=_blank-link(s) zonder rel=noopener" if unsafe else "",
            why="target=_blank zonder noopener is een beveiligings-/performancerisico (tabnabbing).",
            fix="Voeg rel=\"noopener\" (of noreferrer) toe aan externe _blank-links.", field="unsafe_cross_origin")
    aq = _g(rec, "anchor_quality", {}) or {}
    nond = int(_num(aq.get("non_descriptive_count", 0)))
    c.check(0.15, 1.0 if nond == 0 else max(0.0, 1.0 - nond / 10.0), sev="Low" if nond else None,
            title=f"{nond} niet-beschrijvende anchor(s) ('lees meer', 'klik hier')" if nond else "",
            why="Beschrijvende anchortekst vertelt Google waar de doelpagina over gaat; 'lees meer' zegt niets.",
            fix="Vervang generieke anchors door tekst die het doel beschrijft.", field="anchor_quality")
    cats.append(c)

    # ---- 9. TECHNISCH (9) ---------------------------------------------------
    c = _Cat("technical", "Technisch", 9)
    sh = _g(rec, "security_headers", {}) or {}
    important = ["hsts", "x_content_type_options", "x_frame_options", "content_security_policy",
                 "csp", "referrer_policy", "permissions_policy"]
    present = 0; total = 0
    for k in ("hsts", "x_content_type_options", "x_frame_options", "referrer_policy"):
        total += 1
        v = sh.get(k)
        if isinstance(v, dict) and v.get("present"):
            present += 1
    sh_ratio = present / max(1, total)
    missing_sh = [k for k in ("hsts", "x_content_type_options", "x_frame_options", "referrer_policy")
                  if not (isinstance(sh.get(k), dict) and sh.get(k, {}).get("present"))]
    c.check(0.35, sh_ratio, sev="Low" if missing_sh else None,
            title=f"Security-headers ontbreken: {', '.join(missing_sh)}" if missing_sh else "",
            why="Security-headers (HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy) zijn best-practice en een 'best practices'-signaal.",
            fix="Stel de ontbrekende security-headers in op je server/CDN.", field="security_headers")
    comp = _g(rec, "compression", "")
    c.check(0.20, 1.0 if comp else 0.0, sev="Medium" if not comp else None,
            title="Geen compressie (gzip/brotli)" if not comp else "",
            why="Compressie verkleint de overdracht fors -> snellere laadtijd.",
            fix="Zet gzip of (beter) brotli aan op de server.", field="compression")
    cache = bool(_g(rec, "has_caching", False))
    c.check(0.15, 1.0 if cache else 0.0, sev="Low" if not cache else None,
            title="Geen cache-headers" if not cache else "",
            why="Cache-headers versnellen herhaalbezoek en verlagen serverbelasting.",
            fix="Stel Cache-Control/ETag in voor statische assets.", field="caching_headers")
    mc = int(_num(_g(rec, "mixed_content_count", 0)))
    c.check(0.20, 0.0 if mc else 1.0, sev="High" if mc else None,
            title=f"{mc} mixed-content resource(s) (http op https-pagina)" if mc else "",
            why="Mixed content wordt geblokkeerd door browsers en breekt de HTTPS-belofte.",
            fix="Laad alle resources over HTTPS.", field="mixed_content")
    ttfb = _num(_g(rec, "ttfb_ms", _g(rec, "response_ms", 0)))
    if ttfb <= 0: tr = 0.8
    elif ttfb <= 200: tr = 1.0
    elif ttfb <= 600: tr = 0.7
    elif ttfb <= 1200: tr = 0.4
    else: tr = 0.1
    c.check(0.10, tr, sev="Medium" if ttfb > 800 else None,
            title=f"Trage server-respons (TTFB {int(ttfb)}ms)" if ttfb > 800 else "",
            why="Een trage TTFB vertraagt alles erna en drukt de Core Web Vitals.",
            fix="Versnel server/CDN/caching; mik op TTFB < 600ms.", field="ttfb_ms")
    cats.append(c)

    # ---- 10. CORE WEB VITALS (8) — alleen met PSI-data ---------------------
    has_cwv = any(_g(rec, k) is not None for k in ("lcp_field", "inp_field", "cls_field", "lcp", "cls"))
    c = _Cat("cwv", "Core Web Vitals", 8, applicable=bool(psi_enabled and has_cwv))
    if psi_enabled and has_cwv:
        lcp = _num(_g(rec, "lcp_field", _g(rec, "lcp", 0)))
        inp = _num(_g(rec, "inp_field", _g(rec, "tbt", 0)))
        cls = _num(_g(rec, "cls_field", _g(rec, "cls", 0)))
        lr = 1.0 if lcp and lcp <= 2500 else (0.5 if lcp and lcp <= 4000 else (0.1 if lcp else 0.6))
        c.check(0.34, lr, sev="High" if lcp > 2500 else None,
                title=f"LCP traag ({int(lcp)}ms)" if lcp > 2500 else "",
                why="Largest Contentful Paint > 2.5s = trage gevoelde laadtijd (Core Web Vital).",
                fix="Optimaliseer de hero-render: lichtere afbeeldingen, minder render-blocking, snellere TTFB.", field="lcp_field")
        ir = 1.0 if inp and inp <= 200 else (0.5 if inp and inp <= 500 else (0.1 if inp else 0.6))
        c.check(0.33, ir, sev="High" if inp > 200 else None,
                title=f"INP/interactiviteit traag ({int(inp)}ms)" if inp > 200 else "",
                why="Interaction to Next Paint > 200ms voelt als haperende interactie (Core Web Vital).",
                fix="Verminder en splits langlopende JS-taken; minder main-thread-werk.", field="inp_field")
        clr = 1.0 if cls <= 0.1 else (0.5 if cls <= 0.25 else 0.1)
        c.check(0.33, clr, sev="High" if cls > 0.1 else None,
                title=f"Layout-shift te hoog (CLS {round(cls,3)})" if cls > 0.1 else "",
                why="Cumulative Layout Shift > 0.1 = visueel verspringen (Core Web Vital).",
                fix="Reserveer ruimte voor media/ads (width/height, aspect-ratio).", field="cls_field")
    cats.append(c)

    # ---- 11. E-COMMERCE (8) — alleen op productpagina's --------------------
    es = _g(rec, "ecommerce_summary", {}) or {}
    is_product = bool(es.get("has_product_schema")) or bool(_g(rec, "products_found"))
    c = _Cat("ecommerce", "E-commerce", 8, applicable=is_product)
    if is_product:
        c.check(0.30, 1.0 if es.get("has_price") else 0.0, sev="High" if not es.get("has_price") else None,
                title="Productpagina zonder prijs in schema (Offer.price)",
                why="Zonder prijs in Product/Offer-schema geen Google Shopping rich result.",
                fix="Voeg een geldige Offer met price + priceCurrency toe.", field="ecommerce_summary")
        c.check(0.20, 1.0 if es.get("has_availability") else 0.0, sev="Medium" if not es.get("has_availability") else None,
                title="Geen beschikbaarheid (Offer.availability)",
                why="availability (InStock/OutOfStock) is vereist voor merchant rich results.",
                fix="Zet Offer.availability op de juiste schema.org-waarde.", field="ecommerce_summary")
        c.check(0.20, 1.0 if es.get("has_gtin") else 0.0, sev="Medium" if not es.get("has_gtin") else None,
                title="Geen GTIN/EAN op product",
                why="GTIN koppelt je product aan de Google-catalogus -> betere matching en zichtbaarheid.",
                fix="Voeg gtin13/ean/mpn toe waar beschikbaar.", field="gtin")
        c.check(0.15, 1.0 if es.get("has_brand") else 0.0, sev="Low" if not es.get("has_brand") else None,
                title="Geen merk (brand) op product",
                why="Merk is een aanbevolen veld voor product rich results en filtering.",
                fix="Voeg brand toe aan het Product-schema.", field="ecommerce_summary")
        c.check(0.15, 1.0 if es.get("merchant_listing_ready") else 0.0, sev="Medium" if not es.get("merchant_listing_ready") else None,
                title="Niet 'merchant listing ready'",
                why="Mist een of meer velden die Google eist voor een merchant listing rich result.",
                fix="Zorg dat price, availability en (idealiter) reviews/shipping/return compleet zijn.", field="ecommerce_summary")
    cats.append(c)

    # ---- aggregatie ---------------------------------------------------------
    breakdown = []
    issues = []
    num = den = 0.0
    for cat in cats:
        info, cat_issues = cat.finalize()
        breakdown.append(info)
        if info["applicable"]:
            num += info["earned"]
            den += info["weight"]
            issues.extend(cat_issues)
    score = round(100 * num / den, 1) if den else None
    issues.sort(key=lambda it: (SEV_ORDER.get(it["severity"], 9), -(it.get("impact_points") or 0)))
    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
    for it in issues:
        counts[it["severity"]] = counts.get(it["severity"], 0) + 1
    return {
        "score": score, "grade": grade_for(score) if score is not None else "?",
        "weighted_points": round(num, 2), "applicable_weight": round(den, 2),
        "issue_counts": counts, "breakdown": breakdown, "issues": issues,
    }


# ============================================================================ #
def find_duplicates(pages):
    """(dup_titles, dup_descs) = lowercased waarden die op >1 pagina voorkomen."""
    from collections import Counter
    tc = Counter(); dc = Counter()
    for p in pages:
        t = str(p.get("title", "") or "").strip().lower()
        d = str(p.get("meta_description", "") or "").strip().lower()
        if t: tc[t] += 1
        if d: dc[d] += 1
    return ({t for t, n in tc.items() if n > 1}, {d for d, n in dc.items() if n > 1})


def score_site(pages, psi_enabled=False):
    """Score elke pagina (in-place: p['seo_health_score']) + bouw een site-aggregaat."""
    dup_titles, dup_descs = find_duplicates(pages)
    scored = []
    scored_pages = []   # (url, score, grade) — eigen lijst i.p.v. record-herlezen
    cat_acc = {}        # key -> [ratio's] van toepasselijke pagina's
    all_issues = []
    for p in pages:
        sc = score_page(p, dup_titles, dup_descs, psi_enabled)
        # LET OP: NIET 'seo_score' — die key is al van de PSI/Lighthouse-SEO-score
        # (performance_ecom). Onze samengestelde audit-score heet seo_health_score
        # zodat we het bestaande veld niet overschrijven (geen regressie).
        p["seo_health_score"] = sc.get("score")
        p["seo_grade"] = sc.get("grade")
        p["seo_issue_counts"] = sc.get("issue_counts")
        p["seo_breakdown"] = sc.get("breakdown")
        p["seo_issues"] = sc.get("issues")
        if sc.get("score") is not None:
            scored.append(sc["score"])
            scored_pages.append({"url": p.get("url"), "score": sc["score"], "grade": sc.get("grade")})
        for b in sc.get("breakdown", []):
            if b["applicable"]:
                cat_acc.setdefault(b["key"], {"label": b["label"], "weight": b["weight"], "ratios": []})
                cat_acc[b["key"]]["ratios"].append(b["ratio"])
        for it in sc.get("issues", []):
            all_issues.append({**it, "url": p.get("url", "")})

    site_score = round(sum(scored) / len(scored), 1) if scored else None
    cat_scores = {}
    for k, v in cat_acc.items():
        avg = sum(v["ratios"]) / len(v["ratios"]) if v["ratios"] else 0.0
        cat_scores[k] = {"label": v["label"], "weight": v["weight"],
                         "avg_pct": round(100 * avg, 1), "pages": len(v["ratios"])}

    # top-issues geaggregeerd over de site (welke fix raakt de meeste pagina's?)
    from collections import defaultdict
    agg = defaultdict(lambda: {"severity": "", "category": "", "title": "", "why": "", "fix": "", "pages": 0, "urls": []})
    for it in all_issues:
        key = (it["severity"], it["title"])
        a = agg[key]
        a["severity"] = it["severity"]; a["category"] = it["category"]
        a["title"] = it["title"]; a["why"] = it["why"]; a["fix"] = it["fix"]
        a["pages"] += 1
        if len(a["urls"]) < 12:
            a["urls"].append(it["url"])
    top_issues = sorted(agg.values(), key=lambda a: (SEV_ORDER.get(a["severity"], 9), -a["pages"]))

    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
    for it in all_issues:
        counts[it["severity"]] = counts.get(it["severity"], 0) + 1

    worst = sorted(scored_pages, key=lambda x: x["score"])[:10]
    best = sorted(scored_pages, key=lambda x: -x["score"])[:5]

    return {
        "site_score": site_score,
        "site_grade": grade_for(site_score) if site_score is not None else "?",
        "pages_scored": len(scored),
        "category_scores": cat_scores,
        "issue_counts": counts,
        "top_issues": top_issues[:40],
        "worst_pages": worst,
        "best_pages": best,
        "duplicate_titles": len(dup_titles),
        "duplicate_descriptions": len(dup_descs),
    }
