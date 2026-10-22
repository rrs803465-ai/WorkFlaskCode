# Command reference

Generated from the command registry (`python3 docs/gen_commands.py`). Every command has a real implementation; commands that need an optional tool say what to install when it is missing.

Commands start with `@`. Anything else you type is sent to the agent. Flags use `--name value`. `@help <command>` shows usage inside a session, and every command also works as a subcommand: `python3 workflaskcode.py audit --json`.

## General

| Command | What it does | Usage |
|---|---|---|
| `@clear` | clear the visible terminal (keeps all data) (alias: @cls) | `@clear` |
| `@config` | show or change configuration | `@config [show\|get <key>\|set <key> <value>\|path]` |
| `@doctor` | diagnose configuration, dependencies, workspace and providers | `@doctor [--online]` |
| `@exit` | save state and quit (alias: @quit) | `@exit` |
| `@help` | list commands and examples (alias: @?) | `@help [command]` |
| `@history` | search previous sessions | `@history <text> [--all] [--limit N]` |
| `@new` | create a project from a template or description | `@new [list] [description] --name N [--template T] [--path DIR] [--addons docker,ci]` |
| `@open` | switch to another project after validating it | `@open <path\|id\|name> [--new-session]` |
| `@projects` | list registered projects | `@projects [remove <id>]` |
| `@resume` | continue the saved task, or switch to a saved session | `@resume [list\|<session-id>]` |
| `@setup` | reopen the configuration wizard | `@setup` |
| `@status` | current work, tasks, model and usage (alias: @st) | `@status` |
| `@theme` | change the colour theme | `@theme [auto\|dark\|light\|plain]` |
| `@where` | print every relevant absolute path | `@where` |
| `@workspace` | active project and workspace boundaries | `@workspace [add <path>\|remove <path>]` |

## Models and providers

| Command | What it does | Usage |
|---|---|---|
| `@changemodel` | switch model without losing work (alias: @model) | `@changemodel [provider:model-id] [--role coding\|chat] [--force]` |
| `@fallback` | show or edit the fallback model chain | `@fallback [list\|add <ref>\|remove <ref>\|clear]` |
| `@limits` | show or change request, cost and time limits | `@limits [set <name> <value>\|budget <provider:model> <usd\|off>]` |
| `@models` | list the provider's real models (dynamic, filterable) | `@models [refresh\|search <words>] [--free] [--tools] [--vision] [--min-context N] [--sort name\|id\|context\|price\|newest] [--limit N]` |
| `@providers` | list, add, test and manage providers and keys | `@providers [list\|add\|key <n>\|remove <n>\|test <n>\|paid <n> on\|off\|show <n>]` |
| `@retry` | resume after a failure, or show/set the retry policy | `@retry [policy [attempts N] [base S] [max S] [jitter F]]` |
| `@testmodel` | test a model's connectivity, tools and streaming | `@testmodel [provider:model-id]` |
| `@usage` | tokens, requests, cost and provider-reported quota | `@usage [task] [--all]` |

## Planning, safety and recovery

| Command | What it does | Usage |
|---|---|---|
| `@branch` | list, create or switch git branches | `@branch [list\|create <name>\|switch <name>]` |
| `@checkpoint` | save a checkpoint of agent changes and task state | `@checkpoint [label] \| list` |
| `@compact` | compact conversation context deterministically | `@compact` |
| `@diff` | show real uncommitted changes | `@diff [paths] [--staged] [--stat]` |
| `@export` | export the session transcript (secrets redacted) | `@export [path] [--format md\|json]` |
| `@permissions` | inspect or change the permission policy | `@permissions [show\|<mode> [--save]\|allow <glob>\|deny <glob>\|check <command>\|reset]` |
| `@plan` | show the plan, or have the agent plan a goal (read-only) | `@plan [goal] \| add <task> \| clear` |
| `@rollback` | revert the agent's own edits since a checkpoint | `@rollback [checkpoint-id] [--patch] [--yes]` |
| `@tasks` | task ledger: status, validation, manual overrides (alias: @t) | `@tasks [list\|show n\|add text\|start n\|done n [--force]\|verify n\|block n why\|fail n why\|cancel n\|retry n\|next]` |
| `@undo` | undo the agent's last file change(s) safely | `@undo [n] [--yes]` |

## Code intelligence

| Command | What it does | Usage |
|---|---|---|
| `@architecture` | structure, entry points, import graph, routes, tables (alias: @arch) | `@architecture [--explain]` |
| `@audit` | evidence-based project audit | `@audit [category] [--category a,b] [--severity s] [--json\|--md] [--out file] [--no-external] \| show <id> \| fix <id> \| last` |
| `@benchmark` | time a command over several runs and compare with history | `@benchmark --cmd <command> [--runs n] [--warmup n] [--label l] \| history` |
| `@compare` | compare two files or two git revisions | `@compare <a> <b> [--full]` |
| `@deadcode` | find unused imports and unreferenced definitions | `@deadcode [--limit n]` |
| `@debug` | run tests, then have the agent diagnose and fix a real failure | `@debug [problem description] [--cmd ...]` |
| `@deps` | dependency inventory, pinning and health | `@deps [--outdated] [--limit n]` |
| `@docs` | agent writes or updates documentation from the real code | `@docs [target]` |
| `@explain` | explain a file, symbol or the project (read-only) | `@explain [file\|symbol\|question]` |
| `@explain-diff` | explain the current diff or a revision (read-only) | `@explain-diff [rev] [--staged]` |
| `@format` | run the project's formatter | `@format [--check] [--cmd ...]` |
| `@generate-tests` | agent generates and runs tests for a target (alias: @gentests) | `@generate-tests <path\|symbol>` |
| `@impact` | what depends on a file or symbol | `@impact <file\|symbol>` |
| `@refactor` | agent refactors a path without changing behaviour | `@refactor <path> <goal>` |
| `@review` | review the uncommitted diff or a file (read-only) | `@review [path] [--staged]` |
| `@search` | search project files (alias: @grep) | `@search <text> [--regex] [--glob g] [--path p] [--ignore-case] \| files <glob> \| symbol <name>` |
| `@security` | security-focused audit (security, config, dependencies) | `@security [--severity s] [--json] [--out file]` |
| `@test` | run the project's tests and summarise failures | `@test [paths] [--cmd ...] [--keyword expr] [--failed] [--timeout s] [--output]` |
| `@types` | run the project's type checker | `@types [--cmd ...]` |

## Project and release

| Command | What it does | Usage |
|---|---|---|
| `@api-docs` | detect HTTP routes and generate API docs | `@api-docs [--write] [--agent]` |
| `@changelog` | changelog entry from conventional commits | `@changelog [--since tag] [--version v] [--write]` |
| `@migrate` | database migration status, plan, run (confirmed) or create | `@migrate [status\|plan\|run\|create <description>]` |
| `@milestones` | milestones linked to tasks, with verified progress | `@milestones [list\|add <title> [--due date]\|done n\|reopen n\|remove n\|link n <task>...]` |
| `@pr` | pull request description from real commits and diff | `@pr [--base branch] [--title t] [--out file] [--create]` |
| `@release` | release readiness checks; optional local tag (never pushes) | `@release [--tag v1.2.3] [--force] [--skip-tests]` |
| `@review-commit` | review one commit (read-only) (alias: @rc) | `@review-commit [rev]` |
| `@todo` | open tasks plus TODO/FIXME markers found in code | `@todo [add <text>\|import]` |
| `@worktree` | list, add or remove git worktrees | `@worktree [list\|add <branch> [path]\|remove <path>]` |

## Design

| Command | What it does | Usage |
|---|---|---|
| `@design-audit` | check the code against the locked design | `@design-audit [--limit n]` |
| `@design-diff` | show drift from the locked design | `@design-diff` |
| `@design-lock` | lock the current design/architecture so the agent preserves it | `@design-lock [show\|--update\|rule <t>\|prohibit <t>\|decision <t>\|unlock]` |

## Extensions and data

| Command | What it does | Usage |
|---|---|---|
| `@data` | where data lives; export or delete it | `@data [paths\|export [file]\|delete sessions\|reports\|cache]` |
| `@plugins` | list, inspect, enable (with consent) or disable plugins | `@plugins [list\|describe <n>\|enable <n>\|disable <n>]` |
| `@preview` | run the dev server and check the page in headless Chromium (alias: @browser) | `@preview [status\|start\|stop\|check [url] [--selector 'a\|b'] [--screenshot]]` |
| `@skills` | list or show skills the agent can read | `@skills [list\|show <name>]` |

## Modes

| Command | What it does | Usage |
|---|---|---|
| `@btw` | ask a side question without interrupting the main task | `@btw <question> \| list \| cancel [n] \| clear` |
| `@cancel` | stop the running task and save its state | `@cancel` |
| `@chat` | read-only conversation mode | `@chat [question]` |
| `@codingagent` | enable the coding-agent workflow (alias: @code) | `@codingagent [request]` |
| `@pause` | pause the running task safely | `@pause` |
