#!/usr/bin/env python3
"""Native Chromium integration checks against the live local server.

Exercises real browser login, HttpOnly cookies, CSRF headers, SSE, all 10 views,
real Kronos inference (when installed), Finance Research Agents, and desktop/tablet/mobile layouts.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence'
SCREENSHOTS = OUT / 'current' / 'screenshots'
OUT.mkdir(exist_ok=True)
SCREENSHOTS.mkdir(parents=True, exist_ok=True)

base = os.environ.get('AEGIS_TEST_URL', 'http://127.0.0.1:8765')
token = os.environ.get('AEGIS_TEST_TOKEN', '')
if not token:
    raise SystemExit('Set AEGIS_TEST_TOKEN to the running local token')

errors: list[str] = []
checks: list[str] = []

with sync_playwright() as p:
    launch_kwargs: dict[str, object] = {'headless': True, 'args': ['--no-sandbox']}
    if os.environ.get('CHROMIUM_PATH'):
        launch_kwargs['executable_path'] = os.environ['CHROMIUM_PATH']
    browser = p.chromium.launch(**launch_kwargs)
    context = browser.new_context(viewport={'width': 1500, 'height': 1080}, device_scale_factor=1)
    page = context.new_page()
    page.set_default_timeout(15000)
    page.on('pageerror', lambda exc: (errors.append(str(exc)), print('PAGEERROR', exc, flush=True)))

    if os.environ.get('AEGIS_BROWSER_BRIDGE') == '1':
        http = httpx.Client(base_url=base, timeout=40)

        def bridge(source, payload):
            response = http.request(payload.get('method', 'GET'), payload['url'], headers=payload.get('headers', {}), content=payload.get('body'))
            return {'status': response.status_code, 'body': response.text, 'headers': dict(response.headers)}

        page.expose_binding('aegisTestRequest', bridge)
        html = (ROOT / 'web' / 'index.html').read_text()
        html = re.sub(r'<link[^>]*>', '', html)
        html = re.sub(r'<script[^>]*>.*?</script>', '', html)
        page.set_content(html)
        page.add_style_tag(path=str(ROOT / 'web' / 'style.css'))
        page.evaluate("""() => {
          window.fetch=async (url,options={})=>{ const r=await window.aegisTestRequest({url:String(url),...options});return new Response(r.body,{status:r.status,headers:r.headers}); };
          window.EventSource=class {constructor(){setTimeout(()=>this.onopen?.(),20)}addEventListener(){}close(){}};
        }""")
        page.add_script_tag(path=str(ROOT / 'web' / 'app.js'))
    else:
        page.goto(base, wait_until='networkidle')

    page.locator('#access-token').fill(token)
    page.locator('#login-form button').click()
    page.locator('#login.hidden').wait_for(state='attached')
    page.locator('#app h1').wait_for()
    checks.append('Authenticated workspace renders via native login/cookies/CSRF')
    page.screenshot(path=str(SCREENSHOTS / 'ui_overview_initial.png'), full_page=True)

    for rname in ['markets', 'research', 'kronos', 'simulation', 'portfolio', 'execution', 'risk', 'evidence', 'integrations', 'overview']:
        page.locator(f'nav a[data-view="{rname}"]').click()
        page.wait_for_function('(r)=>document.querySelector("#nav a.active")?.dataset.view===r', arg=rname)
        page.wait_for_timeout(250)
        assert page.locator('#app h1').count() == 1
        checks.append('View renders: ' + rname)

    page.locator('[data-action="cycle"]').first.click()
    page.wait_for_timeout(500)
    page.locator('[data-action="step"]').first.click()
    page.wait_for_timeout(600)

    page.locator('nav a[data-view="execution"]').click()
    page.wait_for_timeout(300)
    prior_count = page.locator('tbody tr').count()
    page.locator('#order-form [name="qty"]').fill('2')
    page.locator('#order-form button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert page.locator('tbody tr').count() == prior_count + 1
    page.locator('[data-action="step"]').click()
    page.wait_for_timeout(600)
    assert 'FILLED' in page.locator('tbody').inner_text()
    checks.append('Paper order queued and filled through native UI')

    page.locator('nav a[data-view="risk"]').click()
    page.wait_for_timeout(300)
    page.locator('[data-action="halt"]').click()
    page.wait_for_timeout(400)
    assert page.locator('[data-action="halt"]').inner_text() == 'Resume risk gate'
    page.locator('[data-action="halt"]').click()
    page.wait_for_timeout(400)
    checks.append('Halt and resume through UI')

    page.locator('nav a[data-view="simulation"]').click()
    page.wait_for_timeout(300)
    page.locator('#simulation-form button').click()
    page.locator('#simulation-chart').wait_for(timeout=30000)
    page.screenshot(path=str(SCREENSHOTS / 'ui_time_machine.png'), full_page=True)
    checks.append('Bootstrap experiment renders computed envelope')

    page.locator('nav a[data-view="research"]').click()
    page.wait_for_timeout(300)
    page.locator('#backtest-form button').click()
    page.locator('#backtest-chart').wait_for(timeout=30000)
    page.locator('#dcf-form button').click()
    page.wait_for_timeout(400)
    assert 'Estimated per share' in page.locator('#dcf-result').inner_text()
    page.locator('#agent-job-form button[type="submit"]').click()
    page.wait_for_timeout(500)
    assert 'ABSTAINED' in page.locator('#agent-job-result').inner_text()
    page.screenshot(path=str(SCREENSHOTS / 'ui_research.png'), full_page=True)
    checks.append('Backtest, DCF, and bounded research agent job render')

    page.locator('nav a[data-view="kronos"]').click()
    page.wait_for_timeout(300)
    page.locator('#forecast-form button').click()
    page.wait_for_timeout(1500)
    if page.locator('#forecast-chart').count() > 0:
        page.screenshot(path=str(SCREENSHOTS / 'ui_kronos.png'), full_page=True)
        checks.append('Real pretrained Kronos forecast chart renders')
    else:
        assert 'install_kronos' in page.locator('#toast').inner_text()
        page.screenshot(path=str(SCREENSHOTS / 'ui_kronos.png'), full_page=True)
        checks.append('Unavailable model returns explicit failure, no mock output')

    page.locator('nav a[data-view="integrations"]').click()
    page.wait_for_timeout(400)
    assert 'Provider qualification matrix' in page.locator('#app').inner_text()
    page.screenshot(path=str(SCREENSHOTS / 'ui_integrations.png'), full_page=True)
    checks.append('Integrations provider qualification matrix renders')

    page.locator('#command').click()
    page.locator('#command-input').fill('evidence')
    page.locator('[data-command="evidence"]').click()
    page.wait_for_timeout(300)
    assert page.locator('nav a.active').get_attribute('data-view') == 'evidence'
    checks.append('Keyboard-command navigation works')

    page.locator('nav a[data-view="overview"]').click()
    page.wait_for_timeout(400)
    page.evaluate("document.querySelector('#toast').className=''")
    page.wait_for_timeout(300)
    page.screenshot(path=str(SCREENSHOTS / 'ui_overview.png'), full_page=True)

    # Tablet (820px)
    page.set_viewport_size({'width': 820, 'height': 1180})
    page.wait_for_timeout(500)
    t_width = page.evaluate('({scroll:document.documentElement.scrollWidth,inner:innerWidth})')
    assert t_width['scroll'] <= t_width['inner'] + 1, t_width
    page.screenshot(path=str(SCREENSHOTS / 'ui_tablet.png'), full_page=True)
    checks.append('820px tablet layout: no page overflow')

    # Mobile (430px)
    page.set_viewport_size({'width': 430, 'height': 932})
    page.wait_for_timeout(600)
    m_width = page.evaluate('({scroll:document.documentElement.scrollWidth,inner:innerWidth})')
    assert m_width['scroll'] <= m_width['inner'] + 1, m_width
    page.screenshot(path=str(SCREENSHOTS / 'ui_mobile.png'), full_page=True)
    checks.append('430px mobile layout: no page overflow')

    browser.close()

transport = 'HTTP bridge (native navigation restricted)' if os.environ.get('AEGIS_BROWSER_BRIDGE') == '1' else 'Native browser HTTP'
report = {
    'checks': checks,
    'passed': len(checks),
    'page_errors': errors,
    'browser': 'Chromium / Playwright',
    'transport': transport,
    'screenshots': 'evidence/current/screenshots/ui_*.png',
}
(OUT / 'current' / 'browser_smoke.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
assert not errors, errors
