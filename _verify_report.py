import sys, os
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except Exception as e:
    print("playwright niet beschikbaar:", e); sys.exit(2)
target = Path(sys.argv[1]).resolve().as_uri()
errors=[]; console=[]
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page()
    pg.on("console", lambda m: console.append((m.type,m.text)))
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(target, wait_until="load")
    title=pg.title()
    n_tabs=pg.locator(".tab").count()
    n_cards=pg.locator(".scorecard").count()
    n_gauge=pg.locator(".gauge svg").count()
    has_cmp=pg.locator("table.cmp").count()
    n_issues=pg.locator("table.issues tr").count()
    # click second tab if present
    if n_tabs>1:
        pg.locator(".tab").nth(1).click(); pg.wait_for_timeout(200)
    active=pg.locator(".sitepanel.active").count()
    print("title:",title)
    print("tabs:",n_tabs,"| scorecards:",n_cards,"| gauges:",n_gauge,"| compare-table:",has_cmp,"| issue-rows:",n_issues,"| active-panels-after-tabclick:",active)
    errs=[c for c in console if c[0]=="error"]
    print("console errors:",len(errs), errs[:5])
    print("page errors:",len(errors), errors[:5])
    b.close()
print("RESULT:", "PASS" if not errors and not [c for c in console if c[0]=='error'] else "FAIL")
