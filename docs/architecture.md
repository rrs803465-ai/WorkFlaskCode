# Architecture

```
workflaskcode.py        launcher (version check, dependency check)
workflaskcode/
  cli.py                argparse, interactive loop, headless run
  app.py                Runtime: session, project, mode, approvals, model switch, worker thread
  agent/                engine (loop), router (fallback/backoff), registry+toolset (tools), btw (side chat),
                        context (compaction), handoff, prompts
  providers/            normalised types; openai_compat, openrouter, anthropic; catalog (live model lists)
  security/             permission classifier and approval types
  workspace/            path guard, project detection, metadata
  tools/                file tools (journal, undo), process runner
  tasks/                ledger, checkpoints
  state/                SQLite (messages, tasks, tool executions, usage, kv)
  config/               pydantic settings, TOML store, credentials, paths
  commands/             73 @commands grouped by area
  audit/ design/        audit engine, dead-code analysis, design lock
  plugins/ templates/ browser/ testing/ usage/ git/ ui/ http/ utils/
```

Key decisions:

- Provider responses are normalised into `Message`, `ToolCall`, `ChatResponse` and `Usage`; the loop never sees provider-specific shapes.
- The tool executor gates tools by access class (read, write, exec, meta) per mode, records each execution for idempotency, and reconciles interrupted operations on resume.
- The agent runs on a worker thread; the main thread owns the terminal. Approvals and agent questions are posted to an `ApprovalBroker`, and the next typed line answers them.
- `@btw` runs per-question threads with their own cancel event, a read-only tool context and a separate history channel, so they can never alter main task state.
- Context compaction is deterministic and rebuilt from the ledger, file journal and test results; files on disk stay authoritative.
