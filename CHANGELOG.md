# Changelog

## 0.3.2

- Fixed minor issues and inconveniences regarding paths and codeplaces. Future update will push a better version of the token saver. 

## 0.3.0

- Token saver: output normalisation, stale-read stubs, shortened old output, dropped bulky edit arguments, web-tool gating, web cache; `@tokens` and per-task savings line.
- New read-only tools `repo_map` and `read_symbol` for cheap code navigation.
- Update checker on startup and `@update` (check, install with backup and rollback, rollback) for the GitHub repository.
- Web check results of `browser_check` gain `actions`, title/description/accessible-name checks.

## 0.2.0

- Agent web tools (`web_search`, `web_fetch`, `browser_check`, `start_preview`, `stop_preview`) powered by Playwright; the agent researches before building websites and verifies them in desktop and mobile Chromium. No new command: it is driven by the prompt.
- SSRF guard with per-request and per-redirect checks, untrusted-content wrapping, network approval flow and optional Chromium install with consent.

## 0.1.0

First release.

- Providers: OpenRouter, OpenAI-compatible, Anthropic native, custom; live model discovery; credentials via env, keyring or consented file.
- Four permission modes, workspace guard, write journal with undo and checkpoint rollback.
- Task ledger with validation-gated completion, handoff files, crash-safe resume.
- `@chat`, `@codingagent`, concurrent read-only `@btw`; model switching and fallback chain.
- 73 `@commands`, evidence-based audit, dead-code and impact analysis, design lock, templates, optional browser checks, plugins and skills.
