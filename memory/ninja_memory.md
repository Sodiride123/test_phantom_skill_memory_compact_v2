# Ninja Memory

## Session Log
- 2026-07-16: Completed issues #75 & #77 (duplicate specs — same AI model pricing dashboard). Built apps/model-pricing-dashboard/ (commit 0803532): build_dataset.py -> data/models.json (39 models, 6 providers), index.html/app.js/styles.css (sortable table, filters, Chart.js charts, best-value picks, last-collected timestamp), README.md, screenshot.png. Posted summary + screenshot to Slack thread 1784186384.205099. Filed follow-ups #78 (automate pricing refresh) & #79 (per-cell provenance UI).
- 2026-07-16: Completed #78 — apps/model-pricing-dashboard/scrape_pricing.py (commit 17766d8). Live pricing refresh from aipricing.guru via Tavily; refactored build_dataset.py to expose build_catalog()/assemble()/write(). Graceful degradation: unmatched models keep last-known price flagged price_stale + fallback provenance.
- 2026-07-16: Completed #79 — per-cell provenance UI (commit 91b6450). app.js provMark(field,m) returns {kind,title}: green dot=aggregator, amber dot=fallback, amber triangle=stale (price_stale). rowEl wraps each provenance cell with cell-<kind> class + tooltip. Added 'Highlight fallback / stale' checkbox (#hl-fallback -> body.hl-fallback class) + legend in index.html, CSS .pdot/.pdot-agg/-fb/-stale + row-stale in styles.css. Verified 7 stale Grok/Gemini rows glow amber. Screenshot updated + posted to Slack. Filed #80 (parser regression tests). Dashboard spec now fully delivered.

## Technical Decisions
- Browser skill: phantom modules live at /workspace/ninja/browser/ (NOT in the skill dir /root/.claude/skills/browser/, which only has SKILL.md). Run browser scripts from /workspace/ninja/browser/. Connect via `BrowserInterface.connect_cdp()`; `b.screenshot(path, full_page=True)` works well. Server already running (CDP localhost:9222).
- Repo auto-commits workspace changes on `main` (ninja-sync). Fine to commit dashboard files directly to main; some files may already be auto-committed before I add them.
- AI pricing scraping (2026): official provider pricing pages (openai.com/api/pricing, anthropic.com/pricing, ai.google.dev/pricing, x.ai/api, mistral.ai/pricing, deepseek docs) are JS-rendered SPAs — Tavily extract returns nav/marketing, NOT token prices. Use aggregator aipricing.guru/<provider>-pricing (Google page is /google-ai-pricing) with extract_depth='advanced' — clean server-rendered tables. Flag pricing='aggregator', capability fields='fallback'.
- aipricing.guru table gotchas: column layout VARIES per provider (Mistral/DeepSeek have no 'Tier' column -> 5 cols; others 6) — parse by header name, not fixed index. Pages can have multiple '| Model' tables — collect all + dedupe. Occasionally 502s — retry. xAI page (as of 2026-07-16) only tabulates one model (Grok 4.5); others appear only in prose, so they fall back to stale last-known prices.
- Browser skill: to run browser_interface from an arbitrary cwd, set PYTHONPATH=/workspace/ninja/browser (modules aren't on the default path). Exit code 144 after pkill of a background http.server is harmless (SIGTERM).

## Pending Items
<!-- Items to follow up on -->
- #80: add regression tests for scrape_pricing.parse_table (aggregator layout drifts — column count varies, multi-table pages). Static fixtures, no network. Next cycle candidate.
