#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
MODULE 1.1 — Screenshot-capture (plan "beste websites" §4.1, fase 1).

Per gescrapete pagina, via Playwright headless-Chromium:
  - desktop 1440x900 : above-the-fold PNG + full-page PNG
  - mobiel  390x844  : above-the-fold PNG + full-page PNG (echte mobiele context:
                       mobiele UA, is_mobile, touch, DSF 2 voor scherpte)
  - dom.html         : gerenderde DOM ná JS (desktop-render, ná lazy-load-scroll)
                       -> hierop draaien de audits 1.2/1.3/1.7 (échte render,
                       niet de kale requests-HTML)
  - meta.json        : url, tijdstip, viewports, bestandsnamen, note

Opslag: <site-out>\screenshots\<slug>\  (slug = zelfde regex als content/*.txt,
plus korte hash bij query-URL's zodat varianten elkaar niet overschrijven).

Ontwerpprincipes (INTEGRATION.md):
  - FAIL-SOFT per pagina: een kapotte pagina geeft een record met "note",
    nooit een crash van de run.
  - ÉÉN browser + twee herbruikbare contexts (desktop/mobiel) voor de hele
    site — geen context-per-pagina (performance).
  - Schrijft per pagina het veld  page["screenshots"]  in-place (relatieve
    paden t.o.v. de site-outputmap) en levert een site-manifest terug.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

# --- render-metadata-extractie (voedt audits 1.3 beeld / 1.4 icons / 1.7 contrast)
# Draait in de pagina ná shots+lazy-scroll; alles gecapt en fail-soft.
EXTRACT_JS = r"""
() => {
  const CAP_TEXTS = 400, CAP_IMGS = 250, CAP_ICONS = 250;
  const out = {images: [], texts: [], icons: [], truncated: {}};
  const seenText = new Set();

  const vis = (el) => {
    try {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return null;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return null;
      return r;
    } catch (e) { return null; }
  };

  const sel = (el) => {
    try {
      const parts = [];
      let n = el;
      for (let i = 0; n && n.nodeType === 1 && i < 4; i++) {
        let p = n.tagName.toLowerCase();
        if (n.id) { parts.unshift(p + '#' + n.id); break; }
        const cls = (typeof n.className === 'string') ? n.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
        if (cls) p += '.' + cls;
        parts.unshift(p);
        n = n.parentElement;
      }
      return parts.join('>').slice(0, 160);
    } catch (e) { return '?'; }
  };

  // effectieve achtergrond: loop omhoog tot niet-transparante bg-color;
  // een background-image onderweg => bgImage:true (kleur onbekend)
  const effBg = (el) => {
    try {
      let n = el, hops = 0;
      while (n && n !== document.documentElement && hops < 30) {
        const cs = getComputedStyle(n);
        if (cs.backgroundImage && cs.backgroundImage !== 'none') return {bg: null, bgImage: true};
        const c = cs.backgroundColor;
        const m = c && c.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
        if (m && (m[4] === undefined || parseFloat(m[4]) >= 0.99)) return {bg: c, bgImage: false};
        n = n.parentElement; hops++;
      }
      const b = getComputedStyle(document.body).backgroundColor;
      const mb = b && b.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
      return {bg: (mb && (mb[4] === undefined || parseFloat(mb[4]) >= 0.99)) ? b : 'rgb(255, 255, 255)', bgImage: false};
    } catch (e) { return {bg: null, bgImage: false}; }
  };

  const ancChain = (el) => {
    try {
      const c = [];
      let n = el.parentElement;
      for (let i = 0; n && i < 4; i++) {
        const cls = (typeof n.className === 'string') ? n.className.trim().split(/\s+/).slice(0, 3).join('.') : '';
        c.push((n.tagName || '?').toLowerCase() + (cls ? '.' + cls : ''));
        n = n.parentElement;
      }
      return c.join(' < ').slice(0, 200);
    } catch (e) { return ''; }
  };

  // ---------- IMAGES ----------
  const imgs = [...document.querySelectorAll('img')];
  for (const im of imgs) {
    if (out.images.length >= CAP_IMGS) { out.truncated.images = imgs.length; break; }
    const r = vis(im);
    if (!r) continue;
    const bgi = effBg(im);
    out.images.push({
      src: (im.currentSrc || im.src || '').slice(0, 400),
      alt: (im.getAttribute('alt') || '').slice(0, 160),
      naturalWidth: im.naturalWidth, naturalHeight: im.naturalHeight,
      displayW: Math.round(r.width), displayH: Math.round(r.height),
      x: Math.round(r.x + scrollX), y: Math.round(r.y + scrollY),
      cardBg: bgi.bg, cardBgImage: bgi.bgImage,
      classes: ((typeof im.className === 'string') ? im.className : '').slice(0, 120),
      ancestors: ancChain(im),
      inLink: !!im.closest('a'),
      loading: im.getAttribute('loading') || '',
    });
  }

  // ---------- TEXTS (elementen met eigen text-nodes) ----------
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let el, nTexts = 0, totalCandidates = 0;
  while ((el = walker.nextNode())) {
    const tag = el.tagName.toLowerCase();
    if (['script','style','noscript','svg','path','template','iframe'].includes(tag)) continue;
    let own = '';
    for (const c of el.childNodes) if (c.nodeType === 3) own += c.textContent;
    own = own.replace(/\s+/g, ' ').trim();
    if (own.length < 2) continue;
    totalCandidates++;
    if (nTexts >= CAP_TEXTS) { out.truncated.texts = totalCandidates; continue; }
    const r = vis(el);
    if (!r) continue;
    const key = sel(el) + '|' + own.slice(0, 40);
    if (seenText.has(key)) continue;
    seenText.add(key);
    const cs = getComputedStyle(el);
    const bgi = effBg(el);
    out.texts.push({
      text: own.slice(0, 90), tag: tag, selector: sel(el),
      fontSize: parseFloat(cs.fontSize) || null,
      fontWeight: cs.fontWeight, color: cs.color,
      bg: bgi.bg, bgImage: bgi.bgImage,
      w: Math.round(r.width), h: Math.round(r.height),
      x: Math.round(r.x + scrollX), y: Math.round(r.y + scrollY),
    });
    nTexts++;
  }

  // ---------- ICONS ----------
  const iconFontRe = /(^|\s)(fa[srlbd]?|fa-|bi-|icon-|icon\b|lucide|feather|tabler-|ph-|material-icons|material-symbols|glyphicon|mdi-|ionicon)/i;
  // inline svg's
  const svgs = [...document.querySelectorAll('svg')];
  for (const sv of svgs) {
    if (out.icons.length >= CAP_ICONS) { out.truncated.icons = true; break; }
    const r = vis(sv);
    if (!r || r.width > 120 || r.height > 120) continue;   // icoon-formaat
    let stroke = null, fill = null;
    try {
      const shape = sv.querySelector('path,line,circle,rect,polyline,polygon');
      if (shape) { const scs = getComputedStyle(shape); stroke = scs.strokeWidth || null; fill = scs.fill || null; }
    } catch (e) {}
    out.icons.push({kind: 'svg', selector: sel(sv), w: Math.round(r.width), h: Math.round(r.height),
                    strokeWidth: stroke, fill: (fill || '').slice(0, 40),
                    viewBox: (sv.getAttribute('viewBox') || '').slice(0, 40),
                    useHref: (sv.querySelector('use') ? (sv.querySelector('use').getAttribute('href') || sv.querySelector('use').getAttribute('xlink:href') || '') : '').slice(0, 120),
                    classes: ((typeof sv.className === 'object' ? (sv.className.baseVal || '') : sv.className) || '').slice(0, 120),
                    ancestors: ancChain(sv)});
  }
  // icon-fonts
  for (const el2 of document.querySelectorAll('i,span')) {
    if (out.icons.length >= CAP_ICONS) { out.truncated.icons = true; break; }
    const cls = (typeof el2.className === 'string') ? el2.className : '';
    if (!iconFontRe.test(cls)) continue;
    const r = vis(el2);
    if (!r) continue;
    const cs = getComputedStyle(el2);
    out.icons.push({kind: 'iconfont', selector: sel(el2), w: Math.round(r.width), h: Math.round(r.height),
                    fontFamily: (cs.fontFamily || '').slice(0, 60), classes: cls.slice(0, 120),
                    ancestors: ancChain(el2)});
  }
  // kleine icon-achtige img's
  for (const im of imgs) {
    if (out.icons.length >= CAP_ICONS) { out.truncated.icons = true; break; }
    const r = im.getBoundingClientRect();
    if (r.width < 8 || r.width > 64 || r.height > 64) continue;
    const hint = ((im.src || '') + ' ' + (typeof im.className === 'string' ? im.className : '') + ' ' + (im.alt || '')).toLowerCase();
    if (!/icon|ico[-_.]|glyph|symbool/.test(hint)) continue;
    out.icons.push({kind: 'img', selector: sel(im), w: Math.round(r.width), h: Math.round(r.height),
                    src: (im.currentSrc || im.src || '').slice(0, 300), ancestors: ancChain(im)});
  }
  // emoji / tekst-tekens als icoon
  const emojiRe = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2190}-\u{21FF}\u{FE0F}\u{2713}\u{2714}\u{2716}\u{271A}\u{2764}]/u;
  for (const t of out.texts) {
    if (out.icons.length >= CAP_ICONS) break;
    try {
      const m = t.text.match(emojiRe);
      if (m) out.icons.push({kind: 'emoji', char: m[0], selector: t.selector,
                             text: t.text.slice(0, 40), shortText: t.text.length <= 6});
    } catch (e) {}
  }

  out.pageBg = (effBg(document.body).bg) || null;
  return out;
}
"""
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")
GOTO_TIMEOUT_MS = 45000
SETTLE_MS = 800          # JS/fonts even laten landen na load
POST_SCROLL_MS = 400     # reflow-rust na terugscrollen naar top


def page_slug(url: str) -> str:
    """Zelfde slug als content/*.txt (consistent koppelen), + hash bij query."""
    pu = urlparse(url)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", pu.path).strip("_") or "home"
    slug = slug[:100]
    if pu.query:
        slug += "_q" + hashlib.md5(pu.query.encode("utf-8", "ignore")).hexdigest()[:6]
    return slug


def _auto_scroll(page):
    """Scroll in stappen naar beneden (triggert lazy-load/IntersectionObserver)
    en weer naar top. Terug-scroll is EXPLICIET instant: bij sites met
    `scroll-behavior: smooth` animeert een kale scrollTo(0,0) en valt het
    fold-shot midden in de terug-scroll (les movevolt 03-07). Daarna wordt
    scrollY==0 geverifieerd. Fail-soft: scroll-problemen blokkeren de shot niet."""
    try:
        page.evaluate("""async ({step, maxSteps, pause}) => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            let last = -1;
            for (let i = 0; i < maxSteps; i++) {
                window.scrollBy(0, step);
                await sleep(pause);
                const y = window.scrollY;
                if (y === last) break;
                last = y;
            }
            window.scrollTo({top: 0, left: 0, behavior: 'instant'});
        }""", {"step": 800, "maxSteps": 40, "pause": 120})
        page.wait_for_timeout(POST_SCROLL_MS)
        for _ in range(5):   # verifieer top-positie (smooth-scroll/scroll-restore)
            if page.evaluate("window.scrollY") == 0:
                break
            page.evaluate("window.scrollTo({top: 0, left: 0, behavior: 'instant'})")
            page.wait_for_timeout(200)
    except Exception:
        pass


class ScreenshotCapture:
    """Eén Playwright-browser + 2 herbruikbare contexts voor alle pagina's."""

    def __init__(self, log=None):
        self.log = log
        self._pw = self._browser = None
        self._pages = {}          # "desktop"/"mobile" -> levende Page

    # ---- lifecycle ---------------------------------------------------------
    def _ensure_browser(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)

    def _new_page(self, kind):
        self._ensure_browser()
        if kind == "desktop":
            ctx = self._browser.new_context(
                viewport=DESKTOP, user_agent=DESKTOP_UA, locale="nl-NL",
                device_scale_factor=1)
        else:
            ctx = self._browser.new_context(
                viewport=MOBILE, user_agent=MOBILE_UA, locale="nl-NL",
                device_scale_factor=2, is_mobile=True, has_touch=True)
        return ctx.new_page()

    def _page(self, kind):
        pg = self._pages.get(kind)
        if pg is None or pg.is_closed():
            self._pages[kind] = self._new_page(kind)
        return self._pages[kind]

    def _recycle(self, kind):
        """Na een harde fout: page + context weggooien; volgende pagina krijgt vers."""
        pg = self._pages.pop(kind, None)
        if pg is not None:
            try:
                pg.context.close()
            except Exception:
                pass

    def close(self):
        for kind in list(self._pages):
            self._recycle(kind)
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = None

    # ---- capture -----------------------------------------------------------
    def _shoot(self, kind, url, dest: Path, want_dom: bool):
        """1 viewport-run voor 1 pagina: goto -> settle -> lazy-scroll ->
        fold-PNG + full-PNG (+ DOM + render-metadata).
        Returnt (files-dict, dom_html|None, meta|None)."""
        pg = self._page(kind)
        pg.goto(url, timeout=GOTO_TIMEOUT_MS, wait_until="load")
        pg.wait_for_timeout(SETTLE_MS)
        _auto_scroll(pg)
        files = {}
        fold = dest / f"{kind}-fold.png"
        full = dest / f"{kind}-full.png"
        pg.screenshot(path=str(fold))                    # viewport = above the fold
        pg.screenshot(path=str(full), full_page=True)    # hele pagina
        files[f"{kind}_fold"] = fold.name
        files[f"{kind}_full"] = full.name
        dom = pg.content() if want_dom else None
        # render-metadata (computed styles) voor audits 1.3/1.4/1.7 — fail-soft
        meta = None
        try:
            meta = pg.evaluate(EXTRACT_JS)
        except Exception as e:
            if self.log:
                self.log.warning("render-meta-fout (%s) %s: %s", kind, url, e)
        return files, dom, meta

    def capture_page(self, url: str, shots_root: Path) -> dict:
        """Alle shots voor 1 URL. Fail-soft: bij fouten een record met note."""
        slug = page_slug(url)
        dest = shots_root / slug
        dest.mkdir(parents=True, exist_ok=True)
        rec = {"slug": slug, "dir": f"screenshots/{slug}", "note": None}
        notes = []
        render_meta = {}
        for kind, want_dom in (("desktop", True), ("mobile", False)):
            try:
                files, dom, meta = self._shoot(kind, url, dest, want_dom)
                for k, fname in files.items():
                    rec[k] = f"screenshots/{slug}/{fname}"
                if dom:
                    (dest / "dom.html").write_text(dom, encoding="utf-8")
                    rec["dom"] = f"screenshots/{slug}/dom.html"
                if meta is not None:
                    render_meta[kind] = meta
            except Exception as e:
                notes.append(f"{kind}: {str(e)[:140]}")
                if self.log:
                    self.log.warning("screenshot-fout (%s) %s: %s", kind, url, e)
                self._recycle(kind)   # verse context voor de volgende pagina
        if render_meta:
            try:
                (dest / "render_meta.json").write_text(json.dumps(
                    {"url": url, "viewports": render_meta},
                    ensure_ascii=False), encoding="utf-8")
                rec["render_meta"] = f"screenshots/{slug}/render_meta.json"
            except Exception as e:
                notes.append(f"render_meta: {str(e)[:100]}")
        if notes:
            rec["note"] = " | ".join(notes)
        rec["ok"] = bool(rec.get("desktop_fold") and rec.get("mobile_fold"))
        try:
            (dest / "meta.json").write_text(json.dumps({
                "url": url, "captured": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "viewports": {"desktop": DESKTOP, "mobile": MOBILE},
                **{k: v for k, v in rec.items() if k != "dir"},
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return rec


def capture_site(pages: list, site_out: Path, log=None) -> dict:
    """Entry vanuit de scraper: shots voor álle gecrawlde pagina's van 1 site.
    Zet per page-record `page["screenshots"]` (in-place) en returnt een manifest.
    Crasht nooit: elke pagina is zelfstandig fail-soft."""
    shots_root = site_out / "screenshots"
    shots_root.mkdir(parents=True, exist_ok=True)
    cap = ScreenshotCapture(log=log)
    manifest = {"enabled": True, "viewport_desktop": DESKTOP, "viewport_mobile": MOBILE,
                "pages": 0, "ok": 0, "failed": []}
    try:
        for p in pages:
            url = p.get("url")
            if not url:
                continue
            manifest["pages"] += 1
            rec = cap.capture_page(url, shots_root)
            p["screenshots"] = rec
            if rec.get("ok"):
                manifest["ok"] += 1
            else:
                manifest["failed"].append({"url": url, "note": rec.get("note")})
            print(f"  [shot] {'ok ' if rec.get('ok') else 'FOUT'} {url[:80]}")
    finally:
        cap.close()
    try:
        (shots_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return manifest
