#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTML-DASHBOARD (report.html) — een mooi, zelfstandig rapport per run.

Bevat: per-site SEO-score (gauge + grade), de concurrent-verslaan-vergelijkingstabel
(wie-wint-per-signaal + eindwinnaar), top-issues (Critical/High/Medium met waarom +
fix), keyword-overzicht, Core Web Vitals, near-duplicate-clusters, orphan-pagina's en
de interne-link-PageRank. Eén bestand, inline CSS+JS, UTF-8, geen externe afhankelijkheden.
"""
from __future__ import annotations
import html
import json
from datetime import datetime

# AI Centrum-palet
BG = "#0d0f12"; PANEL = "#15181d"; PANEL2 = "#1b1f26"; LINE = "#262b33"
TXT = "#e8eaed"; MUT = "#9aa3af"; ACCENT = "#ff9f43"

SEV_COLOR = {"Critical": "#e74c3c", "High": "#ff7043", "Medium": "#ffb443", "Low": "#7c8694"}


def _esc(s):
    return html.escape(str(s if s is not None else ""))


def _score_color(v):
    if v is None: return MUT
    try: v = float(v)
    except Exception: return MUT
    if v >= 90: return "#2ecc71"
    if v >= 80: return "#7bd66b"
    if v >= 70: return "#f1c40f"
    if v >= 60: return ACCENT
    if v >= 50: return "#ff7043"
    return "#e74c3c"


def _gauge(score, size=128, label="", grade=""):
    if score is None:
        pct = 0; txt = "n.v.t."
    else:
        pct = max(0.0, min(100.0, float(score))); txt = f"{score:g}"
    r = size / 2 - 11
    import math
    circ = 2 * math.pi * r
    off = circ * (1 - pct / 100.0)
    col = _score_color(score)
    cx = cy = size / 2
    return f"""<div class="gauge">
<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{LINE}" stroke-width="11"/>
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{col}" stroke-width="11"
    stroke-linecap="round" stroke-dasharray="{circ:.1f}" stroke-dashoffset="{off:.1f}"
    transform="rotate(-90 {cx} {cy})"/>
  <text x="50%" y="48%" text-anchor="middle" dominant-baseline="middle"
    font-size="{size*0.30:.0f}" font-weight="800" fill="{col}">{_esc(txt)}</text>
  <text x="50%" y="68%" text-anchor="middle" font-size="{size*0.12:.0f}" fill="{MUT}">/ 100</text>
</svg>
{f'<div class="gauge-grade" style="background:{col}">{_esc(grade)}</div>' if grade else ''}
{f'<div class="gauge-label">{_esc(label)}</div>' if label else ''}
</div>"""


def _bar(pct, col=ACCENT):
    pct = max(0, min(100, pct))
    return f'<div class="bar"><span style="width:{pct:.0f}%;background:{col}"></span></div>'


def _sev_badge(sev):
    return f'<span class="sev" style="background:{SEV_COLOR.get(sev, MUT)}">{_esc(sev)}</span>'


def _chip(text, count=None):
    c = f' <b>{count}</b>' if count is not None else ''
    return f'<span class="chip">{_esc(text)}{c}</span>'


# ---------------------------------------------------------------------------
def _site_scorecards(sites):
    cards = []
    for s in sites:
        seo = s["summary"].get("seo") or {}
        score = seo.get("site_score")
        grade = seo.get("site_grade", "?")
        ic = seo.get("issue_counts", {}) or {}
        cov = (s["summary"].get("coverage") or {})
        chips = "".join(
            f'<span class="sev" style="background:{SEV_COLOR[k]}">{k}: {ic.get(k,0)}</span>'
            for k in ("Critical", "High", "Medium", "Low"))
        cards.append(f"""<div class="card scorecard">
  {_gauge(score, 132, grade=grade)}
  <div class="sc-meta">
    <div class="sc-domain">{_esc(s['domain'])}</div>
    <div class="sc-sub">{s['summary'].get('pages',0)} pagina's · fill-rate {cov.get('fill_rate_overall_pct','?')}%</div>
    <div class="sevrow">{chips}</div>
  </div>
</div>""")
    return f'<div class="grid scorecards">{"".join(cards)}</div>'


def _compare_section(compare):
    if not compare or not compare.get("comparable"):
        reason = (compare or {}).get("reason") or (compare or {}).get("error") or "n.v.t."
        return f'<section><h2>Concurrent verslaan</h2><p class="muted">Vergelijking niet beschikbaar ({_esc(reason)}). Draai met meerdere sites + <code>--compare</code>.</p></section>'
    sites = compare["sites"]
    champ = compare.get("champion", "gelijk")
    # ranking banner
    rank = compare.get("ranking", [])
    banner_items = " ".join(
        f'<span class="rankpill">{i+1}. {_esc(r["domain"])} <b>{r["signals_won"]}</b> signalen</span>'
        for i, r in enumerate(rank))
    head = "".join(f"<th>{_esc(d)}</th>" for d in sites)
    rows = []
    for row in compare["rows"]:
        cells = []
        for d in sites:
            val = row["values"].get(d, "-")
            win = (row["winner"] == d)
            cells.append(f'<td class="{"win" if win else ""}">{_esc(val)}{" ★" if win else ""}</td>')
        arrow = "▲ hoger = beter" if row["higher_is_better"] else "▼ lager = beter"
        rows.append(f'<tr><td class="signal">{_esc(row["signal"])}<span class="hint">{arrow}</span></td>{"".join(cells)}</tr>')
    champ_html = (f'<div class="champion">🏆 Eindwinnaar: <b>{_esc(champ)}</b></div>'
                  if champ != "gelijk" else '<div class="champion">Gelijkspel</div>')
    return f"""<section>
  <h2>Concurrent verslaan — wie wint per signaal?</h2>
  {champ_html}
  <div class="rankrow">{banner_items}</div>
  <div class="tablewrap"><table class="cmp">
    <thead><tr><th>Signaal</th>{head}</tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
</section>"""


def _category_bars(seo):
    cs = seo.get("category_scores", {}) or {}
    # vaste, leesbare volgorde
    order = ["title", "meta_description", "headings", "indexability", "content",
             "structured_data", "images", "links", "technical", "cwv", "ecommerce"]
    rows = []
    for k in order:
        c = cs.get(k)
        if not c:
            continue
        pct = c.get("avg_pct", 0)
        rows.append(f"""<div class="catrow">
  <span class="catname">{_esc(c.get('label',k))}</span>
  {_bar(pct, _score_color(pct))}
  <span class="catpct" style="color:{_score_color(pct)}">{pct:g}%</span>
</div>""")
    return f'<div class="catbars">{"".join(rows)}</div>'


def _issues_table(seo, limit=14):
    items = seo.get("top_issues", []) or []
    if not items:
        return '<p class="muted">Geen issues gevonden — sterk.</p>'
    rows = []
    for it in items[:limit]:
        rows.append(f"""<tr>
  <td>{_sev_badge(it['severity'])}</td>
  <td><div class="iss-title">{_esc(it['title'])}</div>
      <div class="iss-why">{_esc(it['why'])}</div>
      <div class="iss-fix">→ {_esc(it['fix'])}</div></td>
  <td class="num">{it.get('pages',1)}</td>
</tr>""")
    return f"""<div class="tablewrap"><table class="issues">
  <thead><tr><th>Prioriteit</th><th>Issue · waarom · fix</th><th>Pagina's</th></tr></thead>
  <tbody>{"".join(rows)}</tbody></table></div>"""


def _keywords_block(summary):
    kw = summary.get("top_keywords", []) or []
    bg = summary.get("top_bigrams", []) or []
    an = summary.get("top_anchors", []) or []
    types = summary.get("jsonld_types", []) or []
    def chips(lst): return "".join(_chip(x) for x in lst[:18]) or '<span class="muted">—</span>'
    return f"""<div class="kwgrid">
  <div><h4>Top keywords</h4><div class="chips">{chips(kw)}</div></div>
  <div><h4>Top woordcombinaties</h4><div class="chips">{chips(bg)}</div></div>
  <div><h4>Interne anchorteksten</h4><div class="chips">{chips(an)}</div></div>
  <div><h4>Structured-data types</h4><div class="chips">{chips(types)}</div></div>
</div>"""


def _cwv_block(pages):
    rows = []
    for p in pages:
        if any(p.get(k) is not None for k in ("lcp_field", "lcp", "cls_field", "cls")):
            lcp = p.get("lcp_field") or p.get("lcp")
            inp = p.get("inp_field") or p.get("tbt")
            cls = p.get("cls_field") if p.get("cls_field") is not None else p.get("cls")
            rows.append(f"<tr><td>{_esc(p.get('url',''))}</td><td>{_esc(lcp)}</td><td>{_esc(inp)}</td><td>{_esc(cls)}</td></tr>")
    if not rows:
        return '<p class="muted">Geen Core Web Vitals-data (draai met <code>--psi</code> + een PageSpeed API-key voor echte LCP/INP/CLS).</p>'
    return f"""<div class="tablewrap"><table>
  <thead><tr><th>URL</th><th>LCP</th><th>INP/TBT</th><th>CLS</th></tr></thead>
  <tbody>{"".join(rows)}</tbody></table></div>"""


def _analysis_block(analysis):
    nd = analysis.get("near_duplicates", {}) or {}
    orph = analysis.get("orphans", {}) or {}
    lg = analysis.get("link_graph", {}) or {}

    # near-duplicates
    clusters = nd.get("clusters", []) or []
    if clusters:
        cl_html = "".join(
            f'<div class="cluster"><div class="cl-head">{c["size"]} pagina\'s · '
            f'gelijkenis {round(c.get("max_similarity",0)*100)}%{" · EXACT" if c.get("exact") else ""}</div>'
            f'<ul>{"".join(f"<li>{_esc(u)}</li>" for u in c["urls"][:8])}</ul></div>'
            for c in clusters[:8])
    else:
        cl_html = '<p class="muted">Geen near-duplicate-clusters gevonden boven de drempel — uniek genoeg.</p>'

    # orphans
    osm = orph.get("orphan_sitemap", []) or []
    ocr = orph.get("orphan_crawled", []) or []
    orph_html = (
        f'<p><b>{orph.get("orphan_sitemap_count",0)}</b> sitemap-URLs zonder interne link · '
        f'<b>{orph.get("orphan_crawled_count",0)}</b> gecrawlde pagina\'s zonder inbound-link.</p>'
        + (f'<details><summary>Voorbeelden (sitemap-orphans)</summary><ul>'
           + "".join(f"<li>{_esc(u)}</li>" for u in osm[:15]) + '</ul></details>' if osm else '')
        + (f'<details><summary>Gecrawlde orphans</summary><ul>'
           + "".join(f"<li>{_esc(u)}</li>" for u in ocr[:15]) + '</ul></details>' if ocr else '')
        + f'<p class="hint">{_esc(orph.get("note",""))}</p>')

    # pagerank
    top = lg.get("top", []) or []
    if top:
        mx = max((t["pagerank_pct"] for t in top), default=1) or 1
        pr_rows = "".join(
            f'<tr><td>{_esc(t["url"])}</td><td class="num">{t["inbound_internal_links"]}</td>'
            f'<td>{_bar(100*t["pagerank_pct"]/mx, ACCENT)}</td>'
            f'<td class="num">{t["pagerank_pct"]:g}%</td></tr>'
            for t in top[:12])
        pr_html = (f'<p class="muted">{lg.get("nodes",0)} nodes · {lg.get("edges",0)} interne links · '
                   f'gem. {lg.get("avg_inbound",0)} inbound/pagina.</p>'
                   f'<div class="tablewrap"><table><thead><tr><th>Pagina</th><th>Inbound</th>'
                   f'<th>Link-equity</th><th>PR%</th></tr></thead><tbody>{pr_rows}</tbody></table></div>')
    else:
        pr_html = '<p class="muted">Te weinig interne links om een graaf te bouwen.</p>'

    return f"""<div class="analysis">
  <div><h4>Near-duplicate clusters <span class="hint">(kannibalisatie-risico)</span></h4>{cl_html}</div>
  <div><h4>Orphan-pagina's <span class="hint">(slecht vindbaar)</span></h4>{orph_html}</div>
  <div><h4>Interne link-equity (PageRank)</h4>{pr_html}</div>
</div>"""


def _pages_table(pages):
    rows = []
    for p in sorted(pages, key=lambda x: (x.get("seo_health_score") if x.get("seo_health_score") is not None else 999)):
        sc = p.get("seo_health_score")
        col = _score_color(sc)
        ic = p.get("seo_issue_counts", {}) or {}
        crit = ic.get("Critical", 0); high = ic.get("High", 0)
        badge = ""
        if crit: badge += f'<span class="mini" style="background:{SEV_COLOR["Critical"]}">{crit}C</span>'
        if high: badge += f'<span class="mini" style="background:{SEV_COLOR["High"]}">{high}H</span>'
        rows.append(f"""<tr>
  <td><span class="pscore" style="background:{col}">{_esc(sc if sc is not None else '?')}</span> <span class="pgrade">{_esc(p.get('seo_grade',''))}</span></td>
  <td class="urlcell">{_esc(p.get('url',''))}<div class="ptitle">{_esc((p.get('title') or '')[:90])}</div></td>
  <td class="num">{int(p.get('word_count',0) or 0)}</td>
  <td>{badge or '<span class="muted">—</span>'}</td>
</tr>""")
    return f"""<div class="tablewrap"><table class="pages">
  <thead><tr><th>Score</th><th>URL</th><th>Woorden</th><th>Issues</th></tr></thead>
  <tbody>{"".join(rows)}</tbody></table></div>"""


def _site_dir(domain):
    """Mapnaam van de site onder out_root (zelfde transformatie als de scraper)."""
    return str(domain).replace(":", "_")


def _screenshots_block(s, limit=12):
    """Module 1.1 — galerij: per pagina desktop- en mobiel-fold-thumbnail,
    klik = full-page PNG; plus link naar de gerenderde DOM-snapshot."""
    base = _site_dir(s["domain"])
    recs = [(p.get("url", ""), p.get("screenshots"))
            for p in s["pages"] if isinstance(p.get("screenshots"), dict)]
    if not recs:
        return ('<p class="muted">Geen screenshots in deze run — draai met '
                '<code>--screenshots</code> (fase-2-nulmeting: verplicht aan) voor de '
                'visuele schouw + render-gebaseerde audits.</p>')
    tiles = []
    for url, rec in recs[:limit]:
        thumbs = []
        for kind, label in (("desktop", "1440"), ("mobile", "390")):
            fold = rec.get(f"{kind}_fold"); full = rec.get(f"{kind}_full")
            if fold:
                img = f'<img src="{_esc(base)}/{_esc(fold)}" alt="{_esc(kind)} screenshot" loading="lazy"/>'
                thumbs.append(
                    f'<a class="shot {kind}" href="{_esc(base)}/{_esc(full or fold)}" '
                    f'target="_blank" title="{_esc(kind)} full-page">{img}<span>{label}px</span></a>')
            else:
                thumbs.append(f'<span class="shot missing">{label}px ontbreekt</span>')
        dom = rec.get("dom")
        domlink = (f' · <a href="{_esc(base)}/{_esc(dom)}" target="_blank">DOM</a>' if dom else "")
        note = rec.get("note")
        notehtml = f'<div class="shotnote">{_esc(note)}</div>' if note else ""
        tiles.append(f"""<div class="shotcard">
  <div class="shoturl">{_esc(url)}{domlink}</div>
  <div class="shotrow">{"".join(thumbs)}</div>{notehtml}
</div>""")
    more = (f'<p class="muted">+ {len(recs) - limit} pagina\'s meer in de screenshots-map.</p>'
            if len(recs) > limit else "")
    return f'<div class="shotgrid">{"".join(tiles)}</div>{more}'


def _audit_issue_rows(issues, limit=10):
    rows = []
    for it in (issues or [])[:limit]:
        rows.append(
            f'<tr><td>{_sev_badge(it.get("severity", "Low"))}</td>'
            f'<td><b>{_esc(it.get("title", ""))}</b>'
            f'<div class="why">{_esc(it.get("why", ""))}</div>'
            f'<div class="fix">Fix: {_esc(it.get("fix", ""))}</div></td>'
            f'<td class="urlcell">{_esc(it.get("url", ""))}</td></tr>')
    if not rows:
        return '<p class="muted">Geen issues gevonden door deze audit.</p>'
    return (f'<div class="tablewrap"><table class="issues audit-issues">'
            f'<thead><tr><th></th><th>Bevinding</th><th>URL</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _audit_cards(s):
    """Fase-1-audits (audits/-registry): elke module krijgt automatisch een
    eigen card met score-pill, samenvatting, issues en optionele eigen HTML."""
    audits = (s.get("analysis") or {}).get("audits") or {}
    if not audits:
        return ""
    cards = []
    for key, res in sorted(audits.items(), key=lambda kv: (kv[1].get("order", 500), kv[0])):
        label = res.get("label", key)
        if res.get("error"):
            cards.append(f'<div class="card"><h3>{_esc(label)}</h3>'
                         f'<p class="muted">Audit faalde (fail-soft): '
                         f'<code>{_esc(res["error"])}</code></p></div>')
            continue
        score = res.get("score")
        pill = (f'<span class="pscore" style="background:{_score_color(score)}">{score:g}</span>'
                if isinstance(score, (int, float)) else '<span class="muted">n.v.t.</span>')
        summary = f'<p>{_esc(res["summary"])}</p>' if res.get("summary") else ""
        custom = res.get("html") or ""
        cards.append(f"""<div class="card audit" id="audit-{_esc(key)}">
  <h3>{_esc(label)} {pill}</h3>
  {summary}{_audit_issue_rows(res.get("issues"))}{custom}
</div>""")
    return "".join(cards)


def _site_section(s, idx, active):
    summary = s["summary"]; seo = summary.get("seo") or {}
    return f"""<div class="sitepanel {'active' if active else ''}" id="site-{idx}">
  <div class="grid two">
    <div class="card"><h3>Score per categorie</h3>{_category_bars(seo)}</div>
    <div class="card"><h3>Keyword-overzicht</h3>{_keywords_block(summary)}</div>
  </div>
  <div class="card"><h3>Top-issues — concrete fixes (gesorteerd op prioriteit)</h3>{_issues_table(seo)}</div>
  <div class="card"><h3>Screenshots — desktop 1440 &amp; mobiel 390 (module 1.1)</h3>{_screenshots_block(s)}</div>
  {_audit_cards(s)}
  <div class="grid two">
    <div class="card"><h3>Core Web Vitals</h3>{_cwv_block(s['pages'])}</div>
    <div class="card"><h3>Beste / slechtste pagina's</h3>{_best_worst(seo)}</div>
  </div>
  <div class="card"><h3>Onderscheidende analyse</h3>{_analysis_block(s.get('analysis', {}))}</div>
  <div class="card"><h3>Alle pagina's ({len(s['pages'])})</h3>{_pages_table(s['pages'])}</div>
</div>"""


def _best_worst(seo):
    worst = seo.get("worst_pages", []) or []
    best = seo.get("best_pages", []) or []
    def lst(items):
        return "".join(
            f'<li><span class="pscore" style="background:{_score_color(i["score"])}">{i["score"]:g}</span> {_esc(i["url"])}</li>'
            for i in items) or '<li class="muted">—</li>'
    return f"""<div class="bw">
  <div><h4>Laagst scorend (eerst fixen)</h4><ul class="bwlist">{lst(worst[:6])}</ul></div>
  <div><h4>Best scorend</h4><ul class="bwlist">{lst(best[:4])}</ul></div>
</div>"""


CSS = """
*{box-sizing:border-box}
body{margin:0;background:%(BG)s;color:%(TXT)s;font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
a{color:%(ACCENT)s}
.wrap{max-width:1180px;margin:0 auto;padding:24px 18px 80px}
header.top{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:8px}
header.top h1{font-size:26px;margin:0;font-weight:800}
.brandmark{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,%(ACCENT)s,#ff7a18);display:flex;align-items:center;justify-content:center;font-weight:900;color:#1a1206}
.meta{color:%(MUT)s;font-size:13px;margin-left:auto;text-align:right}
.muted{color:%(MUT)s}
.hint{color:%(MUT)s;font-size:11px;font-weight:400;margin-left:6px}
code{background:%(PANEL2)s;padding:1px 6px;border-radius:5px;font-size:13px}
section{margin-top:26px}
h2{font-size:19px;border-left:4px solid %(ACCENT)s;padding-left:10px;margin:0 0 14px}
h3{font-size:15px;margin:0 0 12px}
h4{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:%(MUT)s;margin:0 0 8px}
.card{background:%(PANEL)s;border:1px solid %(LINE)s;border-radius:14px;padding:16px 18px;margin-bottom:16px}
.grid{display:grid;gap:16px}
.grid.two{grid-template-columns:1fr 1fr}
.grid.scorecards{grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
@media(max-width:760px){.grid.two{grid-template-columns:1fr}}
.scorecard{display:flex;gap:18px;align-items:center}
.gauge{position:relative;display:flex;flex-direction:column;align-items:center}
.gauge-grade{position:absolute;top:6px;right:6px;width:26px;height:26px;border-radius:50%%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;color:#11140f}
.gauge-label{font-size:12px;color:%(MUT)s;margin-top:4px}
.sc-domain{font-size:20px;font-weight:800}
.sc-sub{color:%(MUT)s;font-size:13px;margin:2px 0 10px}
.sevrow{display:flex;gap:6px;flex-wrap:wrap}
.sev{font-size:11px;font-weight:700;color:#11140f;padding:2px 8px;border-radius:20px}
.champion{font-size:18px;font-weight:800;margin-bottom:10px}
.rankrow{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.rankpill{background:%(PANEL2)s;border:1px solid %(LINE)s;border-radius:20px;padding:4px 12px;font-size:13px}
.tablewrap{overflow-x:auto}
table{width:100%%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid %(LINE)s;vertical-align:top}
th{color:%(MUT)s;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
td.num,th.num,td.win{text-align:center}
table.cmp td.win{background:rgba(255,159,67,.16);color:%(ACCENT)s;font-weight:800}
td.signal{font-weight:600}
.bar{display:inline-block;width:100%%;min-width:90px;height:8px;background:%(PANEL2)s;border-radius:6px;overflow:hidden;vertical-align:middle}
.bar span{display:block;height:100%%;border-radius:6px}
.catrow{display:grid;grid-template-columns:140px 1fr 48px;align-items:center;gap:10px;margin-bottom:7px;font-size:13px}
.catname{color:%(TXT)s}.catpct{text-align:right;font-weight:700;font-size:12px}
.sev.badge{}
.iss-title{font-weight:700;margin-bottom:2px}
.iss-why{color:%(MUT)s;font-size:12px;margin-bottom:3px}
.iss-fix{color:%(ACCENT)s;font-size:12.5px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{background:%(PANEL2)s;border:1px solid %(LINE)s;border-radius:18px;padding:3px 10px;font-size:12px}
.chip b{color:%(ACCENT)s}
.kwgrid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:680px){.kwgrid{grid-template-columns:1fr}}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0 14px;position:sticky;top:0;background:%(BG)s;padding:8px 0;z-index:5}
.tab{background:%(PANEL)s;border:1px solid %(LINE)s;border-radius:10px;padding:8px 14px;cursor:pointer;font-weight:600;font-size:13px;color:%(TXT)s}
.tab.active{background:%(ACCENT)s;color:#11140f;border-color:%(ACCENT)s}
.sitepanel{display:none}.sitepanel.active{display:block}
.shotgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px}
.shotcard{background:%(PANEL2)s;border:1px solid %(LINE)s;border-radius:10px;padding:10px}
.shoturl{font-size:12px;color:%(MUT)s;margin-bottom:8px;word-break:break-all}
.shotrow{display:flex;gap:10px;align-items:flex-start}
.shot{position:relative;display:block;border:1px solid %(LINE)s;border-radius:8px;overflow:hidden;background:#000}
.shot img{display:block}
.shot.desktop img{width:240px;height:150px;object-fit:cover;object-position:top}
.shot.mobile img{width:70px;height:150px;object-fit:cover;object-position:top}
.shot span{position:absolute;right:4px;bottom:4px;background:rgba(0,0,0,.65);color:#fff;font-size:10px;padding:1px 5px;border-radius:4px}
.shot.missing{display:flex;align-items:center;justify-content:center;width:120px;height:150px;color:%(MUT)s;font-size:11px;border:1px dashed %(LINE)s;border-radius:8px}
.shotnote{margin-top:6px;font-size:11px;color:#ff7043}
.audit h3 .pscore{margin-left:8px}
.audit-issues td .why{color:%(MUT)s;font-size:12px;margin-top:2px}
.audit-issues td .fix{color:#7bd66b;font-size:12px;margin-top:2px}
.analysis{display:grid;gap:18px}
.cluster{background:%(PANEL2)s;border:1px solid %(LINE)s;border-radius:10px;padding:8px 12px;margin-bottom:8px}
.cl-head{font-weight:700;font-size:13px;color:%(ACCENT)s}
.cluster ul,.analysis ul{margin:6px 0 0;padding-left:18px;font-size:12.5px;color:%(MUT)s}
details summary{cursor:pointer;color:%(ACCENT)s;font-size:13px;margin:6px 0}
.bw{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:680px){.bw{grid-template-columns:1fr}}
.bwlist{list-style:none;margin:0;padding:0;font-size:12.5px}
.bwlist li{padding:4px 0;display:flex;gap:8px;align-items:center;word-break:break-all}
.pscore{display:inline-block;min-width:30px;text-align:center;font-weight:800;color:#11140f;border-radius:6px;padding:1px 6px;font-size:12px}
.pgrade{color:%(MUT)s;font-weight:700}
.urlcell{word-break:break-all;max-width:520px}
.ptitle{color:%(MUT)s;font-size:11.5px}
.mini{display:inline-block;color:#11140f;font-weight:800;border-radius:5px;padding:1px 6px;font-size:11px;margin-right:4px}
footer{margin-top:40px;color:%(MUT)s;font-size:12px;text-align:center}
""" % {"BG": BG, "PANEL": PANEL, "PANEL2": PANEL2, "LINE": LINE, "TXT": TXT, "MUT": MUT, "ACCENT": ACCENT}


def build_report(data):
    sites = data["sites"]
    gen = data.get("generated") or datetime.now().strftime("%Y-%m-%d %H:%M")
    total_pages = sum(len(s["pages"]) for s in sites)
    psi = "aan" if data.get("psi") else "uit"
    tabs = "".join(
        f'<div class="tab {"active" if i == 0 else ""}" onclick="showSite({i})">{_esc(s["domain"])}</div>'
        for i, s in enumerate(sites))
    panels = "".join(_site_section(s, i, i == 0) for i, s in enumerate(sites))
    return f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEO-rapport — {_esc(gen)}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<header class="top">
  <div class="brandmark">SEO</div>
  <h1>SEO-scorerapport</h1>
  <div class="meta">{_esc(gen)}<br>{len(sites)} site(s) · {total_pages} pagina's · PSI {psi}</div>
</header>
<p class="muted">Gewogen 0-100-score over title, meta, headings, indexeerbaarheid, content,
structured data, afbeeldingen, links, techniek{', Core Web Vitals' if data.get('psi') else ''} en
e-commerce — met per pagina concrete Critical/High/Medium-fixes. Dit is de meetlat om sites naar 100% te brengen.</p>

<section>{_site_scorecards(sites)}</section>

{_compare_section(data.get('compare'))}

<section>
  <h2>Per site — diepteanalyse</h2>
  <div class="tabs">{tabs}</div>
  {panels}
</section>

<footer>Gegenereerd door seo_scraper_v2 (pro) · {_esc(gen)} · score.json bevat de volledige data.</footer>
</div>
<script>
function showSite(i){{
  document.querySelectorAll('.sitepanel').forEach((p,idx)=>p.classList.toggle('active',idx===i));
  document.querySelectorAll('.tab').forEach((t,idx)=>t.classList.toggle('active',idx===i));
  window.scrollTo({{top:document.querySelector('.tabs').offsetTop-10,behavior:'smooth'}});
}}
</script>
</body></html>"""


def write_report(data, out_path):
    import io
    html_str = build_report(data)
    io.open(out_path, "w", encoding="utf-8", newline="\n").write(html_str)
    return out_path
