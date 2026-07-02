#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Onderscheidende analyse — wat de gratis concurrenten niet (gratis) geven:

  1. NEAR-DUPLICATE-CLUSTERING — welke pagina's lijken inhoudelijk te veel op
     elkaar (shingled Jaccard + exacte content-hash). Kannibalisatie-risico.
  2. ORPHAN-PAGE-DETECTIE — pagina's in de sitemap waar GEEN interne link naar
     wijst (Google vindt ze nauwelijks) + gecrawlde pagina's zonder inbound-links.
  3. INTERNE-LINK-GRAAF + PAGERANK — welke pagina's krijgen de meeste interne
     link-equity (vereenvoudigde, iteratieve PageRank over de crawl-graaf).
  4. CONCURRENT-VERSLAAN (`--compare`) — meerdere sites naast elkaar gescoord met
     wie-wint-per-signaal + eindwinnaar.

Alleen stdlib. Alles fail-soft.
"""
from __future__ import annotations
import re
from collections import defaultdict

_WORD = re.compile(r"[a-zà-ÿ0-9]+", re.I)


def _norm_url(u):
    try:
        return (u or "").split("#")[0].split("?")[0].rstrip("/").lower()
    except Exception:
        return u or ""


def _shingles(text, k=5, cap=4000):
    """Set van k-woord-shingles uit genormaliseerde tekst."""
    toks = _WORD.findall((text or "").lower())
    if len(toks) < k:
        return set(toks)
    out = set()
    for i in range(len(toks) - k + 1):
        out.add(" ".join(toks[i:i + k]))
        if len(out) >= cap:
            break
    return out


def near_duplicate_clusters(pages, page_texts, threshold=0.80, cap=400):
    """Clusters van inhoudelijk (bijna-)identieke pagina's.
    `page_texts`: {url: volledige_tekst}. Valt terug op title+headings als de
    tekst ontbreekt. O(n^2) pairwise (begrensd op `cap` pagina's)."""
    try:
        items = []
        for p in pages[:cap]:
            url = p.get("url", "")
            txt = page_texts.get(url) if page_texts else ""
            if not txt:
                # fallback-proxy uit het record
                hs = p.get("headings", {}) or {}
                txt = " ".join([str(p.get("title", ""))] +
                               [" ".join(hs.get(f"h{i}", []) or []) for i in range(1, 4)])
            items.append({
                "url": url,
                "md5": p.get("content_md5", ""),
                "wc": int(p.get("word_count", 0) or 0),
                "sh": _shingles(txt),
            })

        n = len(items)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        pair_sims = []
        for i in range(n):
            si = items[i]["sh"]
            for j in range(i + 1, n):
                sj = items[j]["sh"]
                # exacte hash-match telt altijd als duplicaat
                if items[i]["md5"] and items[i]["md5"] == items[j]["md5"]:
                    union(i, j)
                    pair_sims.append((items[i]["url"], items[j]["url"], 1.0, True))
                    continue
                if not si or not sj:
                    continue
                inter = len(si & sj)
                if inter == 0:
                    continue
                jac = inter / float(len(si | sj))
                if jac >= threshold:
                    union(i, j)
                    pair_sims.append((items[i]["url"], items[j]["url"], round(jac, 3), False))

        groups = defaultdict(list)
        for i in range(n):
            groups[find(i)].append(i)
        clusters = []
        for members in groups.values():
            if len(members) < 2:
                continue
            urls = [items[m]["url"] for m in members]
            clusters.append({
                "size": len(members),
                "urls": urls,
                "max_similarity": max((s for a, b, s, _ in pair_sims
                                       if a in urls and b in urls), default=1.0),
                "exact": any(e for a, b, s, e in pair_sims if a in urls and b in urls),
            })
        clusters.sort(key=lambda c: (-c["size"], -c["max_similarity"]))
        return {
            "threshold": threshold,
            "clusters": clusters,
            "pages_in_clusters": sum(c["size"] for c in clusters),
            "top_pairs": sorted(pair_sims, key=lambda x: -x[2])[:25],
        }
    except Exception as e:
        return {"error": str(e)[:200], "clusters": []}


def orphan_analysis(pages, sitemap_urls):
    """Orphans = pagina's zonder interne inbound-links.
    - orphan_sitemap: staat in de sitemap, maar geen enkele gecrawlde pagina linkt ernaar.
    - orphan_crawled: wel gecrawld, maar 0 inbound interne links (excl. homepage)."""
    try:
        crawled = [p.get("url", "") for p in pages]
        crawled_norm = {_norm_url(u): u for u in crawled if u}
        # alle interne link-doelen (genormaliseerd) -> inbound-tellingen
        inbound = defaultdict(int)
        for p in pages:
            src = _norm_url(p.get("url", ""))
            for link in (p.get("internal_links", []) or []):
                tgt = _norm_url(link)
                if tgt and tgt != src:
                    inbound[tgt] += 1
        linked = set(inbound.keys())

        home = _norm_url(crawled[0]) if crawled else ""
        orphan_crawled = [u for nu, u in crawled_norm.items()
                          if nu != home and inbound.get(nu, 0) == 0]

        sm_norm = {_norm_url(u): u for u in (sitemap_urls or []) if u}
        orphan_sitemap = [u for nu, u in sm_norm.items() if nu not in linked][:200]

        return {
            "crawled_pages": len(crawled_norm),
            "sitemap_urls": len(sm_norm),
            "orphan_crawled_count": len(orphan_crawled),
            "orphan_crawled": orphan_crawled[:100],
            "orphan_sitemap_count": len(orphan_sitemap),
            "orphan_sitemap": orphan_sitemap,
            "note": ("orphan_sitemap is begrensd door --max-pages: bij een kleine crawl "
                     "linken we simpelweg nog niet naar veel sitemap-URLs. Vergroot --max-pages "
                     "voor een betrouwbaar orphan-oordeel."),
        }
    except Exception as e:
        return {"error": str(e)[:200]}


def internal_link_graph(pages, damping=0.85, iterations=40):
    """Vereenvoudigde PageRank over de interne crawl-graaf (gesloten op gecrawlde
    nodes). Toont welke pagina's de meeste interne link-equity ontvangen."""
    try:
        nodes = []
        idx = {}
        for p in pages:
            nu = _norm_url(p.get("url", ""))
            if nu and nu not in idx:
                idx[nu] = len(nodes)
                nodes.append(nu)
        n = len(nodes)
        if n == 0:
            return {"nodes": 0, "edges": 0, "pagerank": [], "top": []}

        out_links = defaultdict(set)
        in_deg = defaultdict(int)
        edges = 0
        for p in pages:
            src = _norm_url(p.get("url", ""))
            if src not in idx:
                continue
            for link in (p.get("internal_links", []) or []):
                tgt = _norm_url(link)
                if tgt in idx and tgt != src:
                    if tgt not in out_links[src]:
                        out_links[src].add(tgt)
                        in_deg[tgt] += 1
                        edges += 1

        pr = {u: 1.0 / n for u in nodes}
        for _ in range(iterations):
            new = {u: (1.0 - damping) / n for u in nodes}
            dangling = 0.0
            for u in nodes:
                outs = out_links.get(u)
                if outs:
                    share = damping * pr[u] / len(outs)
                    for v in outs:
                        new[v] += share
                else:
                    dangling += damping * pr[u] / n
            if dangling:
                for u in nodes:
                    new[u] += dangling
            pr = new

        total = sum(pr.values()) or 1.0
        ranked = sorted(
            ({"url": u, "pagerank": round(pr[u] / total, 5),
              "pagerank_pct": round(100 * pr[u] / total, 2),
              "inbound_internal_links": in_deg.get(u, 0),
              "outbound_internal_links": len(out_links.get(u, set()))}
             for u in nodes),
            key=lambda x: -x["pagerank"])
        return {
            "nodes": n, "edges": edges,
            "avg_inbound": round(sum(in_deg.values()) / n, 2),
            "top": ranked[:15],
            "pagerank": ranked,
        }
    except Exception as e:
        return {"error": str(e)[:200], "pagerank": [], "top": []}


def analyze_site(pages, page_texts, sitemap_urls, dup_threshold=0.80):
    """Bundel alle onderscheidende analyses voor 1 site."""
    return {
        "near_duplicates": near_duplicate_clusters(pages, page_texts, threshold=dup_threshold),
        "orphans": orphan_analysis(pages, sitemap_urls),
        "link_graph": internal_link_graph(pages),
    }


# ============================================================================ #
# CONCURRENT-VERSLAAN — wie wint per signaal?
# Elke metric: (label, pad-naar-waarde, hoger_is_beter, formatter)
def _metric_rows():
    def gs(s, k, default=0):  # uit summary
        v = s.get(k)
        return default if v is None else v

    def cat(s, key):  # gemiddelde categorie-score uit seo.category_scores
        seo = s.get("seo") or {}
        cs = (seo.get("category_scores") or {}).get(key) or {}
        return cs.get("avg_pct", 0)

    return [
        ("SEO-score (0-100)", lambda s: (s.get("seo") or {}).get("site_score") or 0, True, lambda v: f"{v}"),
        ("Gem. woordenaantal", lambda s: gs(s, "avg_word_count"), True, lambda v: f"{int(v)}"),
        ("% met meta description", lambda s: gs(s, "pct_with_description"), True, lambda v: f"{int(v)}%"),
        ("Unieke descriptions", lambda s: gs(s, "unique_descriptions"), True, lambda v: f"{int(v)}"),
        ("% met canonical", lambda s: gs(s, "pct_with_canonical"), True, lambda v: f"{int(v)}%"),
        ("% met JSON-LD", lambda s: gs(s, "pct_with_jsonld"), True, lambda v: f"{int(v)}%"),
        ("% met precies 1 H1", lambda s: gs(s, "pct_single_h1"), True, lambda v: f"{int(v)}%"),
        ("% met breadcrumbs", lambda s: gs(s, "pct_with_breadcrumbs"), True, lambda v: f"{int(v)}%"),
        ("% afbeeldingen MET alt", lambda s: 100 - gs(s, "pct_images_missing_alt"), True, lambda v: f"{int(v)}%"),
        ("Gem. responstijd (ms)", lambda s: gs(s, "avg_response_ms"), False, lambda v: f"{int(v)}"),
        ("Producten met schema", lambda s: gs(s, "products_found"), True, lambda v: f"{int(v)}"),
        ("Technisch (cat-score)", lambda s: cat(s, "technical"), True, lambda v: f"{v}%"),
        ("Structured data (cat-score)", lambda s: cat(s, "structured_data"), True, lambda v: f"{v}%"),
        ("Content (cat-score)", lambda s: cat(s, "content"), True, lambda v: f"{v}%"),
    ]


def compare_sites(summaries):
    """Scoor sites naast elkaar; bepaal wie-wint per signaal + eindwinnaar."""
    try:
        sites = [s for s in summaries if s.get("pages")]
        if len(sites) < 2:
            return {"comparable": False, "reason": "minder dan 2 sites met pagina's"}
        rows = []
        wins = {s["domain"]: 0 for s in sites}
        for label, getter, higher, fmt in _metric_rows():
            vals = []
            for s in sites:
                try:
                    vals.append(float(getter(s)))
                except Exception:
                    vals.append(0.0)
            best = max(vals) if higher else min(vals)
            winners = [sites[i]["domain"] for i, v in enumerate(vals)
                       if abs(v - best) < 1e-9]
            # alleen 'tellen' als er onderscheid is
            if len({round(v, 3) for v in vals}) > 1:
                for w in winners:
                    wins[w] += 1
            rows.append({
                "signal": label, "higher_is_better": higher,
                "values": {sites[i]["domain"]: fmt(vals[i]) for i in range(len(sites))},
                "winner": winners[0] if len(winners) == 1 else "gelijk",
            })
        overall = sorted(wins.items(), key=lambda kv: -kv[1])
        champion = overall[0][0] if overall and overall[0][1] > (overall[1][1] if len(overall) > 1 else -1) else "gelijk"
        return {
            "comparable": True,
            "sites": [s["domain"] for s in sites],
            "rows": rows,
            "wins": wins,
            "ranking": [{"domain": d, "signals_won": w} for d, w in overall],
            "champion": champion,
        }
    except Exception as e:
        return {"comparable": False, "error": str(e)[:200]}
