# -*- coding: utf-8 -*-
"""
MODULE 1.3 — BEELD-AUDIT (plan §4). Maakt de schouw-klachten van de operator
("wit blok op donker", "product zwemt in het kader", "nep/dubbele foto's")
machinaal meetbaar over ALLE productfoto's van de gerenderde pagina's.

Per productfoto (JS-kaarten tellen mee via render_meta uit module 1.1):
  1. KADERVULLING%  — bounding-box product vs canvas (norm 80-90 goed, <70 rood)
  2. WIT-OP-DONKER  — lichte eigen achtergrond op een donkere kaart
  3. RESOLUTIE/SCHAAL — upscaling (onscherp) + extreem oversized (bytes-verspilling)
  4. FORMAAT/GEWICHT — webp/avif vs png-foto; te zware bestanden
  5. ALT-TEKST       — ontbrekend / leeg / generiek
  6. WATERMERK       — conservatieve hint ("handmatig verifieren"), nooit hard
  7. DUPLICATEN      — perceptual hash (aHash) over de distincte productbeelden
  8. AI-LOOK         — bewust NIET beoordeeld (geen betrouwbaar signaal → nep-zekerheid vermijden)

Databron = alleen wat op disk staat (geen netwerk): per pagina
`p["screenshots"]["render_meta"]` (gerenderde afmetingen + kaartkleur) en de
gedownloade bronbestanden in `<out>/images/` (safe_name(src)). Zonder render
degradeert de audit naar de kale-HTML-afbeeldingen in het page-record + een note.

Fail-soft overal: één kapotte afbeelding/pagina = note, nooit een crash.
Dependencies: Pillow + numpy (fail-soft; ontbreken -> dimensie-/metadata-metingen).
"""
import json
import os
from urllib.parse import urlparse

try:
    from . import _images_visual_helpers as H
except Exception:  # bij los draaien (harness voegt audits/ toe aan path)
    import _images_visual_helpers as H

KEY = "images_visual"
LABEL = "Beeld-audit (productfoto's)"
ORDER = 30

# --- kalibratie / drempels (zie wiring-notitie) -----------------------------
FILL_GOED_MIN = 80          # norm: 80-90% goed
FILL_ROOD_MAX = 70          # <70% = te leeg (rood)
WOD_IMG_LUM = 0.82          # eigen-achtergrond zo licht -> "wit"
WOD_CARD_LUM = 0.35         # kaart zo donker -> "donker"
DUP_HAMMING = 6             # aHash-afstand <= dit -> (bijna) identiek
KB_KAART = 300              # kaartfoto (displayW >= 200) zwaarder = rood
KB_THUMB = 150              # thumbnail zwaarder = rood
MAX_FILES = 600             # cap op distincte pixel-analyses (perf); met melding
MAX_RECORDS_OUT = 500       # cap op per-foto-records in data{}; aggregaten over alles

_EXCLUDE = ("logo", "brand", "icon", "ico-", "sprite", "favicon", "payment",
            "/pay/", "pay-", "ideal", "klarna", "visa", "master", "maestro",
            "bancontact", "amex", "paypal", "keurmerk", "trust", "thuiswinkel",
            "webwinkelkeur", "flag", "avatar", "rating", "review", "star-")
_HERO = ("/lifestyle/", "hero-", "/hero", "sfeer", "/banner", "banner", "slider",
         "swiper", "carousel")
_PROD_CTX = ("product", "realproduct", "card", "item", "thumb", "gallery",
             "pdp", "grid", "shop", "catalog", "listing", "artikel")
_ALT_GENERIC = {"", "image", "afbeelding", "foto", "photo", "product", "logo",
                "img", "picture", "plaatje", "productfoto", "scootmobiel",
                "step", "e-step"}


# ---------------------------------------------------------------------------
def audit(ctx):
    """Nooit-crashende wrapper rond de echte audit."""
    try:
        return _audit(ctx)
    except Exception as e:  # laatste vangnet naast de runner
        try:
            ctx.get("log") and ctx["log"].warning(f"[images_visual] onverwacht: {e}")
        except Exception:
            pass
        return {"score": None,
                "summary": f"Beeld-audit kon niet volledig draaien ({e}).",
                "issues": [], "data": {"error": str(e)}}


def _audit(ctx):
    pages = ctx.get("pages") or []
    out = ctx.get("out")
    out = out if hasattr(out, "__fspath__") else (out and str(out))
    img_dir = os.path.join(str(out), "images") if out else None
    products_img_urls = _product_image_urls(ctx.get("products") or [])

    records = []          # per unieke product-src
    seen_src = set()
    hero_excluded = 0
    excluded_nonprod = 0
    mode = "degraded"     # wordt "render" zodra we een render_meta lezen
    pages_with_render = 0

    for p in pages:
        page_url = p.get("url", "")
        imgs, page_mode = _page_images(p, out)
        if page_mode == "render":
            pages_with_render += 1
        for im in imgs:
            src = im.get("src") or ""
            if not src or src.startswith("data:"):
                continue
            kind = _classify(im, page_url, products_img_urls)
            if kind == "hero":
                hero_excluded += 1
                continue
            if kind != "product":
                excluded_nonprod += 1
                continue
            if src in seen_src:
                continue
            seen_src.add(src)
            records.append({"im": im, "page_url": page_url, "src": src,
                            "mode": page_mode})

    if pages_with_render:
        mode = "render"

    if not records:
        note = ("Geen productfoto's herkend op de gerenderde pagina's."
                if mode == "render" else
                "Geen render/screenshots in deze run en geen productfoto's in de kale HTML.")
        return {"score": None, "summary": note, "issues": [],
                "data": {"n_photos": 0, "mode": mode, "hero_excluded": hero_excluded,
                         "pixels_available": H.PIXELS_AVAILABLE}}

    # ---- pixel-analyse (gecachet per lokaal bestand) ----
    file_cache = {}       # safe_name -> analyse-dict
    files_analyzed = 0
    capped_files = False

    def analyze_src(src):
        nonlocal files_analyzed, capped_files
        if not img_dir or not os.path.isdir(img_dir):
            return None, None
        for name in H.candidate_names(src, ctx.get("safe_name")):
            fp = os.path.join(img_dir, name)
            if os.path.exists(fp):
                if name in file_cache:
                    return name, file_cache[name]
                if files_analyzed >= MAX_FILES:
                    capped_files = True
                    return name, {"ok": False, "error": "niet gemeten (analyse-cap bereikt)"}
                res = H.analyze_image_file(fp)
                try:
                    res["file_kb"] = round(os.path.getsize(fp) / 1024.0, 1)
                except Exception:
                    res["file_kb"] = None
                file_cache[name] = res
                files_analyzed += 1
                return name, res
        return None, None

    photos = []
    for rec in records:
        photos.append(_build_photo(rec, analyze_src))

    # ---- duplicaten: alleen op DISTINCTE lokale bestanden (botsing != duplicaat) ----
    _assign_dup_groups(photos, file_cache)

    # ---- aggregaten ----
    agg = _aggregate(photos, file_cache)
    agg.update({"mode": mode, "hero_excluded": hero_excluded,
                "excluded_nonprod": excluded_nonprod,
                "pixels_available": H.PIXELS_AVAILABLE,
                "distinct_local_files": len(file_cache),
                "capped": bool(capped_files),
                "files_analyzed": files_analyzed})

    issues = _make_issues(photos, agg, mode)
    score = _score(photos, agg)
    summary = _summary(agg, mode)
    html = _html_table(photos)

    # per-foto records in data (met cap; aggregaten zijn over alles berekend)
    data = dict(agg)
    data["collision_note"] = (
        "Let op: de scraper bewaart beelden op safe_name(src) = laatste padsegment. "
        "Sites met per-product submappen en gelijke bestandsnamen (bv. p00.webp) laten "
        "meerdere product-URL's samenvallen op één lokaal bestand; pixel-metingen delen "
        "dan dat bestand. Distincte bestanden: {}, product-URL's: {}.".format(
            len(file_cache), len(photos)))
    data["photos"] = [_slim(ph) for ph in photos[:MAX_RECORDS_OUT]]
    if len(photos) > MAX_RECORDS_OUT:
        data["photos_truncated"] = len(photos) - MAX_RECORDS_OUT
        data["capped"] = True

    return {"score": score, "summary": summary, "issues": issues,
            "data": data, "html": html}


# ---------------------------------------------------------------------------
# Afbeeldingen per pagina ophalen (render vs degradatie)
# ---------------------------------------------------------------------------
def _page_images(p, out):
    """Retourneer (list-van-image-dicts, "render"|"degraded").
    Render: desktop-images uit render_meta.json (fallback mobile).
    Degradatie: p["images"] uit het page-record (kale HTML)."""
    sc = p.get("screenshots") or {}
    rm_rel = sc.get("render_meta")
    if rm_rel and out:
        fp = os.path.join(str(out), str(rm_rel).replace("/", os.sep))
        try:
            rm = json.loads(_read(fp))
            vps = rm.get("viewports", {}) or {}
            vp = vps.get("desktop") or vps.get("mobile") or {}
            imgs = vp.get("images") or []
            page_bg = vp.get("pageBg")
            norm = []
            for im in imgs:
                norm.append({
                    "src": im.get("src"), "alt": im.get("alt"),
                    "naturalWidth": im.get("naturalWidth"),
                    "naturalHeight": im.get("naturalHeight"),
                    "displayW": im.get("displayW"), "displayH": im.get("displayH"),
                    "cardBg": im.get("cardBg"), "cardBgImage": im.get("cardBgImage"),
                    "classes": im.get("classes"), "ancestors": im.get("ancestors"),
                    "inLink": im.get("inLink"), "loading": im.get("loading"),
                    "pageBg": page_bg, "_from": "render"})
            return norm, "render"
        except Exception:
            pass
    # degradatie: kale-HTML-afbeeldingen
    norm = []
    for im in (p.get("images") or []):
        w = _to_int(im.get("width"))
        hh = _to_int(im.get("height"))
        norm.append({
            "src": im.get("src"), "alt": im.get("alt"),
            "naturalWidth": w, "naturalHeight": hh,
            "displayW": w, "displayH": hh,
            "cardBg": None, "cardBgImage": None,
            "classes": im.get("class") or im.get("classes"),
            "ancestors": None, "inLink": None, "loading": im.get("loading"),
            "pageBg": None, "_from": "degraded"})
    return norm, "degraded"


# ---------------------------------------------------------------------------
# Classificatie: product / hero / anders
# ---------------------------------------------------------------------------
def _classify(im, page_url, product_urls):
    src = (im.get("src") or "")
    text = " ".join(str(im.get(k) or "") for k in ("src", "classes", "alt", "ancestors")).lower()
    pathlow = urlparse(src).path.lower()
    dW = im.get("displayW")

    # te klein -> icoon/ornament (alleen als we een maat hebben)
    if isinstance(dW, (int, float)) and dW and dW < 80:
        return "other"
    # hero/sfeer/banner -> geen productfoto (wel apart geteld)
    if any(tok in text for tok in _HERO):
        return "hero"
    # logo/betaal/keurmerk/icoon -> uitsluiten
    if any(tok in text for tok in _EXCLUDE):
        return "other"

    if src in product_urls:
        return "product"
    if any(tok in pathlow for tok in ("product", "realproduct", "/shop/", "/artikel")):
        return "product"
    if any(tok in text for tok in _PROD_CTX):
        return "product"
    # hoofdbeeld op een /product/-detailpagina
    if "product" in urlparse(page_url).path.lower() and isinstance(dW, (int, float)) and dW and dW >= 200:
        return "product"
    return "other"


# ---------------------------------------------------------------------------
# Per-foto-record bouwen
# ---------------------------------------------------------------------------
def _build_photo(rec, analyze_src):
    im = rec["im"]
    src = rec["src"]
    ph = {
        "src": src, "page_url": rec["page_url"], "mode": rec["mode"],
        "natural_w": _to_int(im.get("naturalWidth")), "natural_h": _to_int(im.get("naturalHeight")),
        "display_w": _to_int(im.get("displayW")), "display_h": _to_int(im.get("displayH")),
        "format": H.ext_format(src),
        "fill_pct": None, "img_bg_rgb": None, "card_bg_rgb": None,
        "white_on_dark": False, "wod_assessed": False, "transparent": None,
        "file_kb": None, "alt_ok": None, "dup_group": None,
        "watermark_hint": False, "ai_look": "niet beoordeeld",
        "local_file": None, "notes": [],
    }
    # alt-tekst
    ph["alt_ok"] = _alt_ok(im.get("alt"), src)

    # kaartkleur uit render_meta; bij een gradient/achtergrondbeeld (cardBg None)
    # de PAGINA-achtergrond als benadering gebruiken (lagere zekerheid). Zo werkt
    # wit-op-donker ook op donkere thema's waar de kaarten een gradient hebben —
    # precies het scenario waar de operator over klaagt.
    card_rgb = H.parse_rgb(im.get("cardBg"))
    card_src = "kaart"
    if card_rgb is None:
        page_rgb = H.parse_rgb(im.get("pageBg"))
        if page_rgb is not None:
            card_rgb = page_rgb
            card_src = "pagina-achtergrond (benadering; kaart heeft gradient/achtergrondbeeld)"
    ph["card_bg_rgb"] = list(card_rgb) if card_rgb else None
    ph["card_bg_source"] = card_src if card_rgb else None
    card_lum = H.rel_luminance(card_rgb) if card_rgb else None

    # pixel-analyse (indien lokaal bestand)
    name, res = analyze_src(src)
    ph["local_file"] = name
    if res and res.get("ok"):
        ph["fill_pct"] = res.get("fill_pct")
        ph["transparent"] = res.get("transparent")
        ph["watermark_hint"] = bool(res.get("watermark_hint"))
        ph["file_kb"] = res.get("file_kb")
        if not ph["format"]:
            ph["format"] = (res.get("pil_format") or "")
        if not res.get("transparent"):
            ph["img_bg_rgb"] = list(res.get("bg_rgb")) if res.get("bg_rgb") else None
        img_lum = res.get("bg_lum")
        # wit-op-donker: eigen lichte bg op donkere kaart (transparant = geen eigen bg = goed)
        if res.get("transparent"):
            ph["notes"].append("transparante achtergrond — niet wit-op-donker (juist goed)")
        elif img_lum is not None and card_lum is not None:
            ph["wod_assessed"] = True
            ph["white_on_dark"] = bool(img_lum > WOD_IMG_LUM and card_lum < WOD_CARD_LUM)
            if card_src != "kaart":
                ph["notes"].append("wit-op-donker beoordeeld via " + card_src)
        else:
            ph["notes"].append("kaartkleur onbekend — wit-op-donker niet beoordeeld")
    elif res is not None:
        # bestand gevonden maar niet gemeten (analyse-cap / geen Pillow / kapot beeld)
        ph["notes"].append(res.get("error") or "pixels niet gemeten")
        ph["_not_measured"] = True
    else:
        # geen lokaal bronbestand voor deze src
        if not H.PIXELS_AVAILABLE:
            ph["notes"].append("pixels niet gemeten (Pillow/numpy ontbreekt)")
            ph["_not_measured"] = True
        else:
            ph["notes"].append("bron niet lokaal opgeslagen — pixels niet gemeten")
            ph["_no_local"] = True

    # resolutie/schaal (uit render_meta-afmetingen; die weerspiegelen het geserveerde asset)
    nW, dW = ph["natural_w"], ph["display_w"]
    if isinstance(nW, int) and isinstance(dW, int) and nW > 0 and dW > 0:
        if nW < dW * 0.9:
            ph["scale"] = "upscaled"
        elif nW > dW * 3:
            ph["scale"] = "oversized"
        else:
            ph["scale"] = "ok"
    else:
        ph["scale"] = None
    return ph


# ---------------------------------------------------------------------------
def _assign_dup_groups(photos, file_cache):
    """Groepeer DISTINCTE lokale bestanden op aHash-nabijheid. Twee product-URL's
    die door de safe_name-botsing hetzelfde bestand delen tellen NIET als
    duplicaat (dat is een opslag-artefact, geen echte dubbele foto)."""
    items = [(name, r.get("ahash")) for name, r in file_cache.items()
             if r and r.get("ok") and r.get("ahash") is not None]
    group_of = {}
    gid = 0
    for i in range(len(items)):
        ni, hi = items[i]
        if ni in group_of:
            continue
        members = [ni]
        for j in range(i + 1, len(items)):
            nj, hj = items[j]
            if nj in group_of:
                continue
            if H.hamming(hi, hj) <= DUP_HAMMING:
                members.append(nj)
        if len(members) > 1:
            gid += 1
            for m in members:
                group_of[m] = gid
    for ph in photos:
        ph["dup_group"] = group_of.get(ph.get("local_file"))


# ---------------------------------------------------------------------------
def _aggregate(photos, file_cache):
    fills = [ph["fill_pct"] for ph in photos if isinstance(ph.get("fill_pct"), (int, float))]
    n_fill = len(fills)
    avg_fill = round(sum(fills) / n_fill, 1) if n_fill else None
    n_below70 = sum(1 for f in fills if f < FILL_ROOD_MAX)
    n_below62 = sum(1 for f in fills if f < 62)

    # distincte-bestand-statistiek (eerlijker beeld ondanks botsing)
    dfills = [r["fill_pct"] for r in file_cache.values()
              if r and r.get("ok") and isinstance(r.get("fill_pct"), (int, float))]
    df_avg = round(sum(dfills) / len(dfills), 1) if dfills else None

    return {
        "n_photos": len(photos),
        "n_fill_measured": n_fill,
        "n_no_local": sum(1 for ph in photos if ph.get("_no_local")),
        "n_not_measured": sum(1 for ph in photos if ph.get("_not_measured")),
        "avg_fill_pct": avg_fill,
        "n_below_70": n_below70,
        "n_below_62": n_below62,
        "distinct_files_avg_fill_pct": df_avg,
        "distinct_files_fill_spread": (
            [round(min(dfills), 1), round(max(dfills), 1)] if dfills else None),
        "n_white_on_dark": sum(1 for ph in photos if ph.get("white_on_dark")),
        "n_wod_assessed": sum(1 for ph in photos if ph.get("wod_assessed")),
        "n_wod_not_assessed": sum(1 for ph in photos if not ph.get("wod_assessed")),
        "n_transparent": sum(1 for ph in photos if ph.get("transparent")),
        "n_upscaled": sum(1 for ph in photos if ph.get("scale") == "upscaled"),
        "n_oversized": sum(1 for ph in photos if ph.get("scale") == "oversized"),
        "n_png_photo": sum(1 for ph in photos if ph.get("format") == "png"),
        "n_heavy": sum(1 for ph in photos if _is_heavy(ph)),
        "n_alt_bad": sum(1 for ph in photos if ph.get("alt_ok") is False),
        "n_watermark": sum(1 for ph in photos if ph.get("watermark_hint")),
        "n_dup_groups": len({ph["dup_group"] for ph in photos if ph.get("dup_group")}),
        "n_in_dup": sum(1 for ph in photos if ph.get("dup_group")),
        "ai_look": "niet beoordeeld (geen betrouwbaar signaal)",
    }


# ---------------------------------------------------------------------------
def _score(photos, agg):
    comps = []  # (waarde 0-100, gewicht)

    n_fill = agg["n_fill_measured"]
    if n_fill:
        # per foto: 45% -> 0, 80% -> 1 (norm gehaald), lineair; >80 = vol kader
        ratios = []
        for ph in photos:
            f = ph.get("fill_pct")
            if isinstance(f, (int, float)):
                ratios.append(max(0.0, min(1.0, (f - 45.0) / (FILL_GOED_MIN - 45.0))))
        comps.append((100.0 * sum(ratios) / len(ratios), 0.45))

    if agg["n_wod_assessed"]:
        comps.append((100.0 * (1 - agg["n_white_on_dark"] / agg["n_wod_assessed"]), 0.18))

    n = agg["n_photos"] or 1
    # resolutie (upscaling telt zwaar, oversized licht)
    res_ok = 1 - (agg["n_upscaled"] + 0.4 * agg["n_oversized"]) / n
    comps.append((100.0 * max(0.0, res_ok), 0.12))
    # formaat/gewicht
    fw_ok = 1 - (0.6 * agg["n_png_photo"] + agg["n_heavy"]) / n
    comps.append((100.0 * max(0.0, fw_ok), 0.12))
    # alt
    n_alt = sum(1 for ph in photos if ph.get("alt_ok") is not None)
    if n_alt:
        comps.append((100.0 * (1 - agg["n_alt_bad"] / n_alt), 0.10))
    # duplicaten
    comps.append((100.0 * max(0.0, 1 - agg["n_in_dup"] / n), 0.04))

    if not comps:
        return None
    wsum = sum(w for _, w in comps)
    return round(sum(v * w for v, w in comps) / wsum, 1)


# ---------------------------------------------------------------------------
def _make_issues(photos, agg, mode):
    issues = []
    worst_fill = sorted((ph for ph in photos if isinstance(ph.get("fill_pct"), (int, float))),
                        key=lambda p: p["fill_pct"])

    if mode == "degraded":
        issues.append({
            "severity": "Medium", "category": "render",
            "title": "Beeld-audit draaide op de kale HTML (geen screenshots)",
            "why": ("Zonder de gerenderde pagina's missen we JS-geladen productkaarten, "
                    "de werkelijke weergave-afmetingen en de kaartkleur — kadervulling en "
                    "wit-op-donker zijn dan niet of onbetrouwbaar te meten."),
            "fix": "Draai de scraper met --screenshots zodat de beeld-audit op de echte render meet.",
            "url": ""})

    if agg["n_below_70"]:
        n_fill = agg["n_fill_measured"] or 1
        frac = agg["n_below_70"] / n_fill
        sev = "High" if (agg["avg_fill_pct"] is not None and agg["avg_fill_pct"] < FILL_ROOD_MAX
                         and frac >= 0.3) else "Medium"
        ex = ", ".join(f"{_short(p['src'])} ({p['fill_pct']}%)" for p in worst_fill[:3])
        issues.append({
            "severity": sev, "category": "kadervulling",
            "title": (f"Productfoto's vullen het kader te weinig — gem. {agg['avg_fill_pct']}%, "
                      f"{agg['n_below_70']}/{n_fill} onder de 70%-norm"),
            "why": ("Een half leeg kader met veel witruimte oogt goedkoop/onaf en verkleint het "
                    "product visueel — dat kost vertrouwen en conversie. Norm: 80-90% kadervulling. "
                    f"Slechtste: {ex}."),
            "fix": ("Vul het product tot 80-90% van het kader: strakker croppen / minder marge, "
                    "of corrigeer object-fit en beeldverhouding zodat het product de kaart vult."),
            "url": worst_fill[0]["page_url"] if worst_fill else ""})

    if agg["n_white_on_dark"]:
        exs = [ph for ph in photos if ph.get("white_on_dark")]
        issues.append({
            "severity": "High", "category": "wit-op-donker",
            "title": f"{agg['n_white_on_dark']} productfoto('s) als wit blok op een donkere kaart",
            "why": ("Een foto met eigen witte/lichte achtergrond op een donkere kaart geeft een hard "
                    "wit rechthoekig blok — het breekt het design en oogt als een losse plaatjes-plak."),
            "fix": ("Gebruik uitgeknipte foto's met transparante achtergrond (PNG/WebP met alpha), "
                    "of geef de kaart dezelfde lichte achtergrond als de foto."),
            "url": exs[0]["page_url"] if exs else ""})

    if agg["n_upscaled"]:
        exs = [ph for ph in photos if ph.get("scale") == "upscaled"]
        issues.append({
            "severity": "Medium", "category": "resolutie",
            "title": f"{agg['n_upscaled']} foto('s) worden vergroot weergegeven (upscaling)",
            "why": ("De bron is kleiner dan hoe de foto getoond wordt (naturalWidth < weergavebreedte) "
                    "→ zichtbaar onscherp, vooral op retina-schermen."),
            "fix": "Lever de foto op minstens de weergavebreedte (liefst 2x voor retina).",
            "url": exs[0]["page_url"] if exs else ""})

    if agg["n_oversized"]:
        exs = [ph for ph in photos if ph.get("scale") == "oversized"]
        issues.append({
            "severity": "Low", "category": "resolutie",
            "title": f"{agg['n_oversized']} foto('s) veel groter geladen dan getoond (>3x)",
            "why": "Onnodig zware download; kost laadtijd/Core Web Vitals zonder zichtbare winst.",
            "fix": "Schaal server-side naar de weergavemaat of gebruik srcset/sizes met een thumbnail.",
            "url": exs[0]["page_url"] if exs else ""})

    if agg["n_png_photo"]:
        exs = [ph for ph in photos if ph.get("format") == "png"]
        issues.append({
            "severity": "Low", "category": "formaat",
            "title": f"{agg['n_png_photo']} productfoto('s) als PNG i.p.v. WebP/AVIF",
            "why": "PNG-foto's zijn fors zwaarder dan WebP/AVIF bij gelijke kwaliteit.",
            "fix": "Converteer productfoto's naar WebP (of AVIF).",
            "url": exs[0]["page_url"] if exs else ""})

    if agg["n_heavy"]:
        exs = sorted((ph for ph in photos if _is_heavy(ph)),
                     key=lambda p: -(p.get("file_kb") or 0))
        issues.append({
            "severity": "Medium", "category": "gewicht",
            "title": f"{agg['n_heavy']} foto('s) zwaarder dan de richtlijn (kaart >{KB_KAART}KB / thumb >{KB_THUMB}KB)",
            "why": ("Zware beelden vertragen de pagina en de Largest Contentful Paint. "
                    "(Gemeten op het lokaal opgeslagen bestand — zie collision_note in data.)"),
            "fix": "Comprimeer/schaal de foto's; streef naar <150KB voor thumbs en <300KB voor kaartbeelden.",
            "url": exs[0]["page_url"] if exs else ""})

    if agg["n_alt_bad"]:
        exs = [ph for ph in photos if ph.get("alt_ok") is False]
        issues.append({
            "severity": "Medium", "category": "alt-tekst",
            "title": f"{agg['n_alt_bad']} productfoto('s) zonder bruikbare alt-tekst",
            "why": "Lege/generieke alt-tekst schaadt toegankelijkheid en beeld-SEO (Google Afbeeldingen).",
            "fix": "Geef elke productfoto een beschrijvende alt: merk + model + type (bv. 'Segway Ninebot C8 e-step').",
            "url": exs[0]["page_url"] if exs else ""})

    if agg["n_watermark"]:
        exs = [ph for ph in photos if ph.get("watermark_hint")]
        issues.append({
            "severity": "Medium", "category": "watermerk",
            "title": f"{agg['n_watermark']} foto('s) met mogelijk watermerk/overlay-tekst",
            "why": ("Conservatieve heuristiek zag tekst-achtige structuren in hoek-/randzones — dit kan "
                    "een watermerk van een leverancier zijn. Handmatig verifieren; geen harde conclusie."),
            "fix": "Controleer de foto's; vervang leverancier-watermerken door eigen, rechtenvrije productfoto's.",
            "url": exs[0]["page_url"] if exs else ""})

    if agg["n_dup_groups"]:
        issues.append({
            "severity": "Low", "category": "duplicaten",
            "title": (f"{agg['n_dup_groups']} groep(en) (bijna) identieke productbeelden "
                      f"({agg['n_in_dup']} foto's)"),
            "why": ("Meerdere producten delen (vrijwel) hetzelfde beeld — vaak een placeholder of "
                    "dubbele asset i.p.v. echte, unieke productfotografie."),
            "fix": "Geef elk product een eigen, herkenbare foto.",
            "url": ""})

    n_unmeasured = agg["n_no_local"] + agg["n_not_measured"]
    if n_unmeasured:
        reason = ("Deze beelden stonden niet als apart bestand in images/ (o.a. door de safe_name-"
                  "botsing bij per-product submappen)."
                  if agg["n_no_local"] >= agg["n_not_measured"] else
                  "Pixels konden niet gemeten worden (Pillow/numpy ontbreekt of beeld onleesbaar).")
        issues.append({
            "severity": "Low", "category": "meting",
            "title": (f"{n_unmeasured}/{agg['n_photos']} productfoto's niet op pixelniveau gemeten"),
            "why": reason + " Kadervulling/gewicht zijn voor die foto's onbekend.",
            "fix": "Zie collision_note in data; overweeg de bestandsnaamgeving in de scraper te verrijken.",
            "url": ""})

    return issues


# ---------------------------------------------------------------------------
def _summary(agg, mode):
    parts = [f"{agg['n_photos']} productfoto's"]
    if not agg.get("pixels_available"):
        parts.append("pixels niet gemeten (Pillow/numpy ontbreekt) — alleen alt/afmetingen beoordeeld")
    if agg["avg_fill_pct"] is not None:
        parts.append(f"gem. kadervulling {agg['avg_fill_pct']}% "
                     f"({agg['n_below_70']} onder 70%)")
    if agg["distinct_files_fill_spread"]:
        lo, hi = agg["distinct_files_fill_spread"]
        parts.append(f"spreiding {lo}-{hi}% over {agg['distinct_local_files']} distincte bestanden")
    if agg["n_wod_assessed"]:
        parts.append(f"{agg['n_white_on_dark']} wit-op-donker (van {agg['n_wod_assessed']} beoordeeld)")
    elif agg.get("n_transparent"):
        parts.append("0 wit-op-donker (productfoto's zijn transparante uitsneden)")
    else:
        parts.append("wit-op-donker niet beoordeeld (geen kaart-/pagina-achtergrondkleur bekend)")
    tail = " — gemeten op de kale HTML (geen render)" if mode == "degraded" else ""
    return "; ".join(parts) + tail + "."


# ---------------------------------------------------------------------------
def _html_table(photos):
    worst = sorted((ph for ph in photos if isinstance(ph.get("fill_pct"), (int, float))),
                   key=lambda p: p["fill_pct"])[:10]
    if not worst:
        return ""
    rows = []
    for ph in worst:
        fill = ph["fill_pct"]
        col = "#c0392b" if fill < 70 else ("#e67e22" if fill < 80 else "#27ae60")
        wod = "ja" if ph.get("white_on_dark") else ("—" if ph.get("wod_assessed") else "?")
        kb = ph.get("file_kb")
        rows.append(
            "<tr>"
            f"<td style='padding:4px 8px;font-family:monospace;font-size:12px'>{_esc(_short(ph['src']))}</td>"
            f"<td style='padding:4px 8px;color:{col};font-weight:600'>{fill}%</td>"
            f"<td style='padding:4px 8px'>{_esc(ph.get('format') or '—')}</td>"
            f"<td style='padding:4px 8px'>{kb if kb is not None else '—'}</td>"
            f"<td style='padding:4px 8px'>{'ok' if ph.get('alt_ok') else 'nee'}</td>"
            f"<td style='padding:4px 8px'>{wod}</td>"
            "</tr>")
    return (
        "<div style='margin-top:12px'>"
        "<div style='font-weight:600;margin-bottom:6px'>10 slechtst gevulde productfoto's</div>"
        "<table style='border-collapse:collapse;font-size:13px'>"
        "<tr style='background:#f4f6f8;text-align:left'>"
        "<th style='padding:4px 8px'>bestand</th><th style='padding:4px 8px'>vulling</th>"
        "<th style='padding:4px 8px'>formaat</th><th style='padding:4px 8px'>KB</th>"
        "<th style='padding:4px 8px'>alt</th><th style='padding:4px 8px'>wit-op-donker</th></tr>"
        + "".join(rows) + "</table></div>")


# ---------------------------------------------------------------------------
# kleine helpers
# ---------------------------------------------------------------------------
def _product_image_urls(products):
    urls = set()
    for pr in products or []:
        if not isinstance(pr, dict):
            continue
        for k in ("image", "images", "img", "thumbnail", "photo", "photos"):
            v = pr.get(k)
            if isinstance(v, str):
                urls.add(v)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    if isinstance(x, str):
                        urls.add(x)
    return urls


def _alt_ok(alt, src):
    if alt is None:
        return False
    a = str(alt).strip().lower()
    if not a or a in _ALT_GENERIC or len(a) < 4:
        return False
    # gelijk aan de bestandsnaam? dan generiek
    base = urlparse(src).path.split("/")[-1].lower()
    if a == base or a == os.path.splitext(base)[0]:
        return False
    return True


def _is_heavy(ph):
    kb = ph.get("file_kb")
    if not isinstance(kb, (int, float)):
        return False
    dW = ph.get("display_w")
    budget = KB_KAART if (isinstance(dW, int) and dW >= 200) else KB_THUMB
    return kb > budget


def _slim(ph):
    """Per-foto-record voor data{} conform contract (+ enkele extra's)."""
    return {
        "src": ph["src"], "page_url": ph["page_url"],
        "fill_pct": ph["fill_pct"], "img_bg_rgb": ph["img_bg_rgb"],
        "card_bg_rgb": ph["card_bg_rgb"], "white_on_dark": ph["white_on_dark"],
        "wod_assessed": ph["wod_assessed"],
        "natural_w": ph["natural_w"], "natural_h": ph["natural_h"],
        "display_w": ph["display_w"], "display_h": ph["display_h"],
        "file_kb": ph["file_kb"], "format": ph["format"], "alt_ok": ph["alt_ok"],
        "dup_group": ph["dup_group"], "watermark_hint": ph["watermark_hint"],
        "ai_look": ph["ai_look"], "scale": ph.get("scale"),
        "local_file": ph.get("local_file"), "notes": ph.get("notes") or [],
    }


def _to_int(v):
    try:
        if v is None or v == "":
            return None
        return int(round(float(v)))
    except Exception:
        return None


def _short(src, n=42):
    try:
        path = urlparse(src).path
        return path[-n:] if len(path) > n else path
    except Exception:
        return str(src)[-n:]


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _read(fp):
    with open(fp, "r", encoding="utf-8") as f:
        return f.read()
