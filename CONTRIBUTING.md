# Contributing

Thanks for helping. The short version:

1. `pip install -r requirements-dev.txt`
2. `ruff check . && ruff format --check .` must pass.
3. Keep the core dependency-light: stdlib plus `pydantic`. Anything else must be optional with a graceful fallback.
4. No telemetry, no hardcoded model ids, no secrets in logs, config or tests (use obviously fake values such as `sk-fake-123456789`).
5. Never claim a feature works unless a test or a documented manual check shows it. If something is partial, say so in the docs.
6. Code style: no narration comments or docstrings that restate the code; names and structure should carry the meaning. Comment only for a non-obvious constraint.

New `@commands` need a real implementation, an entry in the registry and an updated `docs/commands.md` (`python3 docs/gen_commands.py`).

Security issues: follow [SECURITY.md](SECURITY.md), not the public tracker.
