# Ninja Memory

## Session Log
- 2026-07-16: Completed issues #75 & #77 (duplicate specs — same AI model pricing dashboard). Built apps/model-pricing-dashboard/ (commit 0803532): build_dataset.py -> data/models.json (39 models, 6 providers), index.html/app.js/styles.css (sortable table, filters, Chart.js charts, best-value picks, last-collected timestamp), README.md, screenshot.png. Posted summary + screenshot to Slack thread 1784186384.205099. Filed follow-ups #78 (automate pricing refresh) & #79 (per-cell provenance UI).

## Technical Decisions
- Browser skill: phantom modules live at /workspace/ninja/browser/ (NOT in the skill dir /root/.claude/skills/browser/, which only has SKILL.md). Run browser scripts from /workspace/ninja/browser/. Connect via `BrowserInterface.connect_cdp()`; `b.screenshot(path, full_page=True)` works well. Server already running (CDP localhost:9222).
- Repo auto-commits workspace changes on `main` (ninja-sync). Fine to commit dashboard files directly to main; some files may already be auto-committed before I add them.
- AI pricing scraping (2026): official provider pricing pages (openai.com/api/pricing, anthropic.com/pricing, ai.google.dev/pricing, x.ai/api, mistral.ai/pricing, deepseek docs) are JS-rendered SPAs — Tavily extract returns nav/marketing, NOT token prices. Use aggregator aipricing.guru/<provider>-pricing (Google page is /google-ai-pricing) with extract_depth='advanced' — clean server-rendered tables. Flag pricing='aggregator', capability fields='fallback'.

## Pending Items
<!-- Items to follow up on -->
- #78: automate pricing refresh (scraper -> data/models.json) for the dashboard.
- #79: surface per-field provenance per-cell in the dashboard UI.
