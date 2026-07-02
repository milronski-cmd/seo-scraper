#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Concurrent-verslaan-vergelijking uit BESTAANDE run-output (geen herscrape).

Laadt summaries.json uit meerdere run-mappen (elk 1 site), draait
advanced_analysis.compare_sites en schrijft compare.json + COMPARE-<naam>.md
(winnaar per signaal + eindwinnaar + audit-scores-tabel).

Gebruik:
  python _compare_from_runs.py --name scootmobielen --out <map> \
      <run>\zekermobiel\zekermobiel.nl <run>\fastfurious\fastfuriousscooters.nl ...
(paden = site-outputmappen; de eerste is "wij" voor de kop van het rapport)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import advanced_analysis  # noqa: E402


def load_summary(site_dir: Path):
    """summaries.json staat in de RUN-root; per-site zit de summary daarin.
    Betrouwbaarder: reconstrueer uit de run-root op domeinnaam; fallback:
    zoek summaries.json één map hoger."""
    run_root = site_dir.parent
    f = run_root / "summaries.json"
    if not f.exists():
        sys.exit(f"FOUT: {f} bestaat niet (wijs de site-map binnen een run aan).")
    summaries = json.loads(f.read_text(encoding="utf-8"))
    dom = site_dir.name
    for s in summaries:
        if s.get("domain") == dom:
            return s
    sys.exit(f"FOUT: domein '{dom}' niet gevonden in {f}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="niche-naam voor de rapportbestanden")
    ap.add_argument("--out", required=True, help="outputmap")
    ap.add_argument("sites", nargs="+", help="site-outputmappen (eerste = eigen site)")
    args = ap.parse_args()

    dirs = [Path(p) for p in args.sites]
    summaries = [load_summary(d) for d in dirs]
    cmp_res = advanced_analysis.compare_sites(summaries)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"compare-{args.name}.json").write_text(
        json.dumps(cmp_res, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# Concurrent-vergelijking — {args.name}",
             f"Eigen site: **{summaries[0].get('domain')}** vs " +
             ", ".join(s.get("domain", "?") for s in summaries[1:]), ""]
    if not cmp_res.get("comparable"):
        lines.append(f"Vergelijking niet mogelijk: {cmp_res.get('reason') or cmp_res.get('error')}")
    else:
        lines.append(f"## Eindwinnaar: **{cmp_res.get('champion')}**")
        lines.append("")
        lines.append("| # | Site | Signalen gewonnen |")
        lines.append("| --- | --- | --- |")
        for i, r in enumerate(cmp_res.get("ranking", []), 1):
            lines.append(f"| {i} | {r['domain']} | {r['signals_won']} |")
        lines.append("")
        lines.append("## Winnaar per signaal")
        lines.append("| Signaal | " + " | ".join(cmp_res["sites"]) + " | Winnaar |")
        lines.append("| --- | " + " | ".join("---" for _ in cmp_res["sites"]) + " | --- |")
        for row in cmp_res.get("rows", []):
            cells = " | ".join(str(row["values"].get(d, "-")) for d in cmp_res["sites"])
            lines.append(f"| {row['signal']} | {cells} | **{row.get('winner') or '-'}** |")
    # audit-scores-tabel (fase-1-modules) uit de summaries
    lines += ["", "## Audit-scores (fase-1-modules)",
              "| Site | SEO | CRO | Beeld | Icons | Links | Doelgroep | Contrast |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in summaries:
        seo = (s.get("seo") or {}).get("site_score", "-")
        au = s.get("audits_summary") or {}
        def sc(k):
            v = (au.get(k) or {}).get("score")
            return "-" if v is None else v
        lines.append(f"| {s.get('domain')} | {seo} | {sc('cro')} | {sc('images_visual')} "
                     f"| {sc('icons')} | {sc('links_optimizer')} | {sc('audience')} | {sc('contrast')} |")
    md = out / f"COMPARE-{args.name}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"geschreven: {md}")
    if cmp_res.get("comparable"):
        print(f"eindwinnaar {args.name}: {cmp_res.get('champion')}")


if __name__ == "__main__":
    main()
