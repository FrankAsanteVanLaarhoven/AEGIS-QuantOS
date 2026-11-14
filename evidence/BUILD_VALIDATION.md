# Build qualification — AEGIS QuantOS 0.2.0

Date: 14 November 2026. Approved project scope: v5.

## Verified in this build

| Check | Actual result | Evidence |
|---|---|---|
| Automated Python suite | 100 passed; 0 failed; 0 skipped; 13.69 seconds | pytest.txt / pytest.xml |
| Kronos adapter interface checks | Six tests included above, using explicit predictor doubles | tests/test_kronos_contract.py |
| Chromium UI harness | 19 checks passed; no JavaScript page errors | browser_smoke.json |
| Responsive layout | 430-pixel viewport: no page-level overflow | ui_mobile.png |
| Python syntax | compileall completed | source checked during build |
| JavaScript syntax | node --check completed | web/app.js checked during build |
| Independent event verification | Export rehashed successfully | independent_verification.txt |
| Accounting reconciliation | Cash and positions match recorded fills after 60 automated replay bars | paper_workflow.json |

Browser restriction: managed Chromium rejected direct local navigation. The harness rendered the actual HTML/CSS/JS and forwarded fetch calls via an HTTP bridge to the real running FastAPI server. This validates UI interactions against backend responses, but does not validate browser-native cookies, SSE transport or public deployment. Authentication and CSRF were tested independently through the API.

The 12 original M0 tests were separately run as legacy reference checks; they are not included in the 100-test count. The M0 source is not bundled into this release.

## Not established

A complete local Kronos clone, downloaded pretrained weights, real pretrained inference parity, external data-provider access, real broker connection, remote CI, Docker execution, Windows helper execution, GPU performance, financial profitability, calibrated forecasts and state-of-the-art benchmark performance were not established. No real or external paper-broker orders were submitted.

The optional Kronos contract tests skip when model libraries are not installed. The reported 100-test run used those libraries, but no learned model weights.

## Release decision

Suitable for local single-operator research and synthetic/CSV paper-replay evaluation. Not approved for public Internet exposure, multi-tenant enterprise use or real-money execution. Complete the target-host pretrained-model, native-browser, licensed-data and independent security gates before making stronger claims.
