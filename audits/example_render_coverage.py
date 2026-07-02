# -*- coding: utf-8 -*-
"""
REFERENTIE-AUDIT — bewijst het module-contract end-to-end en dient als
kopieer-voorbeeld voor modules 1.2-1.7 (INTEGRATION.md heeft het volledige
contract). Meet iets kleins maar nuttigs: hebben de gecrawlde pagina's een
gerenderde momentopname (screenshots + dom.html uit module 1.1)? Zonder die
render draaien de visuele audits (CRO/beeld/contrast) op de kale HTML en is
de meting minder betrouwbaar.
"""
KEY = "render_coverage"
LABEL = "Render-dekking (screenshots + DOM)"
ORDER = 10


def audit(ctx):
    pages = ctx.get("pages") or []
    n = len(pages)
    shots = [p for p in pages if (p.get("screenshots") or {}).get("ok")]
    doms = [p for p in pages if (p.get("screenshots") or {}).get("dom")]
    missing = [p.get("url", "?") for p in pages if not (p.get("screenshots") or {}).get("ok")]

    issues = []
    if n and not shots:
        issues.append({
            "severity": "Medium", "category": "render",
            "title": "Geen screenshots/DOM-snapshots in deze run",
            "why": ("Visuele audits (CRO 1.2, beeld 1.3, contrast 1.7) meten dan op de "
                    "kale requests-HTML i.p.v. de échte render — JS-gerenderde kaarten "
                    "en lazy-geladen beelden worden gemist."),
            "fix": "Draai met --screenshots (fase-2-nulmeting: verplicht aan).",
            "url": "",
        })
    elif missing:
        issues.append({
            "severity": "Low", "category": "render",
            "title": f"{len(missing)} pagina('s) zonder complete screenshots",
            "why": "Die pagina's ontbreken straks in de visuele schouw-galerij.",
            "fix": "Zie screenshots/manifest.json -> failed[] voor de foutnotities.",
            "url": missing[0],
        })

    score = round(100.0 * len(shots) / n, 1) if (n and shots) else (None if not n else None)
    return {
        "score": score,                      # None = niet-toepasselijk (geen shots gevraagd)
        "summary": (f"{len(shots)}/{n} pagina's met desktop+mobiel-shots, "
                    f"{len(doms)}/{n} met gerenderde DOM."),
        "issues": issues,
        "data": {"pages": n, "with_shots": len(shots), "with_dom": len(doms),
                 "missing": missing[:10]},
    }
