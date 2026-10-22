# Security model

## What is protected

- **Credentials**: environment variables, OS keyring, or a 0600 file you consented to. Never written to `config.toml`, project metadata, logs, session exports, reports or git. Known secrets and common key formats are redacted from everything persisted or printed by the agent.
- **Workspace**: every tool path is resolved with `realpath`. Traversal (`../`), symlinks that leave the project, NUL bytes, the agent home, and `.git` internals (for writes) are refused. Extra roots need explicit authorisation (`@workspace add`).
- **Writes**: the agent refuses to overwrite files you modified since it last read them. Every edit is journaled with a backup; `@undo` and `@rollback` revert only the agent's own changes and skip files you changed afterwards.
- **Commands**: argument lists by default; shell strings are classified (read, project, modify, network, destructive, forbidden) and nested shells, `curl | sh`, `sudo`, disk and credential tools are refused.
- **Git**: no automatic push, force-push, branch deletion or history rewriting. `@release` creates only a local tag. `@pr --create` requires an already-pushed branch and your confirmation.
- **Model text** is never passed to `eval`/`exec` or a shell string without classification.
- **No telemetry.** The only outbound traffic is to your configured provider (plus package registries if you ask for `@deps --outdated`).

## Permission modes

| Mode | File writes | Commands |
|---|---|---|
| `read_only` | blocked | blocked |
| `approve_all` | ask each time | ask each time |
| `approve_risky` (default) | allowed in project | safe reads run; risky ones ask |
| `restricted_autonomous` | allowed in project | allow-listed project-local commands run unattended; destructive and network always ask |

Change with `@permissions <mode> [--save]`; inspect a command with `@permissions check <command>`.

## What this is not

**This is an approval policy, not a sandbox.** Approved commands run with your user's privileges and can do anything you can. The classifier is pattern-based and can be wrong. Plugins run in-process with your privileges; consent and permission declarations limit what they may register, not what their Python code can do. If you need isolation, run WorkFlaskCode inside a container or VM.

Reporting vulnerabilities: see [SECURITY.md](../SECURITY.md).
