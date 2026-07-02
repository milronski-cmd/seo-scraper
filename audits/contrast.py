# -*- coding: utf-8 -*-
"""
MODULE 1.7 — CONTRAST / LEESBAARHEID-AUDIT (plan §4, KEY=contrast, ORDER=70).

Meet per tekst-element op de GERENDERDE pagina (beide viewports, mobiel weegt
het zwaarst) of de tekst leesbaar is:

 1. WCAG-contrast (tekstkleur vs effectieve achtergrond): gewone tekst >=4,5:1,
    grote tekst (>=24px, of >=18.66px bij fontWeight>=700) >=3:1.
 2. Minimale fontgroottes: body-achtige tekst (<16px op mobiel) en micro-tekst
    (<11px, overal) worden gemeld.
 3. Tekst-over-foto zonder overlay: waar de achtergrond een background-image is
    (kleur onbekend) wordt het ECHTE worst-case-contrast gemeten via pixel-
    sampling uit de full-page screenshot.
 4. Te lichte grijzen: valt onder de WCAG-check; de fix benoemt het patroon
    expliciet en wijst een concrete, toegankelijke token-kleur aan.

Databron = render_meta.json + full-page PNG's per pagina (module 1.1). GEEN
netwerk, GEEN eigen browser. Fail-soft overal: ontbrekende render => nette note,
nooit een exception naar buiten.

Contract + ctx: zie INTEGRATION.md. Wiring-notitie: audits/_wiring/contrast.md.
"""
import json
import re
import statistics
from collections import defaultdict

KEY = "contrast"
LABEL = "Contrast & leesbaarheid (WCAG)"
ORDER = 70

# ---- optionele dependencies (fail-soft) -------------------------------------
try:
    import numpy as _np
except Exception:                                    # pragma: no cover
    _np = None
try:
    from PIL import Image as _Image
    # Onze eigen full-page screenshots zijn legitiem hoog (mobiel 2x DSF ->
    # 30k+ px); zet de decompression-bomb-limiet uit zodat ze niet warnen/falen.
    _Image.MAX_IMAGE_PIXELS = None
except Exception:                                    # pragma: no cover
    _Image = None

# ---- constanten -------------------------------------------------------------
_BODY_TAGS = {"p", "li", "dd", "dt", "td", "th", "span", "div", "a", "blockquote",
              "figcaption", "label", "small", "strong", "em"}
_STATE_CLASSES = {
    "reveal", "is-visible", "is-inview", "in-view", "inview", "visible", "show",
    "shown", "active", "is-active", "current", "is-current", "open", "is-open",
    "hover", "focus", "selected", "is-selected", "loaded", "is-loaded",
    "lazyloaded", "lazyload", "animated", "aos-animate", "swiper-slide-active",
    "swiper-slide-visible", "slick-active", "fade-in", "revealed", "on",
}
_LARGE_MIN_PX = 24.0
_LARGE_BOLD_MIN_PX = 18.66
_BODY_MIN_PX_MOBILE = 16.0
_MICRO_PX = 11.0
_SAMPLE_CAP = 4000            # niet-stil begrensd; gemeld in data.capped

# =============================================================================
# Kleur- en WCAG-helpers
# =============================================================================
def _parse_color(s):
    """rgb()/rgba()/#hex -> (r, g, b, a) floats, of None. Robuust."""
    if not s:
        return None
    s = str(s).strip()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) >= 6:
            try:
                return (float(int(h[0:2], 16)), float(int(h[2:4], 16)),
                        float(int(h[4:6], 16)), 1.0)
            except ValueError:
                return None
        return None
    nums = re.findall(r"[-+]?\d*\.?\d+", s)
    if len(nums) < 3:
        return None
    try:
        r, g, b = float(nums[0]), float(nums[1]), float(nums[2])
    except ValueError:
        return None
    a = 1.0
    if len(nums) >= 4:
        try:
            a = float(nums[3])
        except ValueError:
            a = 1.0
    return (max(0.0, min(255.0, r)), max(0.0, min(255.0, g)),
            max(0.0, min(255.0, b)), max(0.0, min(1.0, a)))


def _lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb):
    return 0.2126 * _lin(rgb[0]) + 0.7152 * _lin(rgb[1]) + 0.0722 * _lin(rgb[2])


def _contrast(fg, bg):
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def _composite(fg_rgba, bg_rgb):
    """Effectieve kleur van (semi-)transparante tekst over een dekkende bg."""
    a = fg_rgba[3]
    if a >= 1.0:
        return (fg_rgba[0], fg_rgba[1], fg_rgba[2])
    return tuple(a * fg_rgba[i] + (1 - a) * bg_rgb[i] for i in range(3))


def _hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(
        int(round(max(0, min(255, rgb[0])))),
        int(round(max(0, min(255, rgb[1])))),
        int(round(max(0, min(255, rgb[2])))))


def _nl2(x):
    """Ratio met 2 decimalen, Nederlandse komma (3,01)."""
    return f"{x:.2f}".replace(".", ",")


def _nlnorm(x):
    """Norm netjes: 4,5 of 3 (geen '4' voor 4,5)."""
    s = f"{x:.1f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _weight_int(weight):
    try:
        return int(re.findall(r"\d+", str(weight))[0])
    except Exception:
        w = str(weight).lower()
        return 700 if w in ("bold", "bolder") else 400


def _is_large(font_px, weight):
    if not font_px:
        return False
    return font_px >= _LARGE_MIN_PX or (font_px >= _LARGE_BOLD_MIN_PX and _weight_int(weight) >= 700)


def _norm_for(font_px, weight):
    return 3.0 if _is_large(font_px, weight) else 4.5


def _suggest_accessible_fg(fg_rgb, bg_rgb, norm):
    """Concrete token-kleur (hex) die de norm haalt: schuif de tekstkleur naar
    zwart (op lichte bg) of wit (op donkere bg) tot de ratio voldoet."""
    if _contrast(fg_rgb, bg_rgb) >= norm:
        return None
    toward_white = _luminance(bg_rgb) <= 0.18   # donkere achtergrond -> lichtere tekst
    for step in range(1, 101):
        f = step / 100.0
        if toward_white:
            cand = tuple(fg_rgb[i] * (1 - f) + 255.0 * f for i in range(3))
        else:
            cand = tuple(fg_rgb[i] * (1 - f) for i in range(3))
        if _contrast(cand, bg_rgb) >= norm:
            return _hex(cand)
    return "#000000" if not toward_white else "#ffffff"


# =============================================================================
# Selector-patroon (voor agressieve dedup op patroon-niveau)
# =============================================================================
def _selector_pattern(sel):
    """Normaliseer een selector tot een stabiele component-signatuur, zodat
    hetzelfde element op alle pagina's/kaarten op één hoop valt."""
    if not sel:
        return "?"
    segs = re.split(r"\s*>\s*", str(sel).strip())
    tail = segs[-2:] if len(segs) >= 2 else segs
    out = []
    for seg in tail:
        token = re.split(r"\s+", seg.strip())[-1] if seg.strip() else seg
        m = re.match(r"^([a-zA-Z][\w-]*)", token)
        tag = m.group(1) if m else ""
        classes = re.findall(r"\.([A-Za-z][\w-]*)", token)
        good = []
        for c in classes:
            if c.lower() in _STATE_CLASSES:
                continue
            good.append(re.sub(r"[-_]?\d+$", "", c))
        sig = tag + (("." + good[0]) if good else "")
        out.append(sig or (token[:16] if token else "?"))
    return ">".join(out) or "?"


# =============================================================================
# Pixel-sampling voor tekst-over-foto (bgImage, bg-kleur onbekend)
# =============================================================================
def _lum_array(arr):
    a = arr.astype(_np.float64) / 255.0
    lin = _np.where(a <= 0.03928, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
    return 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]


def _dilate(mask, r):
    """4-verbonden binaire dilatatie (r iteraties) — breidt de letter-maat uit
    zodat de anti-aliasing-rand rond de letters óók wordt verwijderd. Zonder dit
    lekt de lichte AA-halo van lichte tekst op donkere bg in de achtergrond en
    ontstaan valse 'te laag contrast'-metingen."""
    m = mask
    for _ in range(int(r)):
        d = m.copy()
        d[1:, :] |= m[:-1, :]
        d[:-1, :] |= m[1:, :]
        d[:, 1:] |= m[:, :-1]
        d[:, :-1] |= m[:, 1:]
        m = d
    return m


def _pixel_sample(img, t, dsf):
    """Meet het worst-case-contrast (10e percentiel) van tekst over een foto.

    Retour: (r10, method_meta) of (None, reden-string). Klemt het bemonsterde
    gebied naar de rijen/kolommen waar de letters echt staan, zodat aangrenzende
    lichtere/donkerdere elementen de meting niet vervuilen.
    """
    col = _parse_color(t.get("color"))
    if not col:
        return None, "geen_kleur"
    try:
        W, H = img.size
        x = (t.get("x") or 0) * dsf
        y = (t.get("y") or 0) * dsf
        w = (t.get("w") or 0) * dsf
        h = (t.get("h") or 0) * dsf
        margin = 4 * dsf
        x0 = int(max(0, x - margin)); y0 = int(max(0, y - margin))
        x1 = int(min(W, x + w + margin)); y1 = int(min(H, y + h + margin))
        if y0 >= H:
            return None, "onder_png"          # lazy element buiten screenshot
        if x1 <= x0 or y1 <= y0:
            return None, "leeg"
        cap = 200 * dsf                        # extreem groot element -> midden ~200x200
        if x1 - x0 > cap:
            cx = (x0 + x1) // 2; x0 = max(0, cx - cap // 2); x1 = min(W, x0 + cap)
        if y1 - y0 > cap:
            cy = (y0 + y1) // 2; y0 = max(0, cy - cap // 2); y1 = min(H, y0 + cap)
        crop = _np.asarray(img.crop((x0, y0, x1, y1)).convert("RGB")).astype(_np.float64)
        if crop.shape[0] < 3 or crop.shape[1] < 3:
            return None, "te_klein"
        med = _np.median(crop.reshape(-1, 3), axis=0)
        if col[3] < 1.0:                        # gerenderde letters = samengesteld
            txt_rgb = _np.array(_composite(col, tuple(med)))
            fg_eff = _composite(col, tuple(med))
        else:
            txt_rgb = _np.array(col[:3]); fg_eff = col[:3]
        dist = _np.sqrt(((crop - txt_rgb) ** 2).sum(axis=2))
        letters = dist < 40                     # strakke letter-maat
        lr = _np.where(letters.any(axis=1))[0]
        lc = _np.where(letters.any(axis=0))[0]
        if len(lr) >= 2 and len(lc) >= 2 and letters.sum() >= 10:
            p = 2 * dsf
            r0 = max(0, lr[0] - p); r1 = min(crop.shape[0], lr[-1] + 1 + p)
            c0 = max(0, lc[0] - p); c1 = min(crop.shape[1], lc[-1] + 1 + p)
            crop = crop[r0:r1, c0:c1]; dist = dist[r0:r1, c0:c1]
        # letters + hun anti-aliasing-halo ruimtelijk verwijderen
        core = dist < 60
        core = _dilate(core, 2 * dsf)
        bgmask = ~core
        bg = crop[bgmask]                       # (N, 3) echte achtergrond
        total = int(dist.size)
        if len(bg) < 12 or total == 0:
            return None, "te_weinig_bg"
        frac = len(bg) / total
        if frac > 0.97:                         # bijna niets als letter herkend -> onbetrouwbaar
            return None, "geen_letters"
        ls = _lum_array(bg.reshape(-1, 1, 3)).ravel()
        lf = _luminance(fg_eff)
        ratios = _np.where(ls >= lf, (ls + 0.05) / (lf + 0.05), (lf + 0.05) / (ls + 0.05))
        r10 = float(_np.percentile(ratios, 10))
        return round(r10, 2), {"frac": round(frac, 2), "medbg": _hex(_np.median(bg, axis=0))}
    except Exception:
        return None, "sample_fout"


# =============================================================================
# Hoofd-audit
# =============================================================================
def audit(ctx):
    try:
        return _audit(ctx)
    except Exception as e:                      # extra vangnet bovenop de runner
        return {"score": None,
                "summary": f"Contrast-audit kon niet draaien: {e}",
                "issues": [], "data": {"error": str(e)}}


def _audit(ctx):
    pages = ctx.get("pages") or []
    out = ctx.get("out")
    log = ctx.get("log")

    def _warn(msg):
        try:
            if log:
                log.warning("contrast: %s", msg)
        except Exception:
            pass

    # per-viewport tellers voor de score (per-element pass/fail)
    vp_measured = {"desktop": 0, "mobile": 0}
    vp_fails = {"desktop": 0, "mobile": 0}
    method_counts = {"computed": 0, "pixel_sample": 0}
    skipped = defaultdict(int)

    # dedup-verzamelaars
    contrast_groups = {}     # key -> aggregatie (computed WCAG-fails)
    photo_groups = {}        # key -> alle pixel-samples (pass+fail) voor robuuste mediaan
    micro_groups = {}
    smallbody_groups = {}
    # pixel-elementen worden pas gescoord NA de mediaan-berekening, zodat een
    # enkele ruis-/drift-uitschieter het contrast-oordeel (en dus de score) niet
    # bepaalt — precies zoals de issue-beslissing (mediaan over het patroon).
    pixel_elems = []         # (vp, group_key, norm, font_fail)

    pages_with_meta = 0
    n_sample = 0
    capped = False

    def _mk_group(store, key):
        if key not in store:
            store[key] = {"count": 0, "pages": set(), "worst": None, "best": None,
                          "ratios": [], "example": None, "norm_body": 0, "norm_large": 0}
        return store[key]

    for p in pages:
        url = p.get("url", "") or ""
        sc = p.get("screenshots") or {}
        rel_meta = sc.get("render_meta")
        if not rel_meta or not out:
            skipped["geen_render_meta"] += 1
            continue
        try:
            meta_path = out / rel_meta
            if not meta_path.exists():
                skipped["geen_render_meta"] += 1
                continue
            rm = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            skipped["render_meta_onleesbaar"] += 1
            _warn(f"render_meta onleesbaar voor {url}: {e}")
            continue

        viewports = (rm.get("viewports") or {})
        if not viewports:
            skipped["geen_render_meta"] += 1
            continue
        pages_with_meta += 1

        for vp in ("desktop", "mobile"):
            vdata = viewports.get(vp)
            if not vdata:
                continue
            texts = vdata.get("texts") or []

            # full-page PNG lazy openen (1x per pagina/viewport, hergebruiken).
            # DSF ligt vast (module 1.1): desktop 1:1, mobiel 2x. NIET uit de
            # PNG-breedte afleiden — die bevat horizontale overflow (bv. MoveVolt
            # heeft mobiele zijwaartse scroll, PNG 1498px breed bij DSF 2).
            img = None
            dsf = 1 if vp == "desktop" else 2
            png_rel = sc.get(f"{vp}_full")
            need_png = any(tt.get("bgImage") or _parse_color(tt.get("bg")) is None
                           for tt in texts)
            if need_png and _Image is not None and _np is not None and png_rel:
                try:
                    png_path = out / png_rel
                    if png_path.exists():
                        img = _Image.open(png_path)
                        img.load()
                except Exception as e:
                    img = None
                    _warn(f"png niet te openen ({vp}) voor {url}: {e}")

            try:
                for t in texts:
                    txt = (t.get("text") or "").strip()
                    if not txt:
                        continue
                    w = t.get("w") or 0
                    h = t.get("h") or 0
                    if w <= 1 or h <= 1:               # clip/visually-hidden
                        continue
                    col = _parse_color(t.get("color"))
                    if not col:
                        skipped["geen_kleur"] += 1
                        continue
                    fs = t.get("fontSize") or 0.0
                    weight = t.get("fontWeight")
                    tag = (t.get("tag") or "").lower()
                    sel = t.get("selector") or ""
                    pat = _selector_pattern(sel)

                    element_failed = False
                    deferred = False

                    # ---- fontgrootte-checks (exact) -----------------------
                    font_fail = False
                    if fs and fs < _MICRO_PX:
                        font_fail = True
                        g = _mk_group(micro_groups, (pat, round(fs, 1)))
                        g["count"] += 1
                        g["pages"].add(url)
                        if g["example"] is None:
                            g["example"] = {"url": url, "text": txt[:70], "selector": sel,
                                            "font_px": fs, "weight": str(weight), "viewport": vp}
                    if (vp == "mobile" and tag in _BODY_TAGS and len(txt) > 40
                            and fs and fs < _BODY_MIN_PX_MOBILE and fs >= _MICRO_PX):
                        font_fail = True
                        g = _mk_group(smallbody_groups, (pat, round(fs, 1)))
                        g["count"] += 1
                        g["pages"].add(url)
                        if g["example"] is None:
                            g["example"] = {"url": url, "text": txt[:70], "selector": sel,
                                            "font_px": fs, "weight": str(weight), "viewport": vp}

                    # ---- contrast ----------------------------------------
                    norm = _norm_for(fs, weight)
                    bg = _parse_color(t.get("bg"))
                    contrast_measured = False

                    if bg is not None and not t.get("bgImage"):
                        # exact berekend (bekende dekkende achtergrond)
                        fg_eff = _composite(col, bg) if col[3] < 1 else col[:3]
                        ratio = _contrast(fg_eff, bg)
                        contrast_measured = True
                        method_counts["computed"] += 1
                        if ratio < norm:
                            element_failed = True
                            key = (_hex(fg_eff), _hex(bg), pat)
                            g = _mk_group(contrast_groups, key)
                            g["count"] += 1
                            g["pages"].add(url)
                            g["ratios"].append(ratio)
                            if g["worst"] is None or ratio < g["worst"]:
                                g["worst"] = ratio
                                g["example"] = {
                                    "url": url, "text": txt[:70], "selector": sel,
                                    "fg": _hex(fg_eff), "bg": _hex(bg), "ratio": round(ratio, 2),
                                    "norm": norm, "font_px": fs, "weight": str(weight),
                                    "viewport": vp, "method": "computed",
                                    "suggest": _suggest_accessible_fg(fg_eff, bg, norm)}
                            if _is_large(fs, weight):
                                g["norm_large"] += 1
                            else:
                                g["norm_body"] += 1
                    else:
                        # tekst over foto: pixel-sampling (worst-case)
                        if img is not None and n_sample < _SAMPLE_CAP:
                            r10, mm = _pixel_sample(img, t, dsf)
                            n_sample += 1
                            if r10 is not None:
                                contrast_measured = True
                                method_counts["pixel_sample"] += 1
                                key = (_hex(col[:3]), "photo", pat)
                                g = _mk_group(photo_groups, key)
                                g["count"] += 1
                                g["pages"].add(url)
                                g["ratios"].append(r10)
                                if _is_large(fs, weight):
                                    g["norm_large"] += 1
                                else:
                                    g["norm_body"] += 1
                                if g["worst"] is None or r10 < g["worst"]:
                                    g["worst"] = r10
                                    g["example"] = {
                                        "url": url, "text": txt[:70], "selector": sel,
                                        "fg": _hex(col[:3]), "bg": "foto", "ratio": r10,
                                        "norm": norm, "font_px": fs, "weight": str(weight),
                                        "viewport": vp, "method": "pixel_sample",
                                        "medbg": (mm or {}).get("medbg")}
                                # score-oordeel uitstellen tot de patroon-mediaan bekend is
                                deferred = True
                                pixel_elems.append((vp, key, norm, font_fail))
                            else:
                                skipped["bg_onbekend"] += 1
                                skipped[f"px_{mm}"] += 1
                        else:
                            if n_sample >= _SAMPLE_CAP:
                                capped = True
                            skipped["bg_onbekend"] += 1
                            if img is None:
                                skipped["geen_png_of_lib"] += 1

                    # ---- score-boekhouding (per element) ------------------
                    # pixel-elementen worden na de mediaan-pass geteld (deferred)
                    if not deferred and (contrast_measured or font_fail):
                        vp_measured[vp] += 1
                        if element_failed or font_fail:
                            vp_fails[vp] += 1
            finally:
                if img is not None:
                    try:
                        img.close()
                    except Exception:
                        pass

    # -------------------------------------------------------------------------
    # Geen enkele render_meta => degradatie-pad (bv. run zonder --screenshots)
    # -------------------------------------------------------------------------
    if pages_with_meta == 0:
        return {
            "score": None,
            "summary": ("Contrast niet gemeten: deze run bevat geen render_meta.json "
                        "(draai met --screenshots voor de contrast/leesbaarheid-audit)."),
            "issues": [{
                "severity": "Low", "category": "contrast",
                "title": "Contrast/leesbaarheid niet gemeten (geen render)",
                "why": ("Zonder gerenderde tekst-metadata (kleuren + posities per element) "
                        "kan WCAG-contrast en tekst-over-foto niet betrouwbaar worden bepaald."),
                "fix": "Draai de scraper met --screenshots; dan levert module 1.1 render_meta.json.",
                "url": "",
            }],
            "data": {"viewports": {}, "fails": [], "skipped": dict(skipped),
                     "pages_with_meta": 0, "method_counts": method_counts},
        }

    # -------------------------------------------------------------------------
    # Pixel-elementen alsnog scoren op basis van de patroon-mediaan (robuust)
    # -------------------------------------------------------------------------
    photo_median = {}
    for key, g in photo_groups.items():
        photo_median[key] = statistics.median(g["ratios"]) if g["ratios"] else None
    for vp, key, norm, font_fail in pixel_elems:
        vp_measured[vp] += 1
        med = photo_median.get(key)
        contrast_ok = med is None or med >= norm
        if (not contrast_ok) or font_fail:
            vp_fails[vp] += 1

    # -------------------------------------------------------------------------
    # Score = % geslaagde gemeten elementen, mobiel 60 / desktop 40
    # -------------------------------------------------------------------------
    def _pass_pct(vp):
        m = vp_measured[vp]
        return round(100.0 * (m - vp_fails[vp]) / m, 1) if m else None

    pct_d, pct_m = _pass_pct("desktop"), _pass_pct("mobile")
    if pct_d is not None and pct_m is not None:
        score = round(0.6 * pct_m + 0.4 * pct_d, 1)
    elif pct_m is not None:
        score = pct_m
    elif pct_d is not None:
        score = pct_d
    else:
        score = None

    # -------------------------------------------------------------------------
    # Issues bouwen (agressief gededupliceerd op patroon-niveau)
    # -------------------------------------------------------------------------
    issues = []
    fails_data = []

    def _sev_contrast(is_body):
        return "High" if is_body else "Medium"

    def _grays_hint(fg_hex, bg_hex, ratio):
        bg_rgb = _parse_color(bg_hex)
        light_bg = bg_rgb is not None and _luminance(bg_rgb) > 0.5
        base = f"{fg_hex} op {bg_hex} = {_nl2(ratio)}:1 — te weinig contrast tussen tekst en achtergrond."
        if light_bg:
            base += " Op wit is #767676 (4,54:1) de lichtste grijstint die nog mag voor gewone tekst."
        return base

    # (a) computed WCAG-fails
    for key, g in contrast_groups.items():
        ex = g["example"]
        if not ex:
            continue
        is_body = g["norm_body"] >= g["norm_large"]
        worst = round(g["worst"], 2)
        norm = 4.5 if is_body else 3.0
        suggest = ex.get("suggest")
        sug_txt = (f" Zet de tekst-/kleur-token op minimaal {suggest}"
                   if suggest else " Verhoog het contrast")
        issues.append({
            "severity": _sev_contrast(is_body),
            "category": "contrast",
            "title": (f"Te laag contrast: {ex['fg']} op {ex['bg']} "
                      f"({_nl2(worst)}:1, norm {_nlnorm(norm)}:1)"
                      + (f" — {g['count']} plekken" if g["count"] > 1 else "")),
            "why": ("Tekst met te weinig contrast is slecht leesbaar (WCAG AA: gewone tekst "
                    f">=4,5:1, grote tekst >=3:1). {_grays_hint(ex['fg'], ex['bg'], worst)}"),
            "fix": (sug_txt + " in de design-token-set (bijv. --color-text / --color-muted), "
                    "niet via een losse CSS-override — dan blijft de hele site consistent."),
            "url": ex["url"],
        })
        fails_data.append({
            "selector": _selector_pattern(ex["selector"]), "viewport": ex["viewport"],
            "fg": ex["fg"], "bg": ex["bg"], "ratio": worst, "norm": norm,
            "font_px": ex["font_px"], "weight": ex["weight"], "pages_n": len(g["pages"]),
            "example_url": ex["url"], "example_text": ex["text"], "method": "computed",
        })

    # (b) tekst-over-foto (pixel-sampling) — robuuste mediaan over instanties
    for key, g in photo_groups.items():
        ratios = g["ratios"]
        if not ratios:
            continue
        is_body = g["norm_body"] >= g["norm_large"]
        norm = 4.5 if is_body else 3.0
        med = round(statistics.median(ratios), 2)
        worst = round(min(ratios), 2)
        if med >= norm:                 # robuust: mediaan haalt de norm -> geen issue
            continue
        ex = g["example"]
        n_pages = len(g["pages"])
        issues.append({
            "severity": "High" if is_body else "Medium",
            "category": "contrast",
            "title": (f"Tekst over foto zonder (voldoende) overlay: {ex['fg']} "
                      f"~{_nl2(med)}:1"
                      + (f" ({g['count']} plekken)" if g["count"] > 1 else "")),
            "why": ("Tekst staat direct op een achtergrondfoto zonder scrim/overlay; het "
                    f"gemeten worst-case-contrast is ~{_nl2(med)}:1 (slechtste plek {_nl2(worst)}:1), "
                    f"norm >={_nlnorm(norm)}:1. Op lichtere foto-delen valt de tekst weg."),
            "fix": ("Leg een halftransparante donkere overlay/gradient (scrim) onder de tekst, "
                    "of gebruik volledig dekkende tekst met een tekstplaat — regel dit in de "
                    "component/design-tokens (bijv. --hero-scrim), niet als losse override. "
                    "Meting via pixel-sampling: verifieer visueel."),
            "url": ex["url"],
        })
        fails_data.append({
            "selector": _selector_pattern(ex["selector"]), "viewport": ex["viewport"],
            "fg": ex["fg"], "bg": "foto", "ratio": med, "norm": norm,
            "font_px": ex["font_px"], "weight": ex["weight"], "pages_n": n_pages,
            "example_url": ex["url"], "example_text": ex["text"], "method": "pixel_sample",
            "worst_ratio": worst,
        })

    # (c) micro-tekst (<11px, overal)
    font_issues_data = []
    for (pat, fs), g in micro_groups.items():
        ex = g["example"]
        issues.append({
            "severity": "Medium", "category": "leesbaarheid",
            "title": (f"Micro-tekst {fs:g}px" + (f" ({g['count']} plekken)" if g["count"] > 1 else "")
                      + f" — {pat}"),
            "why": ("Tekst kleiner dan 11px is voor veel bezoekers (zeker op mobiel en voor "
                    "senioren) nauwelijks leesbaar."),
            "fix": ("Verhoog naar minimaal 12px (liefst >=14px) via de type-schaal-token, "
                    "niet per element."),
            "url": ex["url"] if ex else "",
        })
        font_issues_data.append({"type": "micro", "selector": pat, "font_px": fs,
                                 "pages_n": len(g["pages"]), "count": g["count"],
                                 "example_url": ex["url"] if ex else "",
                                 "example_text": ex["text"] if ex else ""})

    # (d) body-tekst <16px op mobiel
    for (pat, fs), g in smallbody_groups.items():
        ex = g["example"]
        issues.append({
            "severity": "Medium", "category": "leesbaarheid",
            "title": (f"Body-tekst {fs:g}px op mobiel"
                      + (f" ({g['count']} plekken)" if g["count"] > 1 else "") + f" — {pat}"),
            "why": ("Lopende tekst onder 16px leest lastig op mobiel (mobiel is het zwaarst "
                    "wegende verkeer) en veroorzaakt op iOS ongewenste zoom bij formulieren."),
            "fix": ("Zet body-tekst op >=16px op mobiel via de basis-fontgrootte-token "
                    "(bijv. --font-size-base), niet per element."),
            "url": ex["url"] if ex else "",
        })
        font_issues_data.append({"type": "mobiel_body_klein", "selector": pat, "font_px": fs,
                                 "pages_n": len(g["pages"]), "count": g["count"],
                                 "example_url": ex["url"] if ex else "",
                                 "example_text": ex["text"] if ex else ""})

    # sorteren: severity, dan aantal vindplaatsen, dan slechtste ratio
    _sev_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    fail_by_url = {f["example_url"]: f for f in fails_data}

    def _issue_sort(it):
        f = fail_by_url.get(it.get("url"))
        ratio = f["ratio"] if f else 99
        pages_n = f["pages_n"] if f else 0
        return (_sev_rank.get(it["severity"], 9), -pages_n, ratio)

    issues.sort(key=_issue_sort)

    # geen stille cap; alleen als de lijst extreem lang wordt netjes begrenzen
    hard_cap = 60
    capped_issues = False
    if len(issues) > hard_cap:
        capped_issues = True
        issues = issues[:hard_cap]

    # -------------------------------------------------------------------------
    # samenvatting + data + optionele html
    # -------------------------------------------------------------------------
    n_high = sum(1 for i in issues if i["severity"] == "High")
    n_med = sum(1 for i in issues if i["severity"] == "Medium")
    summary = (f"Score {score}/100. "
               f"Gemeten: {vp_measured['mobile']} mobiel / {vp_measured['desktop']} desktop "
               f"tekst-elementen; {n_high} High- en {n_med} Medium-bevindingen. "
               f"Pixel-sampling op {method_counts['pixel_sample']} tekst-over-foto-elementen.")
    if capped:
        summary += f" (Let op: pixel-sampling begrensd op {_SAMPLE_CAP} elementen.)"
    if capped_issues:
        summary += f" (Issue-lijst afgekapt op {hard_cap}; zie data voor de tellingen.)"

    data = {
        "viewports": {
            "desktop": {"elements_measured": vp_measured["desktop"],
                        "fails": vp_fails["desktop"], "pass_pct": pct_d},
            "mobile": {"elements_measured": vp_measured["mobile"],
                       "fails": vp_fails["mobile"], "pass_pct": pct_m},
        },
        "weging": "mobiel 60% / desktop 40%",
        "fails": sorted(fails_data, key=lambda f: f["ratio"]),
        "font_issues": font_issues_data,
        "skipped": dict(skipped),
        "method_counts": method_counts,
        "pages_with_meta": pages_with_meta,
        "pixel_samples_done": n_sample,
    }
    if capped:
        data["capped"] = {"pixel_samples": _SAMPLE_CAP}
    if capped_issues:
        data["capped_issues"] = hard_cap

    html = _render_html(score, data, method_counts)

    return {"score": score, "summary": summary, "issues": issues,
            "data": data, "html": html}


def _render_html(score, data, method_counts):
    """Compacte per-viewport-tabel onder de issues (inline styles, zelfstandig)."""
    try:
        vps = data["viewports"]
        rows = ""
        for name, lbl in (("mobile", "Mobiel (weegt 60%)"), ("desktop", "Desktop (weegt 40%)")):
            v = vps.get(name, {})
            pct = v.get("pass_pct")
            pct_s = "n.v.t." if pct is None else f"{pct:.1f}%"
            rows += (f"<tr><td style='padding:4px 10px'>{lbl}</td>"
                     f"<td style='padding:4px 10px;text-align:right'>{v.get('elements_measured', 0)}</td>"
                     f"<td style='padding:4px 10px;text-align:right'>{v.get('fails', 0)}</td>"
                     f"<td style='padding:4px 10px;text-align:right;font-weight:600'>{pct_s}</td></tr>")
        return (
            "<div style='margin-top:8px;font-size:13px;color:#333'>"
            "<table style='border-collapse:collapse;font-size:13px'>"
            "<thead><tr style='border-bottom:1px solid #ddd'>"
            "<th style='padding:4px 10px;text-align:left'>Viewport</th>"
            "<th style='padding:4px 10px'>Gemeten</th>"
            "<th style='padding:4px 10px'>Fails</th>"
            "<th style='padding:4px 10px'>Geslaagd</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            f"<p style='margin:6px 0 0;color:#666'>Exact berekend: {method_counts['computed']} · "
            f"via pixel-sampling (tekst over foto): {method_counts['pixel_sample']}.</p>"
            "</div>")
    except Exception:
        return ""
