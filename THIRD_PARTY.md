# Third-party references and dependency boundaries

Kronos source: https://github.com/shiyu-coder/Kronos — MIT, copyright (c) 2025 ShiYu, verified at pinned commit `67b630e67f6a18c9e9be918d9b4337c960db1e9a`. The installer clones the real source and keeps LICENSE and notices. Its upstream paper is https://arxiv.org/abs/2508.02739 . Source and model-weight terms must both be reviewed.

Day Trader Skill: https://github.com/Jakeschincariol/day-trader-skill — used as a functional reference only; its implementation is not copied or vendored here.

Alpaca paper API: https://docs.alpaca.markets/us/docs/paper-trading and https://docs.alpaca.markets/us/docs/authentication . Only authenticated read requests to the fixed paper host are implemented. No live endpoint or external order submission exists.

SEC EDGAR API: https://www.sec.gov/search-filings/edgar-application-programming-interfaces and https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data . Identify automated access with a real User-Agent; observe provider conditions. Current retrieval metadata is not point-in-time historical evidence.

Python dependencies are declared in requirements files. No vendor font files, logos or proprietary product assets are included. The interface assets are original HTML/CSS/JS/SVG; system fonts are used.
