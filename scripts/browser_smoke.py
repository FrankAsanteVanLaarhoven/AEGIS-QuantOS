#!/usr/bin/env python3
"""Real Chromium integration checks. Start scripts/run.py first.
AEGIS_TEST_TOKEN must match the launcher token; no real account is used.
"""
from pathlib import Path
import json, os, sys, time, re
import httpx
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];out=ROOT/'evidence';out.mkdir(exist_ok=True)
base=os.environ.get('AEGIS_TEST_URL','http://127.0.0.1:8765');token=os.environ.get('AEGIS_TEST_TOKEN','')
if not token:raise SystemExit('Set AEGIS_TEST_TOKEN to the running local token')
errors=[];checks=[]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1500,'height':1080},device_scale_factor=1)
    page.set_default_timeout(10000)
    page.on('pageerror',lambda exc:(errors.append(str(exc)),print('PAGEERROR',exc,flush=True)))
    if os.environ.get('AEGIS_BROWSER_BRIDGE')=='1':
        # Managed Chromium in this environment disallows URL navigation. Render
        # real application files and forward fetch through a local HTTP client.
        # This does not exercise browser-native cookie or SSE transport.
        http=httpx.Client(base_url=base,timeout=40)
        def bridge(source,payload):
            response=http.request(payload.get('method','GET'),payload['url'],headers=payload.get('headers',{}),content=payload.get('body'))
            return {'status':response.status_code,'body':response.text,'headers':dict(response.headers)}
        page.expose_binding('aegisTestRequest',bridge)
        html=(ROOT/'web'/'index.html').read_text()
        html=re.sub(r'<link[^>]*>','',html);html=re.sub(r'<script[^>]*>.*?</script>','',html)
        page.set_content(html)
        page.add_style_tag(path=str(ROOT/'web'/'style.css'))
        page.evaluate("""() => {
          window.fetch=async (url,options={})=>{ const r=await window.aegisTestRequest({url:String(url),...options});return new Response(r.body,{status:r.status,headers:r.headers}); };
          window.EventSource=class {constructor(){setTimeout(()=>this.onopen?.(),20)}addEventListener(){}close(){}};
        }""")
        page.add_script_tag(path=str(ROOT/'web'/'app.js'))
    else:page.goto(base)
    page.locator('#access-token').fill(token);page.locator('#login-form button').click();page.locator('#login.hidden').wait_for(state='attached');page.locator('#app h1').wait_for();checks.append('Authenticated workspace renders')
    page.screenshot(path=str(out/'ui_overview_initial.png'),full_page=True)
    print('LOGIN OK',flush=True)
    for route in ['markets','research','kronos','simulation','portfolio','execution','risk','evidence','integrations','overview']:
        print('VIEW',route,flush=True);page.locator(f'nav a[data-view="{route}"]').click();page.wait_for_function('(r)=>document.querySelector("#nav a.active")?.dataset.view===r',arg=route);page.wait_for_timeout(300)
        assert page.locator('#app h1').count()==1;checks.append('View renders: '+route)
    page.locator('[data-action="cycle"]').first.click();page.wait_for_timeout(600);page.locator('[data-action="step"]').first.click();page.wait_for_timeout(700)
    page.locator('nav a[data-view="execution"]').click();page.wait_for_timeout(300)
    prior_count=page.locator('tbody tr').count();page.locator('#order-form [name="qty"]').fill('2');page.locator('#order-form button[type="submit"]').click();page.wait_for_timeout(500);assert page.locator('tbody tr').count()==prior_count+1;page.locator('[data-action="step"]').click();page.wait_for_timeout(600)
    assert page.locator('tbody').inner_text().find('FILLED')>=0;checks.append('Paper order queued and filled through UI')
    page.locator('nav a[data-view="risk"]').click();page.wait_for_timeout(300);page.locator('[data-action="halt"]').click();page.wait_for_timeout(400);assert page.locator('[data-action="halt"]').inner_text()=='Resume risk gate';page.locator('[data-action="halt"]').click();page.wait_for_timeout(400);checks.append('Halt and resume through UI')
    page.locator('nav a[data-view="simulation"]').click();page.wait_for_timeout(300);page.locator('#simulation-form button').click();page.locator('#simulation-chart').wait_for(timeout=30000);page.screenshot(path=str(out/'ui_time_machine.png'),full_page=True);checks.append('Bootstrap experiment renders computed envelope')
    page.locator('nav a[data-view="research"]').click();page.wait_for_timeout(300);page.locator('#backtest-form button').click();page.locator('#backtest-chart').wait_for(timeout=30000);page.screenshot(path=str(out/'ui_research.png'),full_page=True);checks.append('Backtest results and equity chart render');page.wait_for_timeout(600)
    page.locator('#dcf-form button').click();page.wait_for_timeout(500);assert 'Estimated per share' in page.locator('#dcf-result').inner_text();checks.append('DCF calculator updates numerical output')
    page.locator('nav a[data-view="kronos"]').click();page.wait_for_timeout(300);page.locator('#forecast-form button').click();page.wait_for_timeout(600);assert 'install_kronos' in page.locator('#toast').inner_text();checks.append('Unavailable model returns explicit failure, no mock output');page.screenshot(path=str(out/'ui_kronos.png'),full_page=True)
    page.locator('#command').click();page.locator('#command-input').fill('evidence');page.locator('[data-command="evidence"]').click();page.wait_for_timeout(300);assert page.locator('nav a.active').get_attribute('data-view')=='evidence';checks.append('Keyboard-command navigation works');page.screenshot(path=str(out/'ui_evidence.png'),full_page=True)
    page.locator('nav a[data-view="overview"]').click();page.wait_for_timeout(500);page.evaluate("document.querySelector('#toast').className=''");page.wait_for_timeout(400);page.screenshot(path=str(out/'ui_overview.png'),full_page=True)
    # Mobile state must not overflow the viewport (table internals may scroll).
    page.set_viewport_size({'width':430,'height':932});page.wait_for_timeout(800);page.screenshot(path=str(out/'ui_mobile.png'),full_page=True)
    width=page.evaluate('({scroll:document.documentElement.scrollWidth,inner:innerWidth})');assert width['scroll']<=width['inner']+1,width;checks.append('430px mobile layout: no page overflow')
    browser.close()
report={'checks':checks,'passed':len(checks),'page_errors':errors,'browser':'Chromium / Playwright','transport':'HTTP bridge (native navigation restricted)' if os.environ.get('AEGIS_BROWSER_BRIDGE')=='1' else 'Native browser HTTP','screenshots':'evidence/ui_*.png','scope':'Local synthetic paper workflow; external provider and pretrained model execution not tested'}
(out/'browser_smoke.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
assert not errors,errors
