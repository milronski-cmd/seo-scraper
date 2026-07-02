# SEO Scraper — complete website-audit vanaf de command line

Volledige site-audit in één run: **SEO** (meta, headings, schema, links, sitemaps), **conversie (CRO-score)**,
**beeld-audit** (kadervulling, achtergronden, gewicht, alt-teksten), **icon-audit**, **interne-link-optimizer**
(PageRank, orphans, linkplan), **doelgroep-lens** en **contrast/leesbaarheid (WCAG)** — met een score 0-100,
een concrete fixlijst en een zelfstandig `report.html`. Vergelijk meerdere sites met `--compare` en zie wie
per signaal wint.

## Installatie

```bash
git clone https://github.com/milronski-cmd/seo-scraper.git
cd seo-scraper
pip install -r requirements.txt
playwright install chromium   # optioneel maar aanbevolen (JS-sites, screenshots, render-audits)
```

Python 3.10+ aanbevolen. Alle modules zijn fail-soft: ontbreekt een optionele dependency (Playwright, Pillow),
dan draait de rest van de audit gewoon door.

## Gebruik

```bash
# Volledige audit van een site (max 30 pagina's)
python seo_scraper_v2.py https://voorbeeld.nl --max-pages 30 --out output/voorbeeld

# Met screenshots + render-audits (desktop 1440 + mobiel 390, above-fold + full-page)
python seo_scraper_v2.py https://voorbeeld.nl --max-pages 30 --out output/voorbeeld --screenshots

# Twee of meer sites vergelijken (winnaar per signaal)
python seo_scraper_v2.py https://site-a.nl https://site-b.nl --compare --out output/vergelijk

# Sneller crawlen
python seo_scraper_v2.py https://voorbeeld.nl --max-pages 50 --fast --concurrency 4 --out output/run
```

Open daarna `output/<map>/report.html` in je browser: score, deelscores per audit, fixlijst met prioriteiten
en (met `--screenshots`) de beelden per pagina.

## Core Web Vitals (optioneel)

Voor echte veldmetingen via Google PageSpeed Insights: zet een (gratis) API-key als omgevingsvariabele
`PAGESPEED_API_KEY` en draai met `--psi`. Zonder key blijft de audit volledig werken; de CWV-sectie blijft dan leeg.

## Wat zit erin

| Module | Meet |
| --- | --- |
| SEO-kern | titles/meta/headings/canonical/hreflang, schema.org, sitemaps, robots, interne/externe links |
| CRO-audit | CTA boven de vouw, boodschappen per viewport, trust-plaatsing, checkout-frictie, prijs-zichtbaarheid |
| Beeld-audit | kadervulling per productfoto, achtergrond vs kaartkleur, formaat/gewicht/alt, duplicaten |
| Icon-audit | emoji-als-icoon, gemixte icon-sets, stroke-consistentie |
| Links-optimizer | PageRank-verdeling, orphans, anchor-advies, silo-structuur, breadcrumbs |
| Doelgroep-lens | configureerbare checklist (bijv. leesbaarheid/fontgroottes) die meeweegt in de CRO-score |
| Contrast-audit | WCAG-contrast per tekst-element, minimale fontgroottes, tekst-over-foto |

Handige extra's: `_compare_from_runs.py` (vergelijk bestaande runs zonder opnieuw te scrapen) en
`_verify_report.py` (rendert het rapport headless en checkt op fouten).

## Netjes scrapen

De crawler respecteert `robots.txt`, gebruikt een bescheiden delay en een duidelijke user-agent.
Gebruik de tool alleen op sites waarvoor je toestemming hebt of die publiek toegankelijk zijn, en houd
`--max-pages` redelijk.

## Licentie

MIT — gebruik vrij, op eigen risico, zonder garantie.
