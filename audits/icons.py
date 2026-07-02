# -*- coding: utf-8 -*-
"""
MODULE 1.4 — ICON-AUDIT (plan §4, klacht §5B-1: "geen standaard AI-iconen,
geen emoji-als-icoon"). Meet per site, geaggregeerd over alle pagina's
(desktop primair, mobiel ter bevestiging voor emoji):

  1. EMOJI-ALS-ICOON   — emoji/losse tekens (pijlen, vinkjes, pictogrammen) die
                          als functioneel icoon dienen (nav/USP/buttons/lijsten).
                          Onderscheiden van emoji-in-lopende-tekst (geen issue).
  2. GEMIXTE ICON-SETS — hoeveel verschillende icoon-families draaien er door
                          elkaar (inline-SVG-grids, icon-fonts, losse img-iconen).
  3. INLINE-SVG vs FONT — icon-fonts = verouderd (FOIT-flits, geen multicolor,
                          slechte a11y) → advies inline-SVG.
  4. STROKE/FILL-STIJL  — lijndikte-verdeling + outline-naast-solide mixen.
  5. MAATVOERING        — sterk verschillende icoonmaten binnen dezelfde rij
                          (conservatief; anders overgeslagen met note).

Kern-output: één concreet "één professionele set per site"-advies.

Databron (GEEN netwerk): per pagina p["screenshots"]["render_meta"] -> een
render_meta.json met viewports.desktop/mobile.icons[] (svg/iconfont/img/emoji).
Fail-soft overal; ontbrekende render_meta = overslaan met telling; run zonder
screenshots => score None + nette note (degradatiepad).
"""
import json
import re
from collections import Counter, defaultdict

KEY = "icons"
LABEL = "Icon-audit (één set, geen emoji-iconen)"
ORDER = 40

# ---------------------------------------------------------------------------
# Tekenklassen voor emoji-classificatie
# ---------------------------------------------------------------------------
_ARROW = set("←↑→↓↔↕⟵⟶⇐⇒⇑⇓➜➔➙➛➞➟➠➡➢➣➤►▶◀◄▲▼△▽»«›‹")
_CHECK = set("✓✔✅❌✗✘✕✖☑☒❎☓✚")
_STAR = set("★☆⭐✨🌟✩✪✫")
_BULLET = set("•◦▪▫‣·●○■□◆◇")
_VARSEL = {0xFE0F, 0xFE0E, 0x200D}  # variation selectors + ZWJ


def _base_cp(ch):
    """Eerste betekenisvolle codepoint (variation selectors/ZWJ/skin-tone weg)."""
    for c in ch:
        cp = ord(c)
        if cp in _VARSEL:
            continue
        if 0x1F3FB <= cp <= 0x1F3FF:  # skin-tone modifiers
            continue
        return c, cp
    return (ch[:1] or " "), ord(ch[:1] or " ")


def _char_class(ch):
    if not ch:
        return "leeg"
    base, cp = _base_cp(ch)
    if base in _ARROW:
        return "pijl"
    if base in _CHECK:
        return "vink"
    if base in _STAR:
        return "ster"
    if base in _BULLET:
        return "bullet"
    if cp >= 0x1F000 or (0x2600 <= cp <= 0x27BF) or (0x2B00 <= cp <= 0x2BFF) \
            or cp >= 0x1F900 or (0x2190 <= cp <= 0x21FF):
        return "picto"
    if cp > 0x2000:
        return "symbool"
    return "tekst"


# ---------------------------------------------------------------------------
# SVG-familie: viewBox -> genormaliseerd grid (voorkomt vals-positief:
# FontAwesome heeft variabele breedte maar altijd hoogte ~512 = één set)
# ---------------------------------------------------------------------------
def _grid_family(viewbox):
    """(grid_label, is_iconish). Niet-vierkante vrije viewBoxes = waarschijnlijk
    logo/illustratie, geen UI-icoon."""
    if not viewbox:
        return ("geen-viewbox", True)
    parts = re.split(r"[\s,]+", str(viewbox).strip())
    if len(parts) != 4:
        return ("viewbox?", True)
    try:
        w = float(parts[2]); h = float(parts[3])
    except Exception:
        return ("viewbox?", True)
    if w <= 0 or h <= 0:
        return ("viewbox0", True)
    ratio = w / h
    # FontAwesome / Ionicons: hoogte ~512, breedte 360..660
    if 496 <= h <= 520 and 340 <= w <= 660:
        return ("grid-512", True)
    if 0.8 <= ratio <= 1.25:
        m = max(w, h)
        for g in (16, 20, 24, 28, 32, 36, 40, 48, 64, 96, 128, 256, 512):
            if abs(m - g) <= max(2.0, g * 0.12):
                return ("grid-%d" % g, True)
        return ("grid-%d" % int(round(m)), True)
    return ("vrij-%.1f" % ratio, False)  # non-square = geen icoon


_GRID_LABEL = {
    "grid-512": "512-grid solide (FontAwesome/Ionicons-stijl)",
    "grid-24": "24-grid (Feather/Lucide/Material/Tabler-stijl)",
    "grid-20": "20-grid (Heroicons-stijl)",
    "grid-16": "16-grid (Bootstrap-Icons-stijl)",
    "grid-32": "32-grid",
    "grid-48": "48-grid",
    "geen-viewbox": "inline-SVG zonder viewBox",
}

# Bekende icon-set klasse-prefixes (voor labeling + icon-font-detectie)
_SET_PREFIX = [
    (("fa", "fas", "far", "fab", "fal", "fad", "fa-solid", "fa-regular", "fa-brands", "fa-light"), "FontAwesome"),
    (("bi",), "Bootstrap Icons"),
    (("material-icons", "material-symbols", "material-symbols-outlined", "mat-icon", "mdi"), "Material Icons"),
    (("lucide",), "Lucide"),
    (("feather",), "Feather"),
    (("ion", "ionicon", "ion-icon"), "Ionicons"),
    (("ti", "tabler", "tabler-icon"), "Tabler"),
    (("bx", "bxs", "bxl"), "Boxicons"),
    (("gg",), "css.gg"),
    (("glyphicon",), "Glyphicons"),
    (("dashicons",), "Dashicons"),
    (("icon-",), "icon-font"),
]


def _set_from_classes(classes):
    toks = (classes or "").lower().split()
    for tok in toks:
        for prefixes, name in _SET_PREFIX:
            for pre in prefixes:
                if tok == pre or tok.startswith(pre + "-") or (pre.endswith("-") and tok.startswith(pre)):
                    return name
    return None


_LOGO_HINT = ("logo", "paylogo", "wordmark", "payment", "paymethod", "brand__svg", "creditcard")


def _is_logo(rec):
    cls = (rec.get("classes") or "").lower()
    anc = (rec.get("ancestors") or "").lower()
    if any(h in cls for h in _LOGO_HINT):
        return True
    if any(h in anc for h in ("brandbar", "paylogo", "payment", "logo-", "__logo", "logos")):
        return True
    return False


_ICON_CTX = ("nav", "menu", "header", "navbar", "topbar", "utilbar", "btn", "button",
             "cta", "usp", "feature", "benefit", "promise", "badge", "chip", "pill",
             "step", "card", "ico", "icon", "hero", "footer", "social", "contact",
             "rating", "star", "check", "tick", "bullet", "action", "list", "li>")


def _icon_context(selector, ancestors=""):
    s = ((selector or "") + " " + (ancestors or "")).lower()
    return any(k in s for k in _ICON_CTX)


def _stroke_px(sw):
    if not sw:
        return None
    m = re.match(r"\s*([\d.]+)\s*px", str(sw))
    if not m:
        return None
    try:
        return round(float(m.group(1)), 2)
    except Exception:
        return None


def _short_sel(sel, n=90):
    sel = sel or ""
    return sel if len(sel) <= n else sel[:n - 1] + "…"


# Emoji-glyphs renderen niet op een legacy (cp1252) console/loghandler; we noemen
# ze in de issue-teksten bij hun naam + codepoint (leesbaar én encodeerbaar) en
# houden de échte glyph alleen in `data` en het UTF-8 HTML-blok (report.html = UTF-8).
_EMOJI_NAMES = {
    0x1F680: "raket", 0x1F4DE: "telefoon", 0x260E: "telefoon", 0x1F4F1: "mobiel",
    0x2713: "vinkje", 0x2714: "vinkje", 0x2705: "vinkje", 0x2611: "vinkje",
    0x2717: "kruis", 0x2718: "kruis", 0x274C: "kruis", 0x2716: "kruis",
    0x2605: "ster", 0x2606: "ster", 0x2B50: "ster", 0x1F31F: "ster", 0x2728: "ster",
    0x2192: "pijl-rechts", 0x2190: "pijl-links", 0x2191: "pijl-omhoog", 0x2193: "pijl-omlaag",
    0x27A1: "pijl-rechts", 0x1F4E6: "pakket", 0x1F69A: "vrachtwagen", 0x1F512: "slot",
    0x1F4A1: "lamp", 0x2764: "hart", 0x1F44D: "duim-omhoog", 0x2b07: "pijl-omlaag",
    0x25B6: "play-driehoek", 0x00BB: "dubbele-punthaak",
}
_KLASSE_NAAM = {"pijl": "pijl", "vink": "vinkje", "ster": "ster",
                "bullet": "opsomteken", "picto": "emoji", "symbool": "teken"}


def _emoji_label(ch, klass):
    """Veilige, informatieve naam voor een emoji/teken (geen rauwe glyph)."""
    _, cp = _base_cp(ch)
    name = _EMOJI_NAMES.get(cp) or _KLASSE_NAAM.get(klass, "teken")
    suffix = "-emoji" if klass == "picto" and not name.endswith("emoji") else ""
    return "%s%s (U+%04X)" % (name, suffix, cp)


_SAFE_MAP = {"→": "->", "←": "<-", "↑": "^", "↓": "v", "•": "-", "★": "(ster)",
             "✓": "(vink)", "✔": "(vink)", "✗": "(kruis)", "✘": "(kruis)"}


def _console_safe(s):
    """Vervang tekens die niet naar cp1252 kunnen door een leesbaar token, zodat
    de harness/logging op een legacy Windows-console nooit crasht. report.html is
    UTF-8 en gebruikt de rauwe tekens uit `data`/`html`, dus daar gaat niets verloren."""
    if not isinstance(s, str):
        return s
    try:
        s.encode("cp1252")
        return s
    except Exception:
        out = []
        for ch in s:
            try:
                ch.encode("cp1252")
                out.append(ch)
            except Exception:
                if ch in _SAFE_MAP:
                    out.append(_SAFE_MAP[ch])
                else:
                    out.append("U+%04X" % ord(ch))
        return "".join(out)


# ---------------------------------------------------------------------------
def _load_meta(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def audit(ctx):
    try:
        return _run(ctx)
    except Exception as e:  # laatste vangnet — mag nooit ontsnappen
        try:
            ctx["log"].warning("icons-audit faalde zacht: %s", e)
        except Exception:
            pass
        return {"score": None,
                "summary": "Icon-audit kon niet draaien (zachte fout, zie run.log).",
                "issues": [], "data": {"error": str(e)}}


def _run(ctx):
    pages = ctx.get("pages") or []
    out = ctx.get("out")

    # --- render_meta per pagina inladen (desktop primair, mobiel voor emoji) ---
    metas = []            # (page_url, desktop_icons, mobile_icons)
    pages_with_meta = 0
    pages_missing = 0
    for p in pages:
        sc = p.get("screenshots") or {}
        rel = sc.get("render_meta")
        page_url = p.get("url") or ""
        if not rel:
            pages_missing += 1
            continue
        path = (out / rel) if out is not None else rel
        meta = _load_meta(path)
        if not meta:
            pages_missing += 1
            continue
        vps = meta.get("viewports") or {}
        d = (vps.get("desktop") or {}).get("icons") or []
        m = (vps.get("mobile") or {}).get("icons") or []
        murl = meta.get("url") or page_url
        metas.append((murl, d, m))
        pages_with_meta += 1

    # --- degradatiepad: geen render_meta => niet meetbaar, niet crashen ---
    if not metas:
        note = ("Geen gerenderde iconen-data gevonden (run zonder --screenshots of "
                "module 1.1 uit). Icon-audit meet op de échte render; zonder DOM-snapshot "
                "is de icoon-consistentie niet te bepalen.")
        issues = [{
            "severity": "Low", "category": "icons",
            "title": "Icon-audit niet uitgevoerd — geen render-data",
            "why": note,
            "fix": "Draai de scraper met --screenshots zodat render_meta.json ontstaat.",
            "url": "",
        }] if pages else []
        return {"score": None,
                "summary": "Icon-audit overgeslagen: geen gerenderde iconen-data in deze run.",
                "issues": issues,
                "data": {"pages_gescand": 0, "pages_zonder_meta": pages_missing,
                         "gedegradeerd": True}}

    # -----------------------------------------------------------------------
    # 1. AGGREGATIE
    # -----------------------------------------------------------------------
    families = defaultdict(lambda: {"count": 0, "label": "", "sets": Counter(),
                                    "vind": [], "seen": set()})
    fill_solid = fill_outline = fill_unknown = 0
    stroke_all = Counter()          # alle svg-stroke-px (transparantie)
    stroke_outline = Counter()      # betekenisvol: strokes op lijn-iconen
    per_pagina = {}
    excluded_logos = 0
    excluded_nonicon = 0
    iconfont_fams = Counter()
    iconfont_vind = []
    img_icon_count = 0
    img_vind = []
    # maatvoering per context
    ctx_sizes = defaultdict(list)   # ctxkey -> [(w, page, selector)]

    # emoji
    emoji_icon = {}   # dedup key (char, selector) -> record dict
    emoji_text = {}   # running-text emoji (note-only)

    def _add_family(fkey, label, setname, page, selector):
        f = families[fkey]
        f["count"] += 1
        if not f["label"]:
            f["label"] = label
        if setname:
            f["sets"][setname] += 1
        dk = (selector or "")
        if dk not in f["seen"]:
            f["seen"].add(dk)
            if len(f["vind"]) < 8:
                f["vind"].append({"selector": _short_sel(selector), "page_url": page})

    for page_url, dicons, micons in metas:
        pc = {"svg": 0, "iconfont": 0, "img": 0, "emoji_icon": 0, "families": set()}

        for ic in dicons:
            kind = ic.get("kind")

            if kind == "svg":
                if _is_logo(ic):
                    excluded_logos += 1
                    continue
                w = ic.get("w") or 0
                if isinstance(w, (int, float)) and w > 80:
                    excluded_nonicon += 1
                    continue
                grid, iconish = _grid_family(ic.get("viewBox"))
                if not iconish:
                    excluded_nonicon += 1
                    continue
                setname = _set_from_classes(ic.get("classes"))
                fkey = "svg:" + grid
                label = _GRID_LABEL.get(grid, "inline-SVG %s" % grid)
                _add_family(fkey, label, setname, page_url, ic.get("selector"))
                pc["svg"] += 1
                pc["families"].add(fkey)
                # fill-stijl
                fill = ic.get("fill")
                sp = _stroke_px(ic.get("strokeWidth"))
                if fill == "none":
                    fill_outline += 1
                    if sp is not None:
                        stroke_outline[sp] += 1
                elif fill in (None, "",):
                    fill_unknown += 1
                else:
                    fill_solid += 1
                if sp is not None:
                    stroke_all[sp] += 1
                # maatvoering-context
                anc = ic.get("ancestors") or ""
                ck = _ctx_key(anc)
                if ck and isinstance(w, (int, float)) and w:
                    ctx_sizes[ck].append((float(w), page_url, ic.get("selector")))

            elif kind == "iconfont":
                name = _iconfont_name(ic)
                iconfont_fams[name] += 1
                fkey = "font:" + name
                _add_family(fkey, "icon-font: %s" % name, name, page_url, ic.get("selector"))
                if len(iconfont_vind) < 8:
                    iconfont_vind.append({"selector": _short_sel(ic.get("selector")),
                                          "page_url": page_url, "font": name})
                pc["iconfont"] += 1
                pc["families"].add(fkey)

            elif kind == "img":
                if _is_logo(ic):
                    excluded_logos += 1
                    continue
                img_icon_count += 1
                _add_family("img", "losse afbeeldings-iconen", None, page_url, ic.get("selector"))
                if len(img_vind) < 8:
                    img_vind.append({"selector": _short_sel(ic.get("selector")),
                                     "page_url": page_url, "src": ic.get("src")})
                pc["img"] += 1
                pc["families"].add("img")

        # emoji: desktop + mobile (dedupe), classificeren
        for src_icons in (dicons, micons):
            for ic in src_icons:
                if ic.get("kind") != "emoji":
                    continue
                _classify_emoji(ic, page_url, emoji_icon, emoji_text)
        pc["emoji_icon"] = sum(1 for r in emoji_icon.values() if r["page_url"] == page_url)
        pc["families"] = sorted(pc["families"])
        per_pagina[page_url] = {"svg": pc["svg"], "iconfont": pc["iconfont"],
                                "img": pc["img"], "emoji_als_icoon": pc["emoji_icon"]}

    total_icons = fill_solid + fill_outline + fill_unknown + \
        sum(iconfont_fams.values()) + img_icon_count
    hard_emoji = [r for r in emoji_icon.values() if r["klasse"] in ("picto", "vink", "ster")]
    soft_emoji = [r for r in emoji_icon.values() if r["klasse"] in ("pijl", "symbool", "bullet")]

    # -----------------------------------------------------------------------
    # 2. FAMILIES: significant vs stray
    # -----------------------------------------------------------------------
    fam_list = sorted(families.items(), key=lambda kv: -kv[1]["count"])
    thresh = max(2, int(round(0.05 * max(1, total_icons))))
    significant = [(k, v) for k, v in fam_list
                   if v["count"] >= thresh or k.startswith("font:") or
                   (k == "img" and v["count"] >= 3)]
    if not significant and fam_list:            # alles klein -> pak de grootste
        significant = fam_list[:1]
    n_families = len(significant)

    issues = []

    # -----------------------------------------------------------------------
    # 3. ISSUES
    # -----------------------------------------------------------------------
    # (A) EMOJI-ALS-ICOON  — operator-irritatie #1
    if hard_emoji or soft_emoji:
        picks = (hard_emoji + soft_emoji)
        picks_sorted = sorted(picks, key=lambda r: (0 if r["klasse"] in ("picto", "vink", "ster") else 1,
                                                     -r["pages"]))
        vind_txt = "; ".join(
            "%s in %s (%d pag.)" % (_emoji_label(r["char"], r["klasse"]),
                                    _short_sel(r["selector"], 60), r["pages"])
            for r in picks_sorted[:8])
        sev = "High" if hard_emoji else "Medium"
        labels = ", ".join(sorted({_emoji_label(r["char"], r["klasse"]) for r in picks}))
        issues.append({
            "severity": sev, "category": "icons",
            "title": "Emoji/tekens als icoon gebruikt (%d plek%s)" % (
                len(picks), "" if len(picks) == 1 else "ken"),
            "why": ("Emoji-als-icoon (%s) ziet er goedkoop/AI-achtig uit, rendert per "
                    "toestel/OS anders en is niet schaalbaar met je huisstijl. "
                    "Vindplaatsen: %s." % (labels, vind_txt)),
            "fix": ("Vervang elke emoji-icoon door een SVG-glyph uit één vaste set "
                    "(pijl naar chevron-SVG, vinkje naar check-SVG, ster naar rating-SVG) "
                    "in je accentkleur. Nul emoji-iconen is de norm."),
            "url": picks_sorted[0]["page_url"],
        })

    # (B) GEMIXTE ICON-SETS
    if n_families >= 2:
        dom_k, dom_v = significant[0]
        others = significant[1:]
        others_txt = "; ".join(
            "%s: %d× (bv. %s op %s)" % (
                v["label"], v["count"],
                (v["vind"][0]["selector"] if v["vind"] else "?"),
                (v["vind"][0]["page_url"] if v["vind"] else "?"))
            for _, v in others[:4])
        issues.append({
            "severity": "Medium", "category": "icons",
            "title": "%d verschillende icoon-sets door elkaar" % n_families,
            "why": ("Dominant is '%s' (%d iconen). Daarnaast draaien: %s. "
                    "Gemixte sets ogen rommelig/onprofessioneel — iconen verschillen "
                    "in lijndikte, hoekstraal en optische maat." % (
                        dom_v["label"], dom_v["count"], others_txt)),
            "fix": ("Kies één set (het dominante '%s') en zet de afwijkende iconen om. "
                    "Eén set = één consistente uitstraling." % dom_v["label"]),
            "url": (dom_v["vind"][0]["page_url"] if dom_v["vind"] else ""),
        })

    # (C) ICON-FONT (verouderd)
    if iconfont_fams:
        fonts = ", ".join("%s (%d×)" % (n, c) for n, c in iconfont_fams.most_common())
        issues.append({
            "severity": "Low", "category": "icons",
            "title": "Icon-font in gebruik: %s" % ", ".join(iconfont_fams.keys()),
            "why": ("Icon-fonts (%s) zijn verouderd: ze geven een FOIT/FOUT-flits bij "
                    "laden, kunnen niet multicolor en zijn slecht voor toegankelijkheid "
                    "(worden door screenreaders soms voorgelezen)." % fonts),
            "fix": "Vervang de icon-font door inline-SVG-iconen uit één set.",
            "url": (iconfont_vind[0]["page_url"] if iconfont_vind else ""),
        })

    # (D) FILL/STROKE-STIJL GEMIXT (outline naast solide)
    fs_total = fill_solid + fill_outline
    if fill_solid > 0 and fill_outline > 0:
        minority = min(fill_solid, fill_outline)
        share = minority / fs_total
        mtype = "lijn (outline)" if fill_outline < fill_solid else "solide (fill)"
        if share >= 0.25 and minority >= 3:
            issues.append({
                "severity": "Medium", "category": "icons",
                "title": "Solide en lijn-iconen door elkaar",
                "why": ("%d solide (gevulde) en %d lijn-iconen op de site — dat zijn twee "
                        "visuele stijlen. De minderheid (%s, %d×) valt uit de toon." % (
                            fill_solid, fill_outline, mtype, minority)),
                "fix": "Kies één stijl (alles solide óf alles lijn) en trek de %d afwijkende iconen recht." % minority,
                "url": "",
            })
        elif share >= 0.10 and minority >= 3:
            issues.append({
                "severity": "Low", "category": "icons",
                "title": "Kleine mix van solide en lijn-iconen",
                "why": ("%d solide vs %d lijn-iconen (%.0f%% minderheid). Kan bewust zijn "
                        "(lege sterren/pijlen), maar check of het één set blijft." % (
                            fill_solid, fill_outline, share * 100)),
                "fix": "Houd één stijl aan; gebruik binnen dezelfde set alleen bewuste outline-varianten.",
                "url": "",
            })

    # (E) STROKE-DIKTE INCONSISTENT (op lijn-iconen)
    if stroke_outline:
        meaningful = sum(stroke_outline.values())
        common = stroke_outline.most_common()
        if len(common) >= 2 and common[1][1] / meaningful >= 0.25 and common[1][1] >= 3:
            verdeling = ", ".join("%.2fpx (%d×)" % (v, c) for v, c in common[:4])
            issues.append({
                "severity": "Medium", "category": "icons",
                "title": "Meerdere lijndiktes door elkaar",
                "why": ("Je lijn-iconen gebruiken verschillende strokeWidths: %s. Verschillende "
                        "diktes maken iconen optisch ongelijk." % verdeling),
                "fix": "Normaliseer alle lijn-iconen op één strokeWidth (bv. 1.5px).",
                "url": "",
            })

    # (F) MAATVOERING binnen dezelfde rij (conservatief)
    size_flags = []
    for ck, arr in ctx_sizes.items():
        if len(arr) < 3:
            continue
        ws = [w for w, _, _ in arr]
        lo, hi = min(ws), max(ws)
        if lo > 0 and hi / lo >= 1.8 and (hi - lo) >= 10:
            size_flags.append((ck, lo, hi, arr[0][1]))
    if size_flags:
        sf = size_flags[:3]
        txt = "; ".join("%s: %d–%dpx" % (ck, int(lo), int(hi)) for ck, lo, hi, _ in sf)
        issues.append({
            "severity": "Low", "category": "icons",
            "title": "Ongelijke icoonmaten binnen dezelfde rij",
            "why": "Binnen dezelfde context lopen icoonmaten sterk uiteen: %s." % txt,
            "fix": "Geef iconen in dezelfde rij één vaste weergavemaat.",
            "url": sf[0][3],
        })

    # -----------------------------------------------------------------------
    # 4. SCORE
    # -----------------------------------------------------------------------
    if total_icons == 0 and not emoji_icon:
        score = None   # geen echte iconen én geen emoji-als-icoon = niet van toepassing
    else:
        score = 100.0
        score -= min(48, len(hard_emoji) * 8)
        score -= min(20, len(soft_emoji) * 4)
        if n_families >= 2:
            score -= min(30, (n_families - 1) * 12)
        if fill_solid and fill_outline:
            share = min(fill_solid, fill_outline) / (fill_solid + fill_outline)
            if share >= 0.25 and min(fill_solid, fill_outline) >= 3:
                score -= 10
            elif share >= 0.10 and min(fill_solid, fill_outline) >= 3:
                score -= 4
        if stroke_outline:
            common = stroke_outline.most_common()
            if len(common) >= 2 and common[1][1] / sum(stroke_outline.values()) >= 0.25 and common[1][1] >= 3:
                score -= 8
        if iconfont_fams:
            score -= 6
        if size_flags:
            score -= 4
        score = max(0.0, round(score, 1))

    # -----------------------------------------------------------------------
    # 5. ADVIES ("één set per site")
    # -----------------------------------------------------------------------
    advies = _advies(significant, families, hard_emoji, soft_emoji, fill_solid,
                     fill_outline, iconfont_fams, total_icons)

    # -----------------------------------------------------------------------
    # 6. SAMENVATTING (afgeleid van de daadwerkelijk gemelde issues)
    # -----------------------------------------------------------------------
    n_emoji_icon = len(emoji_icon)
    _sevrank = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}
    maxsev = max((_sevrank.get(i["severity"], 0) for i in issues), default=0)
    dom_lab = significant[0][1]["label"] if significant else "?"
    dom_cnt = significant[0][1]["count"] if significant else 0
    parts = []
    if n_emoji_icon:
        parts.append("%d emoji-icoon-plek%s" % (n_emoji_icon, "" if n_emoji_icon == 1 else "ken"))
    if n_families >= 2:
        parts.append("%d sets door elkaar" % n_families)
    if iconfont_fams:
        parts.append("icon-font aanwezig")
    if fill_solid and fill_outline and \
            min(fill_solid, fill_outline) / (fill_solid + fill_outline) >= 0.10 \
            and min(fill_solid, fill_outline) >= 3:
        parts.append("solide/lijn-mix (%d vs %d)" % (fill_solid, fill_outline))
    if any(i["title"].startswith("Meerdere lijndiktes") for i in issues):
        parts.append("wisselende lijndiktes")
    if size_flags:
        parts.append("ongelijke icoonmaten")

    if score is None:
        summary = "Geen iconen aangetroffen op de gerenderde pagina's."
    elif not issues:
        summary = ("Eén consistente set: %d iconen, %s. Geen emoji-iconen, geen "
                   "gemixte bronnen." % (dom_cnt, dom_lab))
    elif maxsev <= 1:  # alleen kleine (Low) aandachtspunten
        summary = ("In de kern één set (%s, %d iconen); klein aandachtspunt: %s."
                   % (dom_lab, dom_cnt, ", ".join(parts) or "zie issues"))
    else:
        summary = "Icon-consistentie: " + ", ".join(parts) + " — standaardiseer op één set."

    # -----------------------------------------------------------------------
    # 7. DATA
    # -----------------------------------------------------------------------
    families_out = {}
    for k, v in fam_list:
        families_out[k] = {
            "label": v["label"], "count": v["count"],
            "significant": any(k == sk for sk, _ in significant),
            "sets": dict(v["sets"]), "vindplaatsen": v["vind"],
        }
    emoji_sites = [{"char": r["char"], "selector": r["selector"],
                    "page_url": r["page_url"], "text": r["text"],
                    "klasse": r["klasse"], "pages": r["pages"]}
                   for r in sorted(emoji_icon.values(),
                                   key=lambda r: (0 if r["klasse"] in ("picto", "vink", "ster") else 1,
                                                  -r["pages"]))]
    data = {
        "pages_gescand": pages_with_meta,
        "pages_zonder_meta": pages_missing,
        "totaal_iconen": total_icons,
        "aantal_families": n_families,
        "families": families_out,
        "emoji_sites": emoji_sites,
        "emoji_in_tekst_count": len(emoji_text),
        "emoji_in_tekst_voorbeelden": [
            {"char": r["char"], "text": r["text"], "selector": r["selector"],
             "page_url": r["page_url"]}
            for r in list(emoji_text.values())[:6]],
        "fill_verdeling": {"solide": fill_solid, "lijn": fill_outline,
                           "onbekend": fill_unknown},
        "stroke_verdeling": {("%.2fpx" % k): v for k, v in sorted(stroke_all.items())},
        "stroke_verdeling_lijn": {("%.2fpx" % k): v for k, v in sorted(stroke_outline.items())},
        "iconfonts": dict(iconfont_fams),
        "img_iconen": img_icon_count,
        "uitgesloten_logos": excluded_logos,
        "uitgesloten_niet-icoon": excluded_nonicon,
        "per_pagina": per_pagina,
        "advies": advies,
    }

    # Console/log-veilig maken (report.html blijft UTF-8 via `html` + `data`).
    for it in issues:
        for k in ("title", "why", "fix"):
            if k in it:
                it[k] = _console_safe(it[k])
    summary = _console_safe(summary)

    return {"score": score, "summary": summary, "issues": issues, "data": data,
            "html": _html(significant, families, emoji_sites, fill_solid, fill_outline,
                          stroke_all, iconfont_fams, advies, score)}


# ---------------------------------------------------------------------------
def _ctx_key(ancestors):
    """Signatuur van de rij/container waarin een icoon zit (voor maatvoering)."""
    if not ancestors:
        return ""
    toks = [t.strip() for t in ancestors.split("<")]
    # zoek een container die op een rij/grid lijkt
    for t in toks[1:]:
        low = t.lower()
        if any(x in low for x in ("grid", "row", "list", "band", "bar", "promises",
                                  "features", "usp", "cards", "menu", "nav")):
            return t.split(".")[-1] if "." in t else t
    # anders directe ouder
    return (toks[1].split(".")[-1] if len(toks) > 1 and "." in toks[1]
            else (toks[1] if len(toks) > 1 else ""))


def _iconfont_name(rec):
    ff = (rec.get("fontFamily") or "").lower()
    cls = (rec.get("classes") or "").lower()
    for key, name in [("font awesome", "FontAwesome"), ("fontawesome", "FontAwesome"),
                      ("material icons", "Material Icons"), ("material symbols", "Material Symbols"),
                      ("bootstrap-icons", "Bootstrap Icons"), ("glyphicons", "Glyphicons"),
                      ("ionicons", "Ionicons"), ("themify", "Themify"),
                      ("dashicons", "Dashicons"), ("icomoon", "IcoMoon"),
                      ("feather", "Feather"), ("typicons", "Typicons"),
                      ("elegant", "Elegant Icons")]:
        if key in ff or key in cls:
            return name
    s = _set_from_classes(cls)
    if s:
        return s
    return (rec.get("fontFamily") or "onbekende icon-font").strip() or "onbekende icon-font"


def _classify_emoji(ic, page_url, emoji_icon, emoji_text):
    char = ic.get("char") or ""
    if not char:
        return
    selector = ic.get("selector") or ""
    text = (ic.get("text") or "")
    short = bool(ic.get("shortText"))
    klass = _char_class(char)
    stripped = text.strip()
    standalone = short or stripped == char or (len(stripped) <= 2)
    leading = stripped.startswith(char)
    ctx_icon = _icon_context(selector)

    is_icon = False
    if standalone:
        is_icon = True
    elif klass in ("picto", "vink", "ster") and leading and (ctx_icon or len(stripped) <= 28):
        is_icon = True
    elif klass == "picto" and ctx_icon:
        is_icon = True

    dk = (char, selector)
    bucket = emoji_icon if is_icon else emoji_text
    if dk in bucket:
        bucket[dk]["pages"] += 1
    else:
        bucket[dk] = {"char": char, "selector": selector, "page_url": page_url,
                      "text": stripped[:40], "klasse": klass, "pages": 1}


def _advies(significant, families, hard_emoji, soft_emoji, fill_solid, fill_outline,
            iconfont_fams, total_icons):
    if not significant:
        if hard_emoji or soft_emoji:
            return ("Er draait nog geen echte icoon-set — alleen %d emoji/tekens als icoon. "
                    "Kies één inline-SVG-set (bv. 24-grid lijn-iconen) in je accentkleur en "
                    "vervang alle emoji daardoor." % (len(hard_emoji) + len(soft_emoji)))
        return "Geen iconen aangetroffen; niets te standaardiseren."
    dom_k, dom = significant[0]
    consistent = (len(significant) == 1 and not (hard_emoji or soft_emoji) and not iconfont_fams
                  and not (fill_solid and fill_outline and
                           min(fill_solid, fill_outline) / max(1, fill_solid + fill_outline) >= 0.10))
    if consistent:
        return ("De site draait al op één set: %d van %d iconen zijn %s. Houd dit vast — "
                "voeg geen andere icoon-bron toe en gebruik overal dezelfde accentkleur."
                % (dom["count"], total_icons, dom["label"]))
    todo = []
    for _, v in significant[1:]:
        ex = (" (bv. %s op %s)" % (v["vind"][0]["selector"], v["vind"][0]["page_url"])
              if v["vind"] else "")
        todo.append("%d× %s%s" % (v["count"], v["label"], ex))
    if iconfont_fams:
        todo.append("icon-font %s → inline-SVG" % "/".join(iconfont_fams.keys()))
    n_emoji = len(hard_emoji) + len(soft_emoji)
    if n_emoji:
        todo.append("%d emoji-icoon → SVG-glyph" % n_emoji)
    if fill_solid and fill_outline:
        todo.append("solide/lijn gelijktrekken (%d vs %d)" % (fill_solid, fill_outline))
    return ("Standaardiseer op de dominante set: %d van %d iconen zijn %s. "
            "Zet om: %s. Eindresultaat: één set, één stijl, eigen accentkleur, nul emoji-iconen."
            % (dom["count"], total_icons, dom["label"], "; ".join(todo)))


def _html(significant, families, emoji_sites, fill_solid, fill_outline, stroke_all,
          iconfont_fams, advies, score):
    try:
        rows = []
        for k, v in sorted(families.items(), key=lambda kv: -kv[1]["count"]):
            sig = any(k == sk for sk, _ in significant)
            rows.append(
                "<tr><td style='padding:4px 8px'>%s%s</td>"
                "<td style='padding:4px 8px;text-align:right'>%d</td>"
                "<td style='padding:4px 8px;color:#667'>%s</td></tr>" % (
                    v["label"], "" if sig else " <span style='color:#aab'>(los)</span>",
                    v["count"],
                    (v["vind"][0]["selector"] if v["vind"] else "")))
        fillbar = ""
        if fill_solid or fill_outline:
            fillbar = ("<p style='margin:6px 0;color:#334'>Fill-stijl: "
                       "<b>%d</b> solide · <b>%d</b> lijn</p>" % (fill_solid, fill_outline))
        emoji_html = ""
        if emoji_sites:
            lis = "".join(
                "<li><code>%s</code> — %s <span style='color:#889'>(%s)</span></li>" % (
                    e["char"], e["selector"], e["klasse"])
                for e in emoji_sites[:10])
            emoji_html = ("<p style='margin:8px 0 2px;color:#a33'><b>Emoji als icoon:</b></p>"
                          "<ul style='margin:0 0 6px 18px;padding:0;color:#334'>%s</ul>" % lis)
        return (
            "<div style='font:14px/1.5 system-ui,sans-serif;color:#223'>"
            "<table style='border-collapse:collapse;margin:6px 0;font-size:13px'>"
            "<thead><tr style='background:#f3f5f8'>"
            "<th style='text-align:left;padding:4px 8px'>Icoon-familie</th>"
            "<th style='padding:4px 8px'>#</th>"
            "<th style='text-align:left;padding:4px 8px'>voorbeeld</th></tr></thead>"
            "<tbody>%s</tbody></table>%s%s"
            "<p style='margin:8px 0 0;padding:8px 10px;background:#eef6f1;border-left:3px solid #1f6b4c'>"
            "<b>Advies:</b> %s</p></div>" % (
                "".join(rows), fillbar, emoji_html, advies))
    except Exception:
        return ""
