#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
AUDIT-HARNAS (fase 1) — draai audits/-modules op een BESTAANDE run-output,
zonder opnieuw te crawlen. Dé snelle iteratie-route voor feature-agents:

    1. eenmalig (of pak een bestaande run uit shared\seo-runs\...):
       python seo_scraper_v2.py https://zekermobiel.nl --max-pages 5 --screenshots --out mijn-testrun
    2. itereer op je audit:
       python _audit_harness.py mijn-testrun\zekermobiel.nl
       python _audit_harness.py mijn-testrun\zekermobiel.nl --audit cro
       python _audit_harness.py mijn-testrun\zekermobiel.nl --audit cro --json out.json

De ctx die je audit krijgt is dezelfde als in een echte run (INTEGRATION.md),
met één verschil: page_texts wordt gereconstrueerd uit content/*.txt.
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _slug(url):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", urlparse(url).path).strip("_")[:100] or "home"


def build_ctx(site_dir: Path):
    """Reconstrueer de audit-ctx uit een site-outputmap van een eerdere run."""
    if not (site_dir / "pages.json").exists():
        sys.exit(f"FOUT: {site_dir}\\pages.json bestaat niet — wijs een site-outputmap aan "
                 f"(bv. <run>\\zekermobiel.nl), niet de run-root.")
    pages = json.loads((site_dir / "pages.json").read_text(encoding="utf-8"))
    analysis = {}
    if (site_dir / "analysis.json").exists():
        try:
            analysis = json.loads((site_dir / "analysis.json").read_text(encoding="utf-8"))
        except Exception as e:
            print(f"waarschuwing: analysis.json onleesbaar ({e})")
    products = []
    if (site_dir / "products.json").exists():
        try:
            products = json.loads((site_dir / "products.json").read_text(encoding="utf-8"))
        except Exception:
            pass
    sitemap_urls = []
    if (site_dir / "sitemap-urls.txt").exists():
        sitemap_urls = [l.strip() for l in (site_dir / "sitemap-urls.txt")
                        .read_text(encoding="utf-8").splitlines() if l.strip()]
    page_texts = {}
    for p in pages:
        f = site_dir / "content" / f"{_slug(p.get('url', ''))}.txt"
        if f.exists():
            try:
                page_texts[p.get("url")] = f.read_text(encoding="utf-8")
            except Exception:
                pass
    shots_manifest = {"enabled": False}
    mf = site_dir / "screenshots" / "manifest.json"
    if mf.exists():
        try:
            shots_manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            pass
    log = logging.getLogger("audit-harness")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    domain = pages[0].get("url", "") if pages else ""
    domain = urlparse(domain).netloc or site_dir.name
    try:
        from seo_scraper_v2 import safe_name as _safe_name  # zelfde functie als echte runs
    except Exception:
        _safe_name = None
    return {
        "domain": domain, "pages": pages, "page_texts": page_texts,
        "sitemap_urls": sitemap_urls, "analysis": analysis, "products": products,
        "out": site_dir, "screenshots": shots_manifest, "safe_name": _safe_name,
        "log": log, "fast": False, "psi_enabled": False,
    }


def main():
    ap = argparse.ArgumentParser(description="Draai audits/-modules op een bestaande run-output")
    ap.add_argument("site_dir", help=r"site-outputmap van een eerdere run (bv. run\zekermobiel.nl)")
    ap.add_argument("--audit", help="alleen deze audit-key draaien (default: alle)")
    ap.add_argument("--json", help="resultaat ook als JSON naar dit bestand")
    args = ap.parse_args()

    ctx = build_ctx(Path(args.site_dir))
    import audits as audits_pkg
    mods = audits_pkg.discover()
    if args.audit:
        mods = [(k, m) for k, m in mods if k == args.audit]
        if not mods:
            sys.exit(f"FOUT: geen audit met key '{args.audit}' gevonden. "
                     f"Beschikbaar: {[k for k, _ in audits_pkg.discover()]}")

    results = {}
    for key, mod in mods:
        label = getattr(mod, "LABEL", key)
        try:
            res = mod.audit(ctx)
            results[key] = res
            issues = res.get("issues", []) or []
            print(f"\n=== {key} — {label} ===")
            print(f"score: {res.get('score')}   issues: {len(issues)}")
            if res.get("summary"):
                print(f"samenvatting: {res['summary']}")
            for it in issues[:8]:
                print(f"  [{it.get('severity','?'):8}] {it.get('title','')}  ->  {it.get('fix','')[:90]}")
        except Exception as e:
            results[key] = {"error": str(e), "label": label}
            print(f"\n=== {key} — {label} ===\nFAALDE: {e}")
            import traceback
            traceback.print_exc()

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\nJSON: {args.json}")
    print(f"\n{len(results)} audit(s) gedraaid; fouten: "
          f"{sum(1 for r in results.values() if r.get('error'))}")


if __name__ == "__main__":
    main()
