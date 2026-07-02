# CHANGELOG — seo_scraper_v2 (canoniek: shared\from-agent5\2026-06-11_seo-scraper-v2)

## v2.3.1 — 2026-07-02 (Sage) — render-metadata + design-audits (fase 1b)
- Module 1.1 uitgebreid met **render-metadata-extractie** (EXTRACT_JS in
  screenshots.py): per pagina en per viewport de computed styles van alle
  zichtbare images (incl. kaartachtergrond-kleur), tekst-elementen (kleur/
  effectieve bg/fontSize/positie) en icoon-kandidaten (svg-stroke, iconfont-
  families, icoon-imgs, emoji) → `screenshots\<slug>\render_meta.json`.
  Gecapt en fail-soft; gedocumenteerd in INTEGRATION.md §4.
- Design-audit-modules 1.3 (`images_visual`), 1.4 (`icons`) en 1.7 (`contrast`)
  toegevoegd via de audits-registry — zie de wiring-notes in `audits\_wiring\`
  en de acceptatie-runs in `shared\seo-runs\2026-07-02-fase1-designaudits\`.
- **safe_name-fix**: image-downloads krijgen nu ALTIJD een korte pad-hash in de
  bestandsnaam (`<stem>_<hash8>.<ext>`) — per-product-submappen met gelijke
  bestandsnamen (p00.cut.webp) overschreven elkaar en maakten de beeld-audit
  half blind (movevolt: 139 foto's → 3 bestanden; nu 39 distinct, spreiding
  31-99%). Audits krijgen de functie via `ctx["safe_name"]`; de helper valt
  terug op het legacy-formaat voor oudere run-output. numpy geborgd in
  INTEGRATION.md §8 (naast Pillow).
- Sanity vs handmatige MoveVolt-nulmeting (02-07 ochtend, ~69% vulling,
  ~5/14 wit-op-donker): kadervulling reproduceert (64,6% gem., 26/139 <70%);
  wit-op-donker = 0 is CORRECT — movevolt is inmiddels een licht thema
  (premium-pass Janus 17:05, visueel geverifieerd); detector bewezen op
  synthetische donkere kaart.

## v2.3.0 — 2026-07-02 (Sage) — FASE 1-KICKOFF: module 1.1 + audits-registry
- **Module 1.1 Screenshot-capture** (`screenshots.py`, vlag `--screenshots`):
  per pagina desktop 1440x900 + mobiel 390x844 (echte mobiele context, DSF 2),
  above-the-fold + full-page PNG, én een gerenderde DOM-snapshot ná JS +
  lazy-load-scroll → `<site>\screenshots\<slug>\` (+ meta.json + manifest.json).
  Eén browser / herbruikbare contexts; fail-soft per pagina (note, nooit crash).
- **Audits-registry** (`audits\` + runner in `_finalize`): plugin-contract voor
  modules 1.2-1.7 — bestand droppen = meedraaien; generieke report-sectie met
  score-pill + issues-tabel per audit; resultaten in analysis.json +
  summaries.json (`audits_summary`). Contract: `INTEGRATION.md` (gereserveerde
  keys/orders per module). Referentie-implementatie:
  `audits\example_render_coverage.py`. Sneltest-harnas: `_audit_harness.py`
  (audits draaien op bestaande run-output, zonder crawl).
- report.html: nieuwe secties "Screenshots (module 1.1)" (galerij desktop+mobiel,
  klik = full-page, DOM-link) en automatische audit-cards.
- Rolverdeling fanout: feature-agents schrijven alléén `audits\<key>.py`
  (+ `_wiring\<key>.md`); Sage = finalizer, integreert wiring-verzoeken atomair.

## v2.2.0 — 2026-07-02 (Sage) — CONSOLIDATIE (plan "beste websites", fase 0.2)
- PRO-modules + 100%-capability uit `agent1\scraper-verbetering` samengevoegd
  in deze canonieke map: `seo_scraper_v2.py` (gewired), `scoring.py`,
  `report.py`, `advanced_analysis.py`, `robust.py`, `extractors\` (5 modules),
  `_verify_report.py` (Playwright-check voor report.html).
- Naambotsing bewust opgelost gehouden: `seo_health_score` = samengestelde
  audit-score (scoring.py); `seo_score` = Lighthouse/PSI-categoriescore
  (performance_ecom.py). Twee keys, geen overschrijving (gedocumenteerd in README).
- PSI blijft fail-soft: zonder `--psi` geen calls; met `--psi` zonder key/quota
  → `psi_note="psi_error: ..."`, run crasht nooit (PAGESPEED_API_KEY volgt in fase 0.3).
- `__version__ = "2.2.0"` + `--version`-flag; docstring bijgewerkt.
- `voorbeeld-output\` vervangen door de PRO-acceptance-run van 30-06
  (4 sites incl. `report.html`); oude voorbeeld-output zit in de backup.
- Backup vooraf: `C:\ClaudeAgents\_backups\seo-scraper-v2-pre-consolidatie-2026-07-02.zip`.
- **Afstemming met Janus' parallelle v2_1-lijn** (`shared\from-agent8\seo-scraper-v2.1`,
  2026-06-15, 8 fixes — brein-notitie eiste afstemming bij samenvoegen):
  - GEPORT (bewezen meetgat): sitemap-verzameling merget nu ÁLLE aangekondigde
    sitemaps (robots.txt-regels + /sitemap.xml, gededupliceerd) én volgt een
    sitemap-index over max 10 sub-sitemaps; .xml.gz wordt gedecomprimeerd.
    Bewijs: movevolt.nl 177 URLs (alleen sitemap-de.xml) → 531 URLs (nl+en+de),
    crawl-seed weer NL. Cap max(2000, max_pages*5), afkap wordt geprint (geen
    stille caps — operator-regel). Zekermobiel onveranderd 139 (geen regressie).
  - GEPORT: robots.txt `Crawl-delay` wordt gerespecteerd (max met --delay,
    gecapt op 10s met log-melding).
  - AL GEDEKT in PRO: retry+backoff op 429/503 mét Retry-After
    (robust.make_session: respect_retry_after_header=True).
  - BEWUST NIET geport: og:image→social/-download, url-hash in bestandsnamen,
    render_ms apart, noindex-teller — cosmetisch of al beter gedekt door de
    157-velden-laag (indexability+reden in technical_schema).
- **Vanaf nu is deze map de enige plek om door te bouwen**;
  `agent1\scraper-verbetering` én `from-agent8\seo-scraper-v2.1` zijn
  bevroren historie (agent1-map bevat .bak's van 30-06).

## v2.1 — 2026-06-30 (Atlas, in agent1\scraper-verbetering)
- 100%-capability: 32 → 157 velden per pagina via 5 extractor-modules
  (head_content, links_images, technical_schema, performance_ecom, overig_geo);
  capability-meetlat 152/152, geen regressie op bestaande velden.
- PRO-laag: SEO-score 0-100 + fixlijst (`scoring.py`), HTML-dashboard
  (`report.py` → report.html), near-dup/orphans/PageRank/--compare
  (`advanced_analysis.py`), robuustheid+run.log (`robust.py`),
  `--fast`/`--concurrency` (wave-based threadpool), `--psi`/`--psi-key`.
- Acceptance-run 4 sites: rideparts 91,8(A) · movevolt 93,4(A) ·
  zekermobiel 89,0(B) · coolblue 83,8(B); 0 crashes, Playwright 0 console-errors.
- Details: `SUMMARY-VERBETERD.md`.

## v2.0 — 2026-06-11 (Sage)
- Basis "alles-scraper" op Echo's v1: keywords (n-grams+densiteit+dekking),
  producten (JSON-LD+microdata), volledige content, anchorteksten,
  Playwright-fallback bij botblokkades, COMPARE.md + ANALYSE.md.
