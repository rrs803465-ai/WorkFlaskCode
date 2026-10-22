# Tasks, checkpoints and recovery

The task ledger is a SQLite table per session. Statuses: pending, in progress, completed, blocked, failed, cancelled, paused.

- The agent records a plan with the `plan` tool; you can add tasks yourself (`@plan add`, `@tasks add`).
- A task becomes **completed** only after a passing validation is recorded (`run_tests`, `verify_task` file checks, `@tasks verify`). A claim of success in text does not count. `@tasks done <n> --force` overrides after confirmation and is recorded as a manual override.
- Failed validation, a tool error, cancellation, a limit, or provider exhaustion pause in-progress work; nothing is dropped.

## Checkpoints and rollback

A checkpoint is taken automatically before each coding task (and manually with `@checkpoint`). `@rollback [id]` previews, asks, and reverts the agent's own edits newest-first using stored backups. Files you changed afterwards are skipped and listed; `--patch` writes a reverse patch instead. Git history and your own changes are never touched.

## Resuming

State is saved on exit, on cancel, on model switch, and after every task: `handoff.md`, `tasks.json`, `state.json` in the project metadata folder, plus the database. `@resume` reloads the ledger and conversation, reconciles tool operations that were interrupted mid-flight (it verifies real file state before repeating anything), and continues at the next unfinished task.
