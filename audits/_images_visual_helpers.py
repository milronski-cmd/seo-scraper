# -*- coding: utf-8 -*-
"""
Helpers voor de beeld-audit (module 1.3, KEY=images_visual).

Bevat de zware pixel-analyse zodat images_visual.py leesbaar blijft. Alles is
fail-soft: een kapot bestand levert een {"error": ...}-dict op, nooit een
exception. Dependencies: Pillow (PIL) + numpy — beide fail-soft geimporteerd;
ontbreekt er een, dan degraderen we naar dimensie-/metadata-metingen.
"""
import hashlib
import re
from urllib.parse import urlparse

# --- fail-soft dependency-import (Pillow + numpy) ---------------------------
try:
    from PIL import Image  # type: ignore
    _HAS_PIL = True
except Exception:  # pragma: no cover - alleen als Pillow ontbreekt
    Image = None
    _HAS_PIL = False

try:
    import numpy as np  # type: ignore
    _HAS_NP = True
except Exception:  # pragma: no cover
    np = None
    _HAS_NP = False

PIXELS_AVAILABLE = _HAS_PIL and _HAS_NP


# --- safe_name-spiegels van de scraper ---------------------------------------
# ACTUEEL formaat (v2.3.1+, mét pad-hash — fixt de submap-botsing):
def safe_name(url):
    pu = urlparse(url or "")
    tag = hashlib.md5((pu.path + ("?" + pu.query if pu.query else ""))
                      .encode("utf-8", "ignore")).hexdigest()[:8]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", pu.path.split("/")[-1])[:60]
    if not base or "." not in base:
        return (base or "img") + "_" + tag + ".bin"
    stem, _, ext = base.rpartition(".")
    return (stem or "img") + "_" + tag + "." + ext


# LEGACY-formaat (t/m v2.3.0, laatste padsegment) — voor oudere run-output:
def _legacy_safe_name(url):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", urlparse(url or "").path.split("/")[-1])[:80]
    if not name or "." not in name:
        name = (name or "img") + "_" + hashlib.md5((url or "").encode()).hexdigest()[:8] + ".bin"
    return name


def candidate_names(src, ctx_safe_name=None):
    """Bestandsnaam-kandidaten voor de lokale images/-map, in volgorde:
    de actuele scraper-functie (via ctx doorgegeven als die er is), de
    actuele spiegel, en het legacy-formaat (oudere runs). Elk op de volle
    src én de src zonder ?query."""
    fns = ([ctx_safe_name] if callable(ctx_safe_name) else []) + [safe_name, _legacy_safe_name]
    out = []
    for fn in fns:
        for u in (src, (src or "").split("?")[0]):
            try:
                n = fn(u)
            except Exception:
                continue
            if n and n not in out:
                out.append(n)
    return out


# --- kleur / luminantie -----------------------------------------------------
def parse_rgb(s):
    """'rgb(18, 20, 23)' -> (18,20,23); None/onparse -> None."""
    if not s or not isinstance(s, str):
        return None
    nums = re.findall(r"\d+", s)
    if len(nums) < 3:
        return None
    return tuple(int(x) for x in nums[:3])


def _lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def rel_luminance(rgb):
    """WCAG relative luminance 0..1; None -> None."""
    if not rgb:
        return None
    r, g, b = rgb[:3]
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ext_format(src):
    """Bestandsformaat uit de src-extensie (weerspiegelt wat geserveerd wordt,
    ongeacht de lokale kopie): 'webp' | 'avif' | 'png' | 'jpg' | 'svg' | ..."""
    path = urlparse(src or "").path.lower()
    m = re.search(r"\.([a-z0-9]{2,4})$", path)
    if not m:
        return ""
    e = m.group(1)
    return "jpg" if e == "jpeg" else e


# --- perceptual hash (8x8 aHash op grijswaarden) ----------------------------
def ahash64(gray_small):
    """gray_small: 8x8 numpy grijswaarde-array -> 64-bit int aHash."""
    mean = gray_small.mean()
    bits = (gray_small >= mean).flatten()
    h = 0
    for b in bits:
        h = (h << 1) | int(bool(b))
    return h


def hamming(a, b):
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


# --- kern: pixel-analyse van 1 bestand --------------------------------------
def analyze_image_file(path, max_w=400, dist_thr=30):
    """Analyseer een lokaal beeldbestand.

    Retourneert dict:
      {ok, fill_pct, bg_rgb, bg_lum, transparent, has_alpha,
       file_w, file_h, pil_format, ahash, watermark_hint, error}
    fill_pct = oppervlak van de product-bounding-box / canvas * 100.

    Aanpak (zie wiring-notitie voor kalibratie):
      * echte transparantie -> bbox van alpha>10 (transparant = geen eigen bg = goed);
      * anders -> achtergrond = mediaan van de 2-3px rand; product = pixels met
        euclidische RGB-afstand > drempel; robuuste bbox via 0.4/99.6-percentiel
        van de maskcoordinaten (negeert JPEG-ruis/stray pixels).
    Grote beelden worden eerst verkleind (max_w) voor snelheid.
    """
    if not PIXELS_AVAILABLE:
        return {"ok": False, "error": "pixels niet gemeten (Pillow/numpy ontbreekt)"}
    try:
        im = Image.open(path)
        im.load()
    except Exception as e:
        return {"ok": False, "error": f"kon beeld niet openen: {e}"}

    out = {"ok": True, "pil_format": (im.format or "").lower(),
           "file_w": im.width, "file_h": im.height,
           "fill_pct": None, "bg_rgb": None, "bg_lum": None,
           "transparent": False, "has_alpha": False,
           "ahash": None, "watermark_hint": False, "error": None}
    try:
        # verklein voor snelheid (behoud alpha)
        if im.width > max_w:
            nh = max(1, int(round(max_w * im.height / im.width)))
            im = im.resize((max_w, nh), Image.LANCZOS)

        has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
        out["has_alpha"] = bool(has_alpha)

        alpha = None
        if has_alpha:
            rgba = im.convert("RGBA")
            alpha = np.asarray(rgba.split()[-1])
            transp_frac = float((alpha < 250).mean())
            rgb = np.asarray(rgba.convert("RGB")).astype(np.int16)
        else:
            transp_frac = 0.0
            rgb = np.asarray(im.convert("RGB")).astype(np.int16)

        H, W = rgb.shape[0], rgb.shape[1]
        canvas = float(H * W) or 1.0

        if has_alpha and transp_frac >= 0.03:
            # echte cutout: bbox van niet-transparante pixels
            out["transparent"] = True
            ys, xs = np.where(alpha > 10)
            if len(xs):
                bb = (xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1)
                out["fill_pct"] = round(100.0 * bb / canvas, 1)
            else:
                out["fill_pct"] = 0.0
            # bg is transparant -> geen eigen achtergrondkleur (goed, niet wit-op-donker)
            gray_full = np.asarray(rgba.convert("L"))
        else:
            # opake foto: rand-mediaan = achtergrond
            b = 3 if min(H, W) > 12 else 1
            border = np.concatenate([
                rgb[:b, :, :].reshape(-1, 3), rgb[-b:, :, :].reshape(-1, 3),
                rgb[:, :b, :].reshape(-1, 3), rgb[:, -b:, :].reshape(-1, 3)])
            bg = np.median(border, axis=0)
            out["bg_rgb"] = tuple(int(round(c)) for c in bg.tolist())
            out["bg_lum"] = rel_luminance(out["bg_rgb"])
            dist = np.sqrt(((rgb - bg) ** 2).sum(axis=2))
            mask = dist > dist_thr
            ys, xs = np.where(mask)
            if len(xs) > 20:
                # robuuste bbox: negeer de uiterste 0.4% ruis-pixels
                x0, x1 = np.percentile(xs, [0.4, 99.6])
                y0, y1 = np.percentile(ys, [0.4, 99.6])
                bb = (x1 - x0 + 1) * (y1 - y0 + 1)
                out["fill_pct"] = round(min(100.0, 100.0 * bb / canvas), 1)
            elif len(xs):
                out["fill_pct"] = round(100.0 * len(xs) / canvas, 1)
            else:
                out["fill_pct"] = 0.0
            gray_full = np.asarray(im.convert("L"))

        # aHash (8x8)
        try:
            g8 = np.asarray(Image.fromarray(gray_full.astype(np.uint8)).resize((8, 8), Image.LANCZOS),
                            dtype=np.float32)
            out["ahash"] = ahash64(g8)
        except Exception:
            out["ahash"] = None

        # conservatieve watermerk-heuristiek
        try:
            out["watermark_hint"] = _watermark_hint(gray_full)
        except Exception:
            out["watermark_hint"] = False
    except Exception as e:
        out["error"] = f"analyse mislukt: {e}"
    return out


def _watermark_hint(gray):
    """Conservatieve heuristiek: zoekt tekst-achtige, contrastrijke structuren in
    hoek-/randzones (typisch voor watermerken/overlay-tekst). Bewust streng
    afgesteld -> liever ondergerapporteerd dan een false-positive-lawine.
    Signaal alleen als een hoekzone veel meer fijne randen heeft dan de rest van
    het beeld EN de zone verder relatief egaal is (tekst = veel randen, weinig
    kleurspreiding)."""
    if gray is None or gray.ndim != 2:
        return False
    H, W = gray.shape
    if H < 60 or W < 60:
        return False
    g = gray.astype(np.float32)
    gx = np.abs(np.diff(g, axis=1))
    gy = np.abs(np.diff(g, axis=0))
    # rand-magnitude op gemeenschappelijk raster
    edge = np.zeros((H, W), np.float32)
    edge[:, :-1] += gx
    edge[:-1, :] += gy
    T = 40.0  # sterke lokale contrastsprong
    edges = edge > T
    global_density = float(edges.mean())
    zh, zw = max(8, int(H * 0.16)), max(8, int(W * 0.16))
    zones = {
        "lb": edges[H - zh:H, 0:zw], "rb": edges[H - zh:H, W - zw:W],
        "lt": edges[0:zh, 0:zw], "rt": edges[0:zh, W - zw:W],
        "bc": edges[H - zh:H, (W - zw) // 2:(W + zw) // 2],
    }
    for z in zones.values():
        d = float(z.mean())
        # streng: hoge lokale randdichtheid EN duidelijk boven de rest van het beeld
        # (bewust hoog afgesteld; op de referentie-sets levert dit 0 hints op)
        if d > 0.20 and d > (global_density * 5.0 + 0.04):
            return True
    return False
