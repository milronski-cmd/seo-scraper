# -*- coding: utf-8 -*-
"""
AUDITS-REGISTRY (fase 1, plan §4) — plugin-mechanisme voor modules 1.2-1.7.

Contract (volledig: INTEGRATION.md in de scraper-root):
  - Eén module = één bestand `audits/<key>.py` met minimaal:
        KEY   = "cro"                 # unieke korte sleutel (= bestandsnaam)
        LABEL = "Conversie-audit (CRO)"
        ORDER = 20                    # volgorde in report.html (laag = boven)
        def audit(ctx) -> dict        # zie INTEGRATION.md voor ctx + resultaat
  - audit() MOET fail-soft zijn; de runner vangt bovendien elke exception af
    (een kapotte audit mag de run nooit breken).
  - Bestanden met een `_`-prefix worden overgeslagen (werk-in-uitvoering).

Nieuwe module toevoegen = alleen het bestand droppen. Geen registratieregel,
geen wijziging aan seo_scraper_v2.py of report.py nodig.
"""
import importlib
import pkgutil


def discover():
    """Vind alle audit-modules, gesorteerd op (ORDER, KEY). Fail-soft:
    een module die niet importeert wordt overgeslagen (met melding),
    zodat één kapot bestand nooit de hele run of andere audits blokkeert."""
    mods = []
    for info in pkgutil.iter_modules(__path__):
        name = info.name
        if name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except Exception as e:
            print(f"  [audits] module '{name}' importeert niet, overgeslagen: {e}")
            continue
        if not callable(getattr(mod, "audit", None)):
            print(f"  [audits] module '{name}' mist audit(ctx), overgeslagen")
            continue
        key = getattr(mod, "KEY", name)
        order = getattr(mod, "ORDER", 500)
        mods.append((order, str(key), mod))
    mods.sort(key=lambda t: (t[0], t[1]))
    return [(key, mod) for _, key, mod in mods]
