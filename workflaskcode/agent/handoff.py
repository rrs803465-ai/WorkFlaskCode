from __future__ import annotations

import time

from workflaskcode.agent.registry import ToolContext
from workflaskcode.tasks.ledger import TaskStatus
from workflaskcode.workspace.metadata import ProjectMetadata


def state_summary(ctx: ToolContext, *, model: str | None, mode: str, design_rules: str | None = None) -> str:
    ledger = ctx.ledger
    lines = [
        "## Authoritative state",
        f"model: {model or 'unset'} | mode: {mode} | permission mode: {ctx.policy.mode.value}",
    ]
    lines.append(f"project root: {ctx.project_root}")
    if ctx.git.is_repo():
        dirty = sorted(ctx.git.dirty_paths())
        lines.append(
            f"git: branch {ctx.git.current_branch() or '?'} @ {ctx.git.head() or 'no commits'}; {len(dirty)} changed path(s)"
        )
        if dirty:
            lines.append("changed: " + ", ".join(dirty[:20]) + (" ..." if len(dirty) > 20 else ""))
    else:
        lines.append("git: not a repository")
    lines.append("")
    lines.append("## Tasks")
    lines.append(ledger.render())
    current = ledger.current()
    if current:
        lines.append(f"current task: {current.ordinal}. {current.description}")
        if current.next_action:
            lines.append(f"next action: {current.next_action}")
        if current.relevant_files:
            lines.append("relevant files: " + ", ".join(current.relevant_files[:15]))
        if current.failure:
            lines.append(f"last failure: {current.failure[:300]}")
    else:
        nxt = ledger.next_ready()
        if nxt:
            lines.append(f"next unfinished task: {nxt.ordinal}. {nxt.description}")
    journal = ctx.files.journal(15)
    if journal:
        lines.append("")
        lines.append("## Files changed by the agent (most recent first)")
        for rec in journal:
            lines.append(f"- {rec.op} {rec.path}")
    last_test = ctx.extra.get("last_test")
    if last_test:
        lines.append("")
        lines.append("## Last test run")
        lines.append(
            f"{'PASS' if last_test['ok'] else 'FAIL'}: {last_test['command']} - {last_test['summary']}"
        )
        for failure in (last_test.get("failures") or [])[:5]:
            lines.append(f"  - {str(failure)[:200]}")
    errors = ctx.extra.get("recent_errors") or []
    if errors:
        lines.append("")
        lines.append("## Known errors")
        lines += [f"- {e}" for e in errors[-6:]]
    pending = ctx.db.query(
        "SELECT tool, args FROM tool_executions WHERE session_id=? AND status='started'", (ctx.session_id,)
    )
    if pending:
        lines.append("")
        lines.append("## Unresolved tool operations (verify actual state before repeating)")
        lines += [f"- {p['tool']} {p['args'][:160]}" for p in pending]
    if design_rules:
        lines.append("")
        lines.append("## Design lock")
        lines.append(design_rules)
    return "\n".join(lines)


def next_step_text(ctx: ToolContext) -> str:
    current = ctx.ledger.current()
    if current:
        return f"Continue task {current.ordinal}: {current.description}" + (
            f" (next action: {current.next_action})" if current.next_action else ""
        )
    nxt = ctx.ledger.next_ready()
    if nxt:
        return f"Start task {nxt.ordinal}: {nxt.description}"
    open_left = [t for t in ctx.ledger.list() if t.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)]
    if open_left:
        return "Remaining tasks are blocked or failed; review them with the user."
    return "No unfinished tasks."


def write_handoff(
    ctx: ToolContext,
    metadata: ProjectMetadata | None,
    *,
    model: str | None,
    mode: str,
    reason: str,
    design_rules: str | None = None,
) -> str:
    text = "\n".join(
        [
            "# Handoff",
            f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"reason: {reason}",
            "",
            state_summary(ctx, model=model, mode=mode, design_rules=design_rules),
            "",
            "## Next step",
            next_step_text(ctx),
            "",
        ]
    )
    if metadata is not None:
        metadata.ensure()
        metadata.write_text("handoff.md", text)
        metadata.write_json("tasks.json", ctx.ledger.snapshot())
        metadata.write_json(
            "state.json",
            {
                "session_id": ctx.session_id,
                "model": model,
                "mode": mode,
                "updated": time.time(),
                "counts": ctx.ledger.counts(),
            },
        )
    ctx.db.kv_set("handoff", ctx.session_id, {"text": text, "ts": time.time(), "reason": reason})
    return text


def load_handoff(ctx: ToolContext) -> str | None:
    stored = ctx.db.kv_get("handoff", ctx.session_id)
    if isinstance(stored, dict):
        return stored.get("text")
    if isinstance(stored, str):
        return stored
    return None
