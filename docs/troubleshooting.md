# Troubleshooting

Start with `python3 workflaskcode.py doctor` (add `--online` to contact your provider).

| Symptom | Fix |
|---|---|
| `Missing dependencies` on start | `pip install -r requirements.txt` |
| `no model is configured` | `@setup` or `@changemodel provider:model-id` |
| `no API key for provider` | export the env var named in `@providers`, or `@providers key <name>` |
| Model list empty or fails | `@models refresh`; some endpoints have no list, so use `@changemodel` with an id |
| Tasks stop with "paused: all models exhausted" | wait for the limit window, then `@retry`; or `@fallback add` another provider |
| Approval prompt not visible | type `y`, `a` or `n`; status line shows "approval needed" |
| Colours or boxes look wrong | `--plain` or `@theme plain` |
| Tests not detected | `@test --cmd 'your command'` |
| Logs | `@where` shows the log folder; keys are redacted |
