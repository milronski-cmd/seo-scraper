# -*- coding: utf-8 -*-
"""
MODULE 1.5 — INTERNE-LINK-OPTIMIZER (plan §4, KEY=links_optimizer, ORDER=50)

Bouwt bovenop de bestaande PageRank- + orphan-analyse (analysis.link_graph /
analysis.orphans) en de per-pagina velden en levert een concreet, uitvoerbaar
intern-linkplan op. Zes deelanalyses:

  1. Anchor-tekst-advies per doelpagina — generieke ankers ("lees meer",
     "klik hier", ...) naar belangrijke doelpagina's krijgen 2-3 kant-en-klare
     NL-ankersuggesties afgeleid uit title/H1/keywords van de doelpagina.
  2. Silo-advies — clustert URL's op het eerste pad-segment (met plural-/synoniem-
     normalisatie voor platte .html-structuren), meet hub<->kind-koppeling en
     of content-silo's contextueel naar de commerciële silo linken.
  3. Ontbrekende gids->PLP/PDP-links — content-/gidspagina's zonder CONTEXTUELE
     (niet-menu) link naar een categorie-/productpagina, met de best passende
     doelen op woord-overlap.
  4. Breadcrumb-check — pagina's met pad-diepte >=2 zonder breadcrumbs,
     gegroepeerd per sjabloon/pad-prefix (geen 50 losse issues).
  5. Orphans benutten — analysis.orphans.orphan_sitemap met EERLIJKE nuance:
     bij een kleine crawl (crawled_pages << sitemap_urls) is een groot deel
     crawl-diepte-artefact; beide getallen worden genoemd.
  6. PageRank-verdeling — juridische/service-pagina's (privacy|voorwaarden|
     cookie|disclaimer|financiering|contact) hoog terwijl PLP/PDP laag/afwezig
     -> advies om meer interne links naar PLP's te leggen vanuit content + home
     (NIET: footer beperken/nofollow).

Fail-soft overal: nooit een exception naar buiten; ontbrekende analysis, lege
pages, kapotte DOM of ontbrekende velden -> degraderen + note. Geen netwerk.

Databronnen (zie INTEGRATION.md §3):
  ctx["pages"]         page-records (internal_links [url-strings], anchor_quality,
                       breadcrumbs, headings, title, word_count, url_structure,
                       keywords, products_found, screenshots{dom,ok})
  ctx["analysis"]      link_graph {nodes,edges,avg_inbound,top[],pagerank[]},
                       orphans {crawled_pages,sitemap_urls,orphan_sitemap[],...}
  ctx["sitemap_urls"]  gemergde sitemap-URL's (silo-omvang + orphan-context)
  ctx["out"]           Path van de site-outputmap (dom.html staat hieronder)
  ctx["domain"]        sitedomein
"""
import html as _htmllib
import re
from collections import defaultdict
from urllib.parse import urljoin, urlparse, urldefrag, parse_qs

KEY = "links_optimizer"
LABEL = "Interne-link-optimizer"
ORDER = 50

# ------------------------------------------------------------------ constanten
# Generieke/niet-beschrijvende ankerteksten (NL) — spec-set + nauwe varianten.
GENERIC_ANCHORS = {
    "lees meer", "lees verder", "meer lezen", "verder lezen", "klik hier",
    "klik", "hier", "meer info", "meer informatie", "meer", "bekijk", "bekijken",
    "bekijk hier", "deze pagina", "link", "ga naar", "verder", "ontdek meer",
    "meer weten", "read more", "click here", "hier klikken", "dit artikel",
    "zie hier", "kijk hier",
}

# Service-/juridische pagina's voor de PageRank-verdeling (spec §6, exact).
PAGERANK_SERVICE = ("privacy", "voorwaarden", "cookie", "disclaimer",
                    "financiering", "contact")
# Bredere service-set om deze pagina's uit de "content-pagina"-set te houden.
SERVICE_HINTS = PAGERANK_SERVICE + (
    "algemene-voorwaarden", "verzendkosten", "verzending", "retour", "garantie",
    "verzekering", "over-ons", "over_ons", "klantenservice", "betaal", "levering",
    "bezorg", "herroeping", "afspraak", "reviews", "vacature", "sitemap",
    "inloggen", "account", "winkelwagen", "checkout", "bedankt", "login",
)
# PLP/PDP-patronen (commercieel).
PLP_PDP_HINTS = ("/p/", "/product", "product-", "producten", "modellen", "model/",
                 "/merk", "merken", "merk=", "categorie", "/category", "/c/",
                 "collectie", "collection", "assortiment", "segment=", "/shop",
                 "shop/", "webshop", "aanbod", "kopen/", "/pd/", "artikel/")
# Content-/gids-patronen in de URL.
CONTENT_URL_HINTS = ("kennisbank", "blog", "/gids", "gidsen", "advies", "nieuws",
                     "ratgeber", "guide", "artikel", "artikelen", "/tips", "inspiratie",
                     "magazine", "achtergrond", "uitleg")
# Silo-synoniemen/plurals -> canonieke silo (platte .html-sites).
SILO_SYNONYM = {
    "gidsen": "gids", "kennisbank": "gids", "blog": "gids", "advies": "gids",
    "artikelen": "gids", "artikel": "gids", "nieuws": "gids",
    "merken": "merk", "modellen": "model", "producten": "product", "p": "product",
    "categorieen": "categorie", "categorien": "categorie",
}
STOP = {
    "de", "het", "een", "en", "van", "voor", "met", "in", "op", "te", "uw", "u",
    "je", "is", "of", "om", "aan", "bij", "naar", "per", "als", "dat", "die",
    "wat", "waar", "hoe", "wie", "dit", "deze", "der", "den", "ten", "ter", "d",
    "the", "a", "to", "and", "of", "your", "you", "html", "www", "nl", "com",
}

# Gewichten voor de eindscore (herschaald over de meetbare componenten).
WEIGHTS = {
    "anchor": 0.22,      # 1 - aandeel generieke contextuele ankers
    "guide_link": 0.20,  # content-pagina's met contextuele PLP/PDP-link
    "silo": 0.18,        # hub<->kind-koppeling + commerciele bereikbaarheid
    "pagerank": 0.15,    # balans commercieel vs service in de linkgraaf
    "breadcrumb": 0.15,  # diepe pagina's met breadcrumbs
    "orphan": 0.10,      # crawl-gecorrigeerde wees-ratio
}
GENERIC_ANCHOR_ISSUE_CAP = 12   # max losse anchor-issues (rest in data.linkplan)
LINKPLAN_CAP = 20               # spec: max 20 rijen in data.linkplan
CONTENT_WC_MIN = 700            # "hoog word_count zonder producten" drempel


# ------------------------------------------------------------------- helpers
def _esc(s):
    return _htmllib.escape(str(s if s is not None else ""))


def _brand_of(domain):
    d = (domain or "").lower().split(":")[0]
    d = re.sub(r"^www\.", "", d)
    return re.sub(r"[^a-z0-9]", "", d.split(".")[0]) if d else ""


def _reg_domain(domain):
    d = (domain or "").lower().split(":")[0]
    return re.sub(r"^www\.", "", d)


def _norm(url):
    """Canonieke match-key: schema+host lowercase, fragment weg, index.html en
    lege/'/'-paden -> host-root, enkele trailing slash weg, query behouden."""
    if not url:
        return ""
    url, _ = urldefrag(url.strip())
    try:
        pr = urlparse(url)
    except Exception:
        return url.lower()
    host = re.sub(r"^www\.", "", (pr.netloc or "").lower())
    path = pr.path or ""
    if path in ("", "/", "/index.html", "/index.htm", "/index.php"):
        base = f"{(pr.scheme or 'https').lower()}://{host}/"
        return base + (("?" + pr.query) if pr.query else "")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    key = f"{(pr.scheme or 'https').lower()}://{host}{path}"
    if pr.query:
        key += "?" + pr.query
    return key


def _display(url):
    """Korte weergave (pad+query) voor tabellen/issues."""
    try:
        pr = urlparse(url)
        d = (pr.path or "/") + (("?" + pr.query) if pr.query else "")
        return d or url
    except Exception:
        return url


def _is_internal(url, reg_domain):
    try:
        netloc = re.sub(r"^www\.", "", (urlparse(url).netloc or "").lower())
    except Exception:
        return False
    return (netloc == "") or (netloc == reg_domain) or netloc.endswith("." + reg_domain)


def _path_of(url):
    try:
        return urlparse(url).path or "/"
    except Exception:
        return "/"


def _depth(url, page=None):
    if page and isinstance(page.get("url_structure"), dict):
        d = page["url_structure"].get("depth")
        if isinstance(d, int):
            return d
    return len([s for s in _path_of(url).split("/") if s and s != "index.html"])


def _silo_of(url):
    pr_path = _path_of(url)
    segs = [s for s in pr_path.split("/") if s]
    if len(segs) >= 2:
        base = segs[0]
    elif len(segs) == 1:
        stem = re.sub(r"\.(html?|php|aspx)$", "", segs[0])
        base = stem.split("-")[0] if "-" in stem else stem
    else:
        return "(home)"
    base = base.lower()
    return SILO_SYNONYM.get(base, base)


def _tokens(*texts):
    out = []
    for t in texts:
        if not t:
            continue
        for w in re.split(r"[^0-9a-zà-ÿ]+", str(t).lower()):
            if len(w) >= 3 and w not in STOP:
                out.append(w)
    return out


def _overlap(a_tokens, b_tokens):
    if not a_tokens or not b_tokens:
        return 0
    return len(set(a_tokens) & set(b_tokens))


def _looks_generic(text):
    if not text:
        return True
    t = re.sub(r"[\s→›>»‹\-–—:.!?]+$", "", text.strip().lower())
    t = re.sub(r"^[\s←«‹<\-–—]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return (t in GENERIC_ANCHORS) or (len(t) <= 2)


def _is_service(url):
    u = url.lower()
    return any(h in u for h in SERVICE_HINTS)


def _is_pagerank_service(url):
    u = url.lower()
    return any(h in u for h in PAGERANK_SERVICE)


def _is_plp_pdp(url):
    u = url.lower()
    return any(h in u for h in PLP_PDP_HINTS)


def _is_content_page(page):
    url = (page.get("url") or "").lower()
    if any(h in url for h in CONTENT_URL_HINTS):
        return True
    wc = page.get("word_count") or 0
    pf = page.get("products_found") or 0
    if wc >= CONTENT_WC_MIN and pf == 0 and not _is_service(url) and not _is_plp_pdp(url):
        return True
    return False


def _humanize(slug):
    s = re.sub(r"\.(html?|php|aspx)$", "", slug or "")
    s = s.replace("-", " ").replace("_", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s


_ROMAN = {"ii", "iii", "iv", "vi", "vii", "viii", "ix"}


def _titlecase(s):
    words = []
    for w in s.split():
        if not w:
            continue
        if any(ch.isdigit() for ch in w) or w.lower() in _ROMAN:
            words.append(w.upper())            # modelcodes/romeinse cijfers: ST6D, X2, II
        else:
            words.append(w[:1].upper() + w[1:])
    return " ".join(words)


def _clean_title(t, brand=""):
    if not t:
        return ""
    parts = re.split(r"\s*[|·•]\s*|\s+[–—-]\s+", t)
    parts = [p.strip() for p in parts if p.strip()]
    if brand:
        kept = [p for p in parts if re.sub(r"[^a-z0-9]", "", p.lower()) != brand]
        parts = kept or parts
    return " – ".join(parts).strip() if parts else t.strip()


def _slug_based_anchors(url):
    """Ankersuggesties uit de URL zelf (werkt ook voor niet-gecrawlde doelen)."""
    low = url.lower()
    pr = urlparse(url)
    qs = parse_qs(pr.query)
    segs = [s for s in (pr.path or "").split("/") if s]
    stem = re.sub(r"\.(html?|php|aspx)$", "", segs[-1]) if segs else ""
    res = []
    if "segment=" in low and qs.get("segment"):
        seg = _humanize(qs["segment"][0])
        res += [f"{seg} modellen", f"bekijk de {seg} modellen"]
    elif "merk=" in low and qs.get("merk"):
        mk = _titlecase(_humanize(qs["merk"][0]))
        res += [mk, f"alle {mk}-modellen"]
    elif "/p/" in low or "/product" in low or "/pd/" in low:
        name = _titlecase(_humanize(stem))
        if name:
            res += [name, f"bekijk de {name}"]
    elif stem in ("modellen", "model", "producten", "product", "assortiment", "aanbod"):
        res += ["alle modellen", "het volledige assortiment"]
    elif stem in ("merken", "merk"):
        res += ["alle merken", "merkoverzicht"]
    else:
        h = _humanize(stem)
        if h and h not in ("index", "home"):
            res.append(_titlecase(h))
    return res


def _anchor_suggestions(target_url, page_by_key, brand, max_n=3):
    key = _norm(target_url)
    sugg = []
    p = page_by_key.get(key)
    if p:
        headings = p.get("headings") or {}
        h1 = (headings.get("h1") or [None])[0]
        title = _clean_title(p.get("title") or "", brand)
        kws = [t.get("term") for t in ((p.get("keywords") or {}).get("top_unigrams") or [])
               if t.get("term")][:4]
        if h1 and 3 <= len(h1) <= 65:
            sugg.append(re.sub(r"\s+", " ", h1).strip())
        if title and len(title) <= 70 and (not h1 or title.lower() != h1.lower()):
            sugg.append(title)
        if len(kws) >= 2:
            sugg.append(" ".join(kws[:3]))
        elif kws:
            sugg.append(kws[0])
    if len(sugg) < max_n:
        sugg += _slug_based_anchors(target_url)
    out, seen = [], set()
    for s in sugg:
        s2 = re.sub(r"\s+", " ", (s or "")).strip()
        k = s2.lower()
        if not s2 or k in seen or _looks_generic(s2):
            continue
        seen.add(k)
        out.append(s2)
        if len(out) >= max_n:
            break
    return out


def _anchor_text(a):
    """Effectieve ankertekst zoals een zoekmachine die leest: zichtbare tekst,
    anders aria-label van de <a>, anders de alt-tekst(en) van ingesloten <img>,
    anders het title-attribuut. Zo tellen logo-/beeldlinks met alt/aria NIET
    als 'lege' anker (voorkomt false positives op merk-/productlogo's)."""
    txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
    if txt:
        return txt
    al = re.sub(r"\s+", " ", (a.get("aria-label") or "")).strip()
    if al:
        return al
    alts = []
    try:
        for img in a.find_all("img"):
            alt = (img.get("alt") or img.get("aria-label") or "").strip()
            if alt:
                alts.append(alt)
    except Exception:
        pass
    if alts:
        return re.sub(r"\s+", " ", " ".join(alts)).strip()
    ti = re.sub(r"\s+", " ", (a.get("title") or "")).strip()
    return ti


# ------------------------------------------------------- DOM-anchor-parsing
def _parse_dom_edges(ctx, reg_domain, notes):
    """Lees per gecrawlde pagina de gerenderde DOM en verzamel interne link-edges
    met ankertekst + boilerplate-vlag (nav/header/footer). Fail-soft: pagina's
    zonder dom/bs4 vallen terug op internal_links (targets zonder anker).

    Retourneert:
      edges: list van (src_key, tgt_key, tgt_raw, anchor_text, is_boiler)
      anchors_ok: aantal pagina's waarvoor echte ankerteksten gelezen zijn
    """
    edges = []
    anchors_ok = 0
    out = ctx.get("out")
    pages = ctx.get("pages") or []
    fast = bool(ctx.get("fast"))

    BeautifulSoup = None
    if not fast:
        try:
            from bs4 import BeautifulSoup as _BS
            BeautifulSoup = _BS
        except Exception:
            notes.append("bs4 niet beschikbaar — ankerteksten uit internal_links "
                         "(zonder anchor); anchor-advies gedegradeerd.")
    elif fast:
        notes.append("fast-modus: geen DOM-anchorparsing — internal_links gebruikt.")

    boiler_tags = {"nav", "header", "footer"}
    boiler_words = ("nav", "menu", "header", "footer", "breadcrumb", "topbar",
                    "megamenu", "offcanvas", "drawer")

    for p in pages:
        src = p.get("url") or ""
        src_key = _norm(src)
        dom_rel = ((p.get("screenshots") or {}).get("dom")) if isinstance(p.get("screenshots"), dict) else None
        parsed_this = False
        if BeautifulSoup and out and dom_rel:
            try:
                from pathlib import Path
                dom_path = Path(out) / dom_rel
                if dom_path.exists():
                    soup = BeautifulSoup(dom_path.read_text(encoding="utf-8", errors="replace"),
                                         "html.parser")
                    for a in soup.find_all("a", href=True):
                        href = (a.get("href") or "").strip()
                        if not href or href.startswith(("#", "tel:", "mailto:",
                                                        "javascript:", "data:", "sms:", "whatsapp:")):
                            continue
                        tgt_raw = urljoin(src or ("https://" + reg_domain + "/"), href)
                        if not _is_internal(tgt_raw, reg_domain):
                            continue
                        tgt_key = _norm(tgt_raw)
                        if not tgt_key or tgt_key == src_key:
                            continue
                        text = _anchor_text(a)
                        is_boiler = False
                        for anc in a.parents:
                            nm = getattr(anc, "name", None)
                            if not nm:
                                continue
                            if nm in boiler_tags:
                                is_boiler = True
                                break
                            cls = " ".join(anc.get("class", []) or []).lower()
                            rid = (anc.get("id") or "").lower()
                            role = (anc.get("role") or "").lower()
                            if role == "navigation" or any(w in cls or w in rid for w in boiler_words):
                                is_boiler = True
                                break
                        edges.append((src_key, tgt_key, tgt_raw, text, is_boiler))
                        parsed_this = True
            except Exception as e:
                notes.append(f"DOM-parse mislukt voor {_display(src)} ({e}); internal_links gebruikt.")
        if parsed_this:
            anchors_ok += 1
        else:
            # Fallback: alleen targets (geen anchor, geen boiler-info).
            for tgt in (p.get("internal_links") or []):
                if not tgt or not _is_internal(tgt, reg_domain):
                    continue
                tk = _norm(tgt)
                if not tk or tk == src_key:
                    continue
                edges.append((src_key, tk, tgt, None, None))
    return edges, anchors_ok


# ---------------------------------------------------------------- de audit
def audit(ctx):
    try:
        return _audit_impl(ctx)
    except Exception as e:  # laatste vangnet — mag nooit crashen
        return {
            "score": None,
            "summary": "Interne-link-optimizer kon niet volledig draaien (fail-soft).",
            "issues": [{
                "severity": "Low", "category": "links",
                "title": "Interne-link-analyse gedegradeerd door een interne fout",
                "why": f"De audit ving een fout af en gaf een lege uitkomst terug: {e}",
                "fix": "Controleer analysis.json/pages.json van deze run; meld de fout aan de finalizer.",
                "url": "",
            }],
            "data": {"error": str(e), "notes": ["fail-soft: onverwachte fout in audit()"]},
        }


def _audit_impl(ctx):
    pages = ctx.get("pages") or []
    analysis = ctx.get("analysis") or {}
    sitemap_urls = ctx.get("sitemap_urls") or []
    domain = ctx.get("domain") or ""
    reg_domain = _reg_domain(domain)
    brand = _brand_of(domain)
    notes = []
    issues = []

    if not pages:
        return {
            "score": None,
            "summary": "Geen pagina's in deze run — interne-link-optimizer niet toepasbaar.",
            "issues": [],
            "data": {"notes": ["Lege pages: niets te analyseren."]},
        }

    page_by_key = {_norm(p.get("url", "")): p for p in pages if p.get("url")}
    crawled_keys = set(page_by_key.keys())

    # --- DOM-edges (ankerteksten) ------------------------------------------
    edges, anchors_ok = _parse_dom_edges(ctx, reg_domain, notes)
    have_anchors = anchors_ok > 0

    # inbound (distinct bron-pagina's) + set van doel-URL's (raw voor weergave)
    inbound_sources = defaultdict(set)
    target_raw = {}
    contextual_out = defaultdict(list)   # src_key -> list of (tgt_key, tgt_raw, text, boiler)
    for (src, tgt, traw, text, boiler) in edges:
        inbound_sources[tgt].add(src)
        target_raw.setdefault(tgt, traw)
        contextual_out[src].append((tgt, traw, text, boiler))

    # link_graph inbound is gezaghebbend voor gecrawlde nodes
    link_graph = analysis.get("link_graph") or {}
    lg_inbound = {}
    for node in (link_graph.get("pagerank") or link_graph.get("top") or []):
        if isinstance(node, dict) and node.get("url"):
            lg_inbound[_norm(node["url"])] = node.get("inbound_internal_links")

    def inbound_of(tgt_key):
        a = len(inbound_sources.get(tgt_key, ()))
        b = lg_inbound.get(tgt_key)
        return max(a, b) if isinstance(b, int) else a

    # ================================================== 1. ANCHOR-ADVIES
    generic_total = 0
    contextual_total = 0
    generic_targets = defaultdict(list)   # tgt_key -> list of (src_key, anchor)
    if have_anchors:
        for (src, tgt, traw, text, boiler) in edges:
            if text is None:
                continue
            if boiler:
                continue  # menu-links niet meetellen (elke pagina herhaalt het menu)
            contextual_total += 1
            if _looks_generic(text):
                generic_total += 1
                important = _is_plp_pdp(traw) or inbound_of(tgt) >= 2 or tgt in lg_inbound
                if important:
                    generic_targets[tgt].append((src, text))
    else:
        notes.append("Geen gerenderde DOM beschikbaar — anchor-generiek-analyse "
                     "overgeslagen (draai met --screenshots voor ankerteksten).")

    anchor_issue_count = 0
    for tgt_key, hits in sorted(generic_targets.items(),
                                key=lambda kv: (-inbound_of(kv[0]), kv[0])):
        if anchor_issue_count >= GENERIC_ANCHOR_ISSUE_CAP:
            notes.append(f"Anchor-issues afgekapt op {GENERIC_ANCHOR_ISSUE_CAP}; "
                         f"resterende doelen staan in data.linkplan.")
            break
        traw = target_raw.get(tgt_key, tgt_key)
        sugg = _anchor_suggestions(traw, page_by_key, brand)
        if not sugg:
            continue
        srcs = sorted({_display(s) for s, _ in hits})
        used = sorted({t for _, t in hits if t})
        commercieel = "commerciele " if _is_plp_pdp(traw) else ""
        if used:
            anker_desc = "vage ankertekst (" + ", ".join(repr(u) for u in used[:3]) + ")"
            title = f"Generieke ankertekst naar {_display(traw)}"
        else:
            anker_desc = "ontbrekende ankertekst (link zonder zichtbare tekst, alt of aria-label)"
            title = f"Link zonder ankertekst naar {_display(traw)}"
        sev = "High" if _is_plp_pdp(traw) else "Medium"
        issues.append({
            "severity": sev, "category": "anchor",
            "title": title,
            "why": (f"{len(hits)} interne link(s) wijzen met {anker_desc} naar deze "
                    f"{commercieel}doelpagina; zoekmachines lezen de ankertekst als "
                    f"onderwerp-signaal, dus vage of lege ankers verspillen relevantie."),
            "fix": ("Gebruik beschrijvende ankertekst, bv.: "
                    + " | ".join(f"“{a}”" for a in sugg)
                    + f". Aanpassen op: {', '.join(srcs[:4])}."),
            "url": traw,
        })
        anchor_issue_count += 1

    anchor_score = round(100.0 * (1 - generic_total / contextual_total), 1) if contextual_total else None

    # ================================================== 2. SILO-ADVIES
    silo_members = defaultdict(set)
    all_internal = set(crawled_keys)
    for p in pages:
        for l in (p.get("internal_links") or []):
            if _is_internal(l, reg_domain):
                all_internal.add(_norm(l))
    for u in sitemap_urls:
        if _is_internal(u, reg_domain):
            all_internal.add(_norm(u))
    for k in all_internal:
        silo_members[_silo_of(k)].add(k)

    # hub per silo = de échte categorie-landing: pad met precies één segment dat
    # (genormaliseerd, zonder extensie) exact de silo-naam of een synoniem is
    # (bv. /merken, /e-steps, /product/). Nooit zomaar het kortste LID: op platte
    # PDP-structuren (/product/<sku>) is dat een productpagina en die als "hub"
    # adviseren is ruis (Fable-gate-bevinding op movevolt.nl, 02-07).
    def _hub_of(silo):
        cands = silo_members.get(silo) or set()
        if not cands:
            return None
        names = {silo} | {syn for syn, canon in SILO_SYNONYM.items() if canon == silo}
        for u in sorted(cands, key=lambda c: ("?" in c, len(_path_of(c)), c)):
            segs = [s for s in _path_of(u).split("/") if s]
            if len(segs) != 1:
                continue
            stem = re.sub(r"\.(html?|php|aspx)$", "", segs[0]).lower()
            if stem in names:
                return u
        return None

    commercial_silos = {s for s in silo_members
                        if s in ("model", "merk", "product", "categorie")
                        or any(_is_plp_pdp(u) for u in silo_members[s])}

    # hub-backlink: linkt elke gecrawlde niet-hub-pagina naar zijn silo-hub?
    # Gefaalde pagina's per (silo, hub) GEGROEPEERD tot één issue (geen ruis).
    hub_need = hub_ok = 0
    hub_missing = {}
    for p in pages:
        src_key = _norm(p.get("url", ""))
        silo = _silo_of(src_key)
        hub = _hub_of(silo)
        if not hub or hub == src_key or silo == "(home)":
            continue
        if len(silo_members.get(silo, ())) < 2:
            continue
        hub_need += 1
        links_to_hub = any(t == hub for (t, _tr, _tx, _b) in contextual_out.get(src_key, []))
        if links_to_hub:
            hub_ok += 1
        else:
            hub_missing.setdefault((silo, hub), []).append(src_key)
    for (silo, hub), srcs in hub_missing.items():
        voorbeelden = ", ".join(_display(s) for s in srcs[:3])
        issues.append({
            "severity": "Low", "category": "silo",
            "title": f"{len(srcs)} pagina('s) in silo '{silo}' linken niet naar de hub ({_display(hub)})",
            "why": (f"Deze pagina's horen bij silo '{silo}' maar linken niet terug naar de "
                    f"categorie-/overzichtspagina; dat verzwakt de thematische cluster en de "
                    f"doorstroming van link-equity. Voorbeelden: {voorbeelden}."),
            "fix": f"Voeg op deze pagina's een contextuele link toe naar {_display(hub)}.",
            "url": srcs[0] if srcs else "",
        })

    # commerciele bereikbaarheid: linken content/home-pagina's contextueel naar een commerciele silo?
    comm_need = comm_ok = 0
    for p in pages:
        src_key = _norm(p.get("url", ""))
        if not (_is_content_page(p) or _silo_of(src_key) == "(home)"):
            continue
        comm_need += 1
        if have_anchors:
            reaches = any((not b) and _silo_of(t) in commercial_silos
                          for (t, _tr, _tx, b) in contextual_out.get(src_key, []))
        else:
            reaches = any(_silo_of(_norm(l)) in commercial_silos
                          for l in (p.get("internal_links") or []) if _is_internal(l, reg_domain))
        if reaches:
            comm_ok += 1

    silo_parts = []
    if hub_need:
        silo_parts.append(hub_ok / hub_need)
    if comm_need and commercial_silos:
        silo_parts.append(comm_ok / comm_need)
    silo_score = round(100.0 * sum(silo_parts) / len(silo_parts), 1) if silo_parts else None
    if commercial_silos and comm_need and comm_ok == 0:
        hubs = [_display(_hub_of(s)) for s in list(commercial_silos)[:3] if _hub_of(s)]
        issues.append({
            "severity": "Medium", "category": "silo",
            "title": "Content- en homepagina's linken niet naar de commerciele silo('s)",
            "why": ("Gidsen/kennisbank en de homepage sturen binnen deze crawl geen "
                    "contextuele link naar de categorie-/productsilo; link-equity en "
                    "koopintentie blijven in de content hangen."),
            "fix": ("Voeg in de lopende tekst van je gidsen en op de homepage links toe "
                    f"naar de relevante PLP's (bv. hub {', '.join(hubs)})."),
            "url": "",
        })

    # ================================================== 3. GIDS -> PLP/PDP
    commercial_candidates = sorted(
        {k for k in all_internal if _is_plp_pdp(target_raw.get(k, k))},
        key=lambda k: (0 if _is_plp_pdp(k) else 1, _path_of(k))
    )
    content_pages = [p for p in pages if _is_content_page(p)]
    guide_ok = 0
    guide_missing = []
    for p in content_pages:
        src_key = _norm(p.get("url", ""))
        outs = contextual_out.get(src_key, [])
        if have_anchors:
            has_plp = any((not b) and _is_plp_pdp(tr) for (_t, tr, _tx, b) in outs)
        else:
            has_plp = any(_is_plp_pdp(l) for l in (p.get("internal_links") or []))
        if has_plp:
            guide_ok += 1
            continue
        guide_missing.append(p)
        g_tokens = _tokens(p.get("title"), ((p.get("headings") or {}).get("h1") or [None])[0],
                           " ".join(t.get("term", "") for t in
                                    ((p.get("keywords") or {}).get("top_unigrams") or [])[:8]))
        scored = []
        for ck in commercial_candidates:
            cp = page_by_key.get(ck)
            if cp:
                c_tokens = _tokens(cp.get("title"), ((cp.get("headings") or {}).get("h1") or [None])[0])
            else:
                q = urlparse(target_raw.get(ck, ck)).query
                c_tokens = _tokens(_humanize(_path_of(ck).split("/")[-1]),
                                   parse_qs(q).get("segment", [""])[0],
                                   parse_qs(q).get("merk", [""])[0])
            scored.append((_overlap(g_tokens, c_tokens), ck))
        scored.sort(key=lambda t: (-t[0], _path_of(t[1])))
        targets = [target_raw.get(k, k) for s, k in scored[:3] if s > 0] or \
                  [target_raw.get(k, k) for _s, k in scored[:2]]
        if not targets:
            continue
        tnames = [f"{_display(t)}" for t in targets]
        issues.append({
            "severity": "Medium", "category": "gids-link",
            "title": f"Gids zonder link naar een categorie-/productpagina: {_display(p.get('url',''))}",
            "why": ("Deze content-/gidspagina heeft veel tekst maar linkt (in de lopende "
                    "tekst) niet door naar een PLP/PDP; bezoekers met koopintentie lopen "
                    "dood en de pagina geeft geen link-equity door naar commerciele pagina's."),
            "fix": ("Voeg 1-3 contextuele links toe naar de best passende doelen: "
                    + ", ".join(tnames) + "."),
            "url": p.get("url", ""),
        })
    guide_link_score = round(100.0 * guide_ok / len(content_pages), 1) if content_pages else None

    # ================================================== 4. BREADCRUMB-CHECK
    deep_need = deep_ok = 0
    bc_missing_by_prefix = defaultdict(list)
    for p in pages:
        url = p.get("url", "")
        if _depth(url, p) < 2:
            continue
        deep_need += 1
        bc = p.get("breadcrumbs")
        has_bc = isinstance(bc, list) and len([x for x in bc if str(x).strip()]) >= 1
        if has_bc:
            deep_ok += 1
        else:
            prefix = "/" + (_path_of(url).lstrip("/").split("/")[0] or "")
            bc_missing_by_prefix[prefix].append(url)
    for prefix, urls in sorted(bc_missing_by_prefix.items(), key=lambda kv: -len(kv[1])):
        issues.append({
            "severity": "Medium", "category": "breadcrumb",
            "title": f"{len(urls)} pagina('s) onder {prefix}/ zonder breadcrumbs (pad-diepte >=2)",
            "why": ("Diepe pagina's zonder breadcrumb-navigatie missen context voor "
                    "gebruiker en zoekmachine (geen BreadcrumbList-schema, zwakkere "
                    "hierarchie-signalen en slechtere terugnavigatie)."),
            "fix": (f"Voeg een breadcrumb-trail toe op dit sjabloon "
                    f"(bv. {_display(urls[0])}); een component dekt alle {len(urls)} pagina's."),
            "url": urls[0],
        })
    breadcrumb_score = round(100.0 * deep_ok / deep_need, 1) if deep_need else None

    # ================================================== 5. ORPHANS
    orphans = analysis.get("orphans") or {}
    orphan_score = None
    orphan_meta = {}
    if orphans:
        crawled_pages = orphans.get("crawled_pages") or len(pages)
        sm_urls = orphans.get("sitemap_urls") or len(sitemap_urls)
        orphan_sitemap = orphans.get("orphan_sitemap") or []
        orphan_sitemap_count = orphans.get("orphan_sitemap_count", len(orphan_sitemap))
        orphan_crawled = orphans.get("orphan_crawled") or []
        orphan_crawled_count = orphans.get("orphan_crawled_count", len(orphan_crawled))
        crawl_ratio = (crawled_pages / sm_urls) if sm_urls else 1.0
        is_artifact = bool(sm_urls) and crawl_ratio < 0.5
        orphan_meta = {
            "crawled_pages": crawled_pages, "sitemap_urls": sm_urls,
            "orphan_sitemap_count": orphan_sitemap_count,
            "orphan_crawled_count": orphan_crawled_count,
            "crawl_ratio": round(crawl_ratio, 3), "largely_crawl_artifact": bool(is_artifact),
        }
        if orphan_crawled_count:
            sample = ", ".join(_display(u) for u in orphan_crawled[:5])
            issues.append({
                "severity": "High", "category": "orphan",
                "title": f"{orphan_crawled_count} gecrawlde pagina('s) zonder enige interne inbound-link",
                "why": ("Deze pagina's zijn wel gecrawld maar krijgen geen enkele interne "
                        "link — echte wezen die nauwelijks vindbaar/indexeerbaar zijn."),
                "fix": f"Link ze vanuit relevante hubs/gidsen. Voorbeelden: {sample}.",
                "url": orphan_crawled[0] if orphan_crawled else "",
            })
        if orphan_sitemap_count:
            if is_artifact:
                sev = "Low"
                why = (f"{orphan_sitemap_count} sitemap-URL's zijn niet bereikt binnen deze "
                       f"crawl van {crawled_pages} pagina('s) (sitemap telt {sm_urls}). "
                       f"Bij zo'n kleine crawl is dit grotendeels een crawl-diepte-artefact, "
                       f"niet per se echt wees — vergroot --max-pages voor een betrouwbaar oordeel.")
                fix = (f"Draai een diepere crawl (bv. --max-pages {min(sm_urls, max(50, crawled_pages*10))}) "
                       f"en herbeoordeel; controleer daarna de dan nog niet-gelinkte URL's.")
            else:
                sev = "Medium" if crawl_ratio < 0.85 else "High"
                why = (f"{orphan_sitemap_count} van {sm_urls} sitemap-URL's kregen geen interne "
                       f"link (crawl bereikte {crawled_pages} pagina's, {round(crawl_ratio*100)}%). "
                       f"Deze pagina's staan wel in de sitemap maar zijn intern niet bereikbaar.")
                fix = ("Voeg interne links toe vanuit categorie-/hub-pagina's en gerelateerde "
                       "content naar deze URL's, of verwijder ze uit de sitemap als ze niet relevant zijn.")
            issues.append({
                "severity": sev, "category": "orphan",
                "title": f"{orphan_sitemap_count} sitemap-URL's zonder interne link "
                         f"(crawl: {crawled_pages}/{sm_urls})",
                "why": why, "fix": fix,
                "url": orphan_sitemap[0] if orphan_sitemap else "",
            })
        true_pen = 100.0 * (orphan_crawled_count / crawled_pages) if crawled_pages else 0.0
        sm_pen = 0.0
        if sm_urls:
            sm_pen = 50.0 * (orphan_sitemap_count / sm_urls) * crawl_ratio
        orphan_score = round(max(0.0, min(100.0, 100.0 - true_pen - sm_pen)), 1)
    else:
        notes.append("Geen orphan-analyse in analysis.json — orphan-check overgeslagen.")

    # ================================================== 6. PAGERANK-VERDELING
    pagerank_score = None
    pr_nodes = link_graph.get("pagerank") or link_graph.get("top") or []
    pr_nodes = [n for n in pr_nodes if isinstance(n, dict) and n.get("url")]
    pr_top_meta = []
    if len(pr_nodes) >= 3:
        n = len(pr_nodes)
        top_half = pr_nodes[:max(1, (n + 1) // 2)]
        service_top = [x for x in top_half if _is_pagerank_service(x["url"])]
        commercial_any = [x for x in pr_nodes if _is_plp_pdp(x["url"])]
        commercial_in_top = [x for x in top_half if _is_plp_pdp(x["url"])]
        pr_top_meta = [{"url": x["url"], "pagerank_pct": x.get("pagerank_pct")} for x in top_half]
        fired = bool(service_top) and not commercial_in_top
        if fired:
            svc_pct = sum((x.get("pagerank_pct") or 0) for x in service_top)
            svc_names = ", ".join(_display(x["url"]) for x in service_top[:3])
            if commercial_any:
                extra = (f"Commerciele pagina's staan wel in de graaf maar lager "
                         f"(bv. {_display(commercial_any[0]['url'])}).")
            else:
                extra = ("Er staat binnen deze crawl geen enkele PLP/PDP in de interne "
                         "linkgraaf — de commerciele pagina's krijgen dus geen interne link-equity "
                         "(deels een gevolg van de crawlomvang).")
            issues.append({
                "severity": "Medium", "category": "pagerank",
                "title": "Service-/juridische pagina's domineren de interne PageRank",
                "why": (f"De interne link-equity gaat vooral naar {svc_names} "
                        f"(~{round(svc_pct)}% van de PageRank in de top), terwijl commerciele "
                        f"PLP/PDP-pagina's ontbreken of lager staan. {extra}"),
                "fix": ("Leg meer interne links naar je PLP's en topproducten vanuit de "
                        "content/gidsen en de homepage (bv. 'bekijk alle modellen', merk- en "
                        "segmentpagina's in de lopende tekst), zodat link-equity naar "
                        "commerciele pagina's stroomt."),
                "url": service_top[0]["url"],
            })
            svc_share = min(1.0, svc_pct / 100.0)
            pagerank_score = round(max(20.0, 100.0 - 30.0 - 40.0 * svc_share), 1)
        else:
            pagerank_score = 100.0
    else:
        if not pr_nodes:
            notes.append("Geen link_graph in analysis.json — PageRank-verdeling overgeslagen.")
        else:
            notes.append(f"Linkgraaf te klein ({len(pr_nodes)} nodes) voor een "
                         f"PageRank-verdelingsoordeel.")

    # ================================================== LINKPLAN (data + html)
    cand_keys = set(generic_targets.keys())
    for k in all_internal:
        if _is_plp_pdp(target_raw.get(k, k)):
            cand_keys.add(k)
    for k in lg_inbound:
        cand_keys.add(k)

    def _importance(k):
        # Prioriteer commerciële doelen die interne links NODIG hebben: onder-gelinkte
        # pagina's en pagina's met generieke ankers eerst; goed gelinkte pagina's lager.
        traw = target_raw.get(k, k)
        inb = inbound_of(k)
        base = 2 if _is_plp_pdp(traw) else (1 if k in lg_inbound else 0)
        generic_bonus = 1 if k in generic_targets else 0
        referenced = 0.6 if inb >= 1 else 0.0      # in de crawl gezien = actiegerichter
        need = max(0, 2 - inb) * 0.2               # onder-gelinkt = hogere prioriteit
        return base + generic_bonus + referenced + need
    ranked = sorted(cand_keys, key=lambda k: (-_importance(k), inbound_of(k), _path_of(k)))
    linkplan = []
    content_home_srcs = [p for p in pages
                         if _is_content_page(p) or _silo_of(_norm(p.get("url", ""))) == "(home)"]
    for k in ranked[:LINKPLAN_CAP]:
        traw = target_raw.get(k, k)
        already = set(inbound_sources.get(k, set()))
        q = urlparse(traw).query
        t_tokens = _tokens((page_by_key.get(k) or {}).get("title"),
                           _humanize(_path_of(k).split("/")[-1]),
                           parse_qs(q).get("segment", [""])[0],
                           parse_qs(q).get("merk", [""])[0])
        vanaf = []
        for sp in content_home_srcs:
            sk = _norm(sp.get("url", ""))
            if sk == k or sk in already:
                continue
            s_tokens = _tokens(sp.get("title"), " ".join(
                t.get("term", "") for t in ((sp.get("keywords") or {}).get("top_unigrams") or [])[:6]))
            if _overlap(t_tokens, s_tokens) >= 1:
                vanaf.append(_display(sp.get("url", "")))
            if len(vanaf) >= 3:
                break
        linkplan.append({
            "doel_url": traw,
            "huidige_inbound": inbound_of(k),
            "advies_anchors": _anchor_suggestions(traw, page_by_key, brand),
            "link_vanaf": vanaf,
        })
    capped = len(ranked) > LINKPLAN_CAP
    if capped:
        notes.append(f"Linkplan afgekapt op {LINKPLAN_CAP} doelen van {len(ranked)} kandidaten "
                     f"(data.capped=true).")

    # ---------------------------------------------------- EINDSCORE (gewogen)
    subscores = {
        "anchor": anchor_score, "guide_link": guide_link_score, "silo": silo_score,
        "pagerank": pagerank_score, "breadcrumb": breadcrumb_score, "orphan": orphan_score,
    }
    num = den = 0.0
    weights_used = {}
    for name, val in subscores.items():
        if isinstance(val, (int, float)):
            w = WEIGHTS[name]
            num += w * val
            den += w
            weights_used[name] = w
    score = round(num / den, 1) if den else None

    # 'Geen stille None'-vangnet: pagina's aanwezig maar geen enkele deeldimensie
    # meetbaar -> benoem exact welke input ontbrak (+ hoe die te verkrijgen) i.p.v. kaal None.
    none_reason = ""
    if score is None:
        missing = []
        if not have_anchors:
            missing.append("gerenderde DOM met ankerteksten (draai de scraper met --screenshots)")
        if not link_graph:
            missing.append("analysis.link_graph (PageRank/inbound-links)")
        if not orphans:
            missing.append("analysis.orphans (wees-analyse)")
        if not content_pages:
            missing.append("herkenbare content-/gidspagina's (voor de gids->PLP-dimensie)")
        if deep_need == 0:
            missing.append("pagina's met pad-diepte >=2 (voor de breadcrumb-dimensie)")
        none_reason = "; ".join(missing) or ("onbekende reden — controleer pages.json en "
                                             "analysis.json van deze run")
        issues.append({
            "severity": "Low", "category": "links",
            "title": "Interne-link-score niet te berekenen — meetbare input ontbreekt",
            "why": ("Geen van de zes deelanalyses (anchor, silo, gids->PLP, breadcrumb, orphan, "
                    "PageRank) was meetbaar op deze run, dus er is geen betrouwbare totaalscore. "
                    "Ontbrekende input: " + none_reason + "."),
            "fix": ("Draai de scraper met --screenshots (voor ankerteksten) en een ruimere "
                    "--max-pages zodat de link_graph- en orphan-analyse gevuld raken; controleer "
                    "daarna of analysis.json de sleutels 'link_graph' en 'orphans' bevat."),
            "url": "",
        })
        notes.append("score=None: geen meetbare deeldimensie — informatieve Low-issue "
                     "toegevoegd (geen stille None).")

    meas = [k for k, v in subscores.items() if isinstance(v, (int, float))]
    if score is None:
        summary = ("Interne-link-score niet te berekenen: geen meetbare deeldimensie op deze "
                   "run. Ontbrekende input: " + none_reason + ". Zie de Low-bevinding voor de "
                   "oplossing.")
    else:
        summary = (f"Linkplan met {len(linkplan)} doel(en); "
                   f"{len(issues)} bevinding(en). "
                   f"Gemeten dimensies: {', '.join(meas) if meas else 'geen'}"
                   f"{' (deels gedegradeerd)' if notes else ''}.")

    html_out = _render_html(linkplan, capped, len(ranked), subscores, orphan_meta, notes)

    order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    issues.sort(key=lambda it: order.get(it.get("severity", "Low"), 3))

    data = {
        "linkplan": linkplan,
        "capped": capped,
        "linkplan_total_candidates": len(ranked),
        "subscores": subscores,
        "weights_used": weights_used,
        "generic_anchor": {
            "contextual_internal_links": contextual_total,
            "generic_count": generic_total,
            "generic_ratio": round(generic_total / contextual_total, 3) if contextual_total else None,
            "anchor_source": "dom.html" if have_anchors else "internal_links (geen anchors)",
        },
        "silo": {
            "silos": {s: len(m) for s, m in sorted(silo_members.items(), key=lambda kv: -len(kv[1]))},
            "commercial_silos": sorted(commercial_silos),
            "hub_backlink": {"need": hub_need, "ok": hub_ok},
            "commercial_reach": {"need": comm_need, "ok": comm_ok},
        },
        "guide_links": {
            "content_pages": len(content_pages),
            "with_plp_link": guide_ok,
            "missing": [p.get("url") for p in guide_missing],
        },
        "breadcrumb": {"deep_pages": deep_need, "deep_with_breadcrumb": deep_ok},
        "orphans": orphan_meta,
        "pagerank_top": pr_top_meta,
        "notes": notes,
    }

    return {
        "score": score,
        "summary": summary,
        "issues": issues,
        "data": data,
        "html": html_out,
    }


# ------------------------------------------------------------------- render
def _render_html(linkplan, capped, total, subscores, orphan_meta, notes):
    if not linkplan:
        return ('<div style="margin-top:10px;color:#8a94a6;font-size:13px">'
                'Geen linkplan-doelen afgeleid uit deze run.</div>')
    css_td = "padding:7px 9px;border-bottom:1px solid #e6e8ee;vertical-align:top;font-size:12.5px"
    css_th = ("padding:8px 9px;text-align:left;font-size:11px;text-transform:uppercase;"
              "letter-spacing:.04em;color:#5b6472;border-bottom:2px solid #d7dbe3")
    rows = []
    for i, r in enumerate(linkplan):
        bg = "#ffffff" if i % 2 == 0 else "#f7f8fb"
        anchors = r.get("advies_anchors") or []
        an_html = "".join(
            f'<span style="display:inline-block;background:#eef4ff;color:#1c4bd6;'
            f'border:1px solid #cfe0ff;border-radius:5px;padding:1px 6px;margin:1px 3px 1px 0;'
            f'font-size:11.5px">{_esc(a)}</span>' for a in anchors) or \
            '<span style="color:#8a94a6">—</span>'
        vanaf = r.get("link_vanaf") or []
        inb = r.get("huidige_inbound", 0)
        if vanaf:
            vh = "<br>".join(_esc(v) for v in vanaf)
        elif inb == 0:
            vh = '<span style="color:#8a94a6">koppel vanuit relevante hub/gids</span>'
        else:
            vh = '<span style="color:#8a94a6">voldoende inbound</span>'
        inb_color = "#c0392b" if inb == 0 else ("#b8860b" if inb == 1 else "#2e7d32")
        rows.append(
            f'<tr style="background:{bg}">'
            f'<td style="{css_td}"><code style="font-size:11.5px;color:#243044">{_esc(_display(r.get("doel_url","")))}</code></td>'
            f'<td style="{css_td};text-align:center;font-weight:700;color:{inb_color}">{_esc(inb)}</td>'
            f'<td style="{css_td}">{an_html}</td>'
            f'<td style="{css_td};color:#3a4658">{vh}</td>'
            f'</tr>')
    sub_bits = " · ".join(
        f'{k}: {("%.0f" % v) if isinstance(v,(int,float)) else "n.v.t."}'
        for k, v in subscores.items())
    cap_note = (f'<div style="margin-top:6px;font-size:11.5px;color:#b8860b">'
                f'Linkplan toont de top {len(linkplan)} van {total} kandidaat-doelen '
                f'(data.capped=true).</div>' if capped else "")
    orph = ""
    if orphan_meta:
        orph = (f'<div style="margin-top:6px;font-size:11.5px;color:#5b6472">'
                f'Crawl: {_esc(orphan_meta.get("crawled_pages"))}/{_esc(orphan_meta.get("sitemap_urls"))} '
                f'sitemap-URL\'s bereikt'
                + (' — sitemap-wezen zijn grotendeels crawl-diepte-artefact.'
                   if orphan_meta.get("largely_crawl_artifact") else '.')
                + '</div>')
    note_html = ""
    if notes:
        note_html = ('<div style="margin-top:6px;font-size:11px;color:#8a94a6">'
                     'Degradatie/notities: ' + _esc(" ".join(notes[:4])) + '</div>')
    return f"""<div style="margin-top:12px">
  <div style="font-size:12.5px;color:#243044;margin-bottom:6px">
    <b>Intern-linkplan</b> — concrete ankerteksten + waar de link vandaan moet komen.
    <span style="color:#8a94a6">Deelscores: {_esc(sub_bits)}</span>
  </div>
  <div style="overflow-x:auto;border:1px solid #e6e8ee;border-radius:8px">
  <table style="border-collapse:collapse;width:100%;min-width:640px;background:#fff">
    <thead><tr style="background:#fbfcfe">
      <th style="{css_th}">Doelpagina</th>
      <th style="{css_th};text-align:center">Inbound&nbsp;nu</th>
      <th style="{css_th}">Aanbevolen ankerteksten</th>
      <th style="{css_th}">Link toevoegen vanaf</th>
    </tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
  </div>{cap_note}{orph}{note_html}
</div>"""
