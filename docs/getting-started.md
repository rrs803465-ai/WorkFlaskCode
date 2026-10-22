# Getting started

## 1. Install

```bash
pip install -r requirements.txt
python3 workflaskcode.py doctor
```

`doctor` reports Python version, dependencies, git, optional analyzers (ruff, bandit, semgrep, pip-audit), your agent home, configuration and terminal capabilities, with a fix hint for each warning.

## 2. Set up a provider

Run `python3 workflaskcode.py` (the wizard starts on first run) or `@setup` later.

1. Choose OpenRouter, an OpenAI-compatible endpoint, Anthropic, or a custom base URL.
2. Provide the key. Preferred: an environment variable such as `OPENROUTER_API_KEY`. Otherwise the OS keyring (needs the `keyring` package). A 0600 file under the agent home is used only if you consent.
3. Pick a model from the live list. Filter with `@models --free`, `--tools`, `--vision`, `--min-context 64000`, or search by name.
4. Test it with `@testmodel` (it sends a tiny request and, if you ask, probes tool calling).

Keys never go into `config.toml`, project metadata, logs, or exports.

## 3. Open a project

Launch from the project folder, pass `--project DIR`, or use `@open`. Your project stays yours: the agent's database, backups, logs and reports live in the agent home (`@where` shows it), and a small `.workflaskcode/` folder (or a folder in the agent home, your choice during setup) holds task state and `PROJECT.md`.

## 4. Work

- Type a request to start the coding agent. Progress shows in the status line and `@tasks`.
- `@btw <question>` asks something without interrupting; the side agent is read-only.
- `Ctrl+C` cancels the running task safely; progress is saved. `Ctrl+C` twice or `@exit` leaves.
- `@resume` continues unfinished work after a crash, a quit, or a model switch.

## 5. Check the work

`@test`, `@diff`, `@audit`, `@review`. `@rollback` reverts only the agent's own edits.
