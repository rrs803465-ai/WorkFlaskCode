from __future__ import annotations

import difflib
import json
import time
from pathlib import Path

from workflaskcode.agent.context import estimate_tokens
from workflaskcode.commands.helpers import agent_task, ago
from workflaskcode.commands.registry import Command, CommandContext, CommandError, CommandRegistry
from workflaskcode.config.models import PermissionMode
from workflaskcode.git import GitRepo
from workflaskcode.tasks.ledger import TaskStatus
from workflaskcode.utils.fs import atomic_write_text
from workflaskcode.utils.redact import redact


def cmd_plan(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    assert rt.ledger is not None
    sub = ctx.sub
    if sub == "add":
        text = " ".join(ctx.positional[1:])
        if not text:
            raise CommandError("usage: @plan add <task>")
        task = rt.ledger.add(text)
        c.ok(f"task {task.ordinal} added")
        return
    if sub == "clear":
        open_count = sum(
            1 for t in rt.ledger.list() if t.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
        )
        if open_count and ctx.interaction.confirm(
            f"cancel {open_count} open task(s)? Completed tasks are kept.", False
        ):
            c.ok(f"{rt.ledger.cancel_open()} task(s) cancelled")
        return
    text = " ".join(ctx.positional)
    if text:
        if rt.worker_running:
            raise CommandError("a task is running; wait for it or @cancel")
        agent_task(
            ctx,
            f"Plan this work. Inspect the project, then record ordered, verifiable steps with the `plan` tool. Do not change any file.\n\nGoal: {text}",
            mode="plan",
        )
        return
    tasks = rt.ledger.list()
    if tasks:
        c.task_list(tasks)
        c.dim("@plan <goal> asks the agent to plan; @plan add <task>; @tasks manages status")
    else:
        c.dim("no plan yet. @plan <goal> asks the agent to create one.")


def _task_ref(ctx: CommandContext, usage: str):
    ref = ctx.need(2, usage)[1]
    task = ctx.rt.ledger.resolve(ref)
    if task is None:
        raise CommandError(f"unknown task '{ref}'")
    return task


def cmd_tasks(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    ledger = rt.ledger
    assert ledger is not None
    sub = ctx.sub or "list"
    if sub == "list":
        tasks = ledger.list()
        if not tasks:
            c.dim("no tasks")
            return
        c.task_list(tasks)
        counts = ledger.counts()
        c.dim("  ".join(f"{k} {v}" for k, v in counts.items() if v))
    elif sub == "show":
        t = _task_ref(ctx, "tasks show <n>")
        c.kv(
            [
                ("id", t.id),
                ("task", f"{t.ordinal}. {t.description}"),
                (
                    "status",
                    t.status.value
                    + ("" if t.verified or t.status is not TaskStatus.COMPLETED else " (unverified)"),
                ),
                ("attempts", str(t.attempts)),
                ("model", t.assigned_model or "-"),
                ("files", ", ".join(t.relevant_files) or "-"),
                ("next action", t.next_action or "-"),
                ("failure", t.failure or "-"),
                ("checkpoint", t.checkpoint_id or "-"),
                ("depends on", ", ".join(t.dependencies) or "-"),
            ],
            title=f"task {t.ordinal}",
        )
        for v in t.validation[-8:]:
            c.line(
                f"  {'pass' if v['passed'] else 'FAIL'}  {v['name']}  {v['detail'][:100]}  ({ago(v['ts'])})"
            )
    elif sub == "add":
        text = " ".join(ctx.positional[1:])
        if not text:
            raise CommandError("usage: @tasks add <description>")
        c.ok(f"task {ledger.add(text).ordinal} added")
    elif sub == "start":
        t = ledger.set_status(_task_ref(ctx, "tasks start <n>").id, TaskStatus.IN_PROGRESS)
        c.ok(f"task {t.ordinal} in progress")
    elif sub == "done":
        t = _task_ref(ctx, "tasks done <n> [--force]")
        if ctx.flag("force"):
            if not ctx.interaction.confirm(
                f"mark task {t.ordinal} complete without automated validation? This is recorded as a manual override.",
                False,
            ):
                return
            ledger.force_verified(t.id, "marked by the user")
        ledger.complete(t.id)
        c.ok(f"task {t.ordinal} completed")
    elif sub == "verify":
        t = _task_ref(ctx, "tasks verify <n>")
        from workflaskcode.commands.helpers import run_user_command

        argv = rt.info().test_commands[0] if rt.info().test_commands else None
        if not argv:
            raise CommandError("no test command detected; use @tasks done <n> --force or @test --cmd")
        result = run_user_command(ctx, list(argv))
        from workflaskcode.testing.runner import parse_test_summary

        summary = parse_test_summary(result.output, result.exit_code)
        ledger.record_validation(t.id, "tests: " + " ".join(argv), result.ok, summary.headline)
        (c.ok if result.ok else c.warn)(f"task {t.ordinal}: {summary.headline}")
    elif sub in ("block", "fail"):
        t = _task_ref(ctx, f"tasks {sub} <n> <reason>")
        reason = " ".join(ctx.positional[2:]) or None
        ledger.set_status(t.id, TaskStatus.BLOCKED if sub == "block" else TaskStatus.FAILED, failure=reason)
        c.ok(f"task {t.ordinal} {sub}ed")
    elif sub == "cancel":
        t = _task_ref(ctx, "tasks cancel <n>")
        ledger.set_status(t.id, TaskStatus.CANCELLED)
        c.ok(f"task {t.ordinal} cancelled")
    elif sub == "retry":
        t = _task_ref(ctx, "tasks retry <n>")
        ledger.set_status(t.id, TaskStatus.PENDING)
        c.ok(f"task {t.ordinal} back to pending; @resume continues")
    elif sub == "next":
        t = ledger.next_ready()
        c.line(
            f"next ready: {t.ordinal}. {t.description}"
            if t
            else "nothing ready (all done, blocked, or waiting on dependencies)"
        )
    else:
        raise CommandError(
            "usage: @tasks [list|show n|add text|start n|done n [--force]|verify n|block n why|fail n why|cancel n|retry n|next]"
        )


def cmd_checkpoint(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    assert rt.checkpoints is not None and rt.git is not None
    if ctx.sub == "list" or (not ctx.positional and ctx.flag("list")):
        rows = [
            [cp.id, cp.label[:50], ago(cp.created), (cp.git_head or "-")[:8], str(len(cp.tasks))]
            for cp in rt.checkpoints.list()
        ]
        c.table(["id", "label", "created", "git", "tasks"], rows) if rows else c.dim("no checkpoints")
        return
    label = " ".join(ctx.positional) or "manual checkpoint"
    head = rt.git.head() if rt.git.is_repo() else None
    cp = rt.checkpoints.create(label, git_head=head, tasks=rt.ledger.snapshot() if rt.ledger else [])
    rt.save_state(f"checkpoint {cp.id}")
    c.ok(f"checkpoint {cp.id} saved ({label})")
    dirty = rt.git.dirty_paths() if rt.git.is_repo() else set()
    if dirty:
        c.dim(
            f"{len(dirty)} uncommitted path(s) exist; a checkpoint records the agent's change journal position, it does not commit or stash your work"
        )


def _reverse_patch(ctx: CommandContext, changes) -> str:
    rt = ctx.rt
    out = []
    for ch in changes:
        real = Path(ch.path)
        rel = rt.guard.relative(real)
        current = (
            real.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if real.is_file()
            else []
        )
        before = (
            Path(ch.backup_path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if ch.backup_path and Path(ch.backup_path).is_file()
            else []
        )
        out.append("".join(difflib.unified_diff(current, before, f"a/{rel}", f"b/{rel}")))
    return "".join(out)


def cmd_rollback(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    assert rt.checkpoints is not None
    if rt.worker_running:
        raise CommandError("a task is running; @cancel it first")
    ref = ctx.positional[0] if ctx.positional else None
    cp, lines = rt.checkpoints.preview_rollback(ref)
    if not lines:
        c.info(f"nothing to roll back since checkpoint {cp.id} ({cp.label})")
        return
    c.heading(f"rollback to {cp.id}", cp.label)
    for ln in lines:
        c.line("  " + ln)
    if ctx.flag("patch"):
        changes = rt.checkpoints.changes_since(cp)
        target = rt.home.reports_dir / f"rollback-{cp.id}.patch"
        atomic_write_text(target, _reverse_patch(ctx, changes))
        c.ok(f"reverse patch written to {target}; apply it yourself with: patch -p1 < {target}")
        return
    c.dim(
        "Only the agent's own edits are reverted, newest first. Files you changed afterwards are skipped and listed. Git history, branches and your other changes are not touched."
    )
    if not ctx.flag("yes") and not ctx.interaction.confirm(f"revert {len(lines)} agent change(s)?", False):
        c.info("nothing changed. Use --patch to write a reverse patch instead.")
        return
    _, results = rt.checkpoints.rollback(ref)
    for r in results:
        (c.warn if r.startswith("SKIPPED") else c.ok)(r)
    if any(r.startswith("SKIPPED") for r in results):
        c.info(
            "skipped files were modified after the agent wrote them; use @rollback --patch for a patch you can review"
        )


def cmd_compact(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    assert rt.loop is not None
    before_tokens = estimate_tokens(rt.loop.history)
    if rt.worker_running:
        raise CommandError("a task is running; compaction applies between tasks")
    before, after = rt.loop.compact_now(force=True)
    rt.save_state("manual compaction")
    c.ok(
        f"history {before} -> {after} messages, about {before_tokens} -> {estimate_tokens(rt.loop.history)} tokens"
    )
    c.dim(
        "The summary is rebuilt from the task ledger, file journal and test results; files on disk remain authoritative."
    )


def cmd_permissions(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    sub = ctx.sub
    help_text = {
        PermissionMode.READ_ONLY: "inspect only; no writes, no commands",
        PermissionMode.APPROVE_ALL: "ask before every write and command",
        PermissionMode.APPROVE_RISKY: "run safe reads; ask for anything risky",
        PermissionMode.RESTRICTED_AUTONOMOUS: "allow-listed project-local actions run unattended",
    }
    if sub == "check":
        command = ctx.rest_after_sub()
        if not command:
            raise CommandError("usage: @permissions check <command line>")
        assert rt.policy is not None
        d = rt.policy.evaluate(shell=command)
        c.kv(
            [
                ("command", command),
                ("risk", d.risk.name.lower()),
                ("decision", d.action),
                ("reason", d.reason),
                ("effects", ", ".join(d.effects) or "-"),
            ],
            title="policy check",
        )
        return
    if sub in ("allow", "deny"):
        pattern = " ".join(ctx.positional[1:])
        if not pattern:
            raise CommandError(f"usage: @permissions {sub} <command glob>")
        key = "allow" if sub == "allow" else "deny"
        current = list(getattr(rt.global_settings.permissions, key))
        if pattern not in current:
            current.append(pattern)
        rt.apply_settings(
            lambda cur: cur.model_copy(
                update={"permissions": cur.permissions.model_copy(update={key: current})}
            )
        )
        c.ok(f"{key} rule added: {pattern}")
        return
    if sub == "reset":
        rt.session_overrides.pop("permissions", None)
        rt.reload_settings()
        c.ok("session override removed")
        return
    if sub and sub not in ("show",):
        try:
            mode = PermissionMode(sub.replace("-", "_"))
        except ValueError as exc:
            raise CommandError(
                f"unknown mode '{sub}'",
                hint="choose read_only, approve_all, approve_risky or restricted_autonomous",
            ) from exc
        if mode is PermissionMode.RESTRICTED_AUTONOMOUS and not ctx.interaction.confirm(
            "restricted autonomous lets allow-listed commands run without asking. Continue?", False
        ):
            return
        persist = bool(ctx.flag("save"))
        rt.set_permission_mode(mode, persist=persist)
        c.ok(
            f"permission mode: {mode.value} ({'saved as default' if persist else 'this session only; add --save to keep'})"
        )
        return
    cur = rt.policy.mode if rt.policy else rt.settings.permissions.mode
    p = rt.settings.permissions
    c.kv(
        [
            ("mode", f"{cur.value}: {help_text[cur]}"),
            ("allow rules", ", ".join(p.allow) or "none"),
            ("deny rules", ", ".join(p.deny) or "none"),
            ("network", p.network),
            ("shell strings", "allowed" if p.allow_shell_strings else "disabled"),
            ("command timeout", f"{p.command_timeout_seconds:.0f}s"),
            ("extra roots", ", ".join(p.extra_roots) or "none"),
        ],
        title="permissions",
    )
    c.dim(
        "modes: "
        + ", ".join(m.value for m in PermissionMode)
        + ".  @permissions <mode> [--save]  |  allow <glob>  |  deny <glob>  |  check <command>"
    )
    c.dim(
        "This is an approval policy with a command risk classifier, not an OS sandbox. Approved commands run with your user's privileges."
    )


def cmd_export(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    assert rt.session is not None and rt.ledger is not None
    fmt = str(ctx.flag("format", "md"))
    if fmt not in ("md", "json"):
        raise CommandError("--format must be md or json")
    main = rt.sessions.messages(rt.session.id, "main")
    side = rt.sessions.messages(rt.session.id, "side")
    target = (
        Path(ctx.positional[0]).expanduser()
        if ctx.positional
        else rt.home.reports_dir / f"session-{rt.session.id}.{fmt}"
    )
    if target.exists() and not ctx.interaction.confirm(f"{target} exists. Overwrite?", False):
        return
    if fmt == "json":
        body = json.dumps(
            {
                "session": rt.session.id,
                "project": str(rt.root),
                "tasks": rt.ledger.snapshot(),
                "main": main,
                "side": side,
                "usage": rt.tracker.by_model(),
            },
            indent=2,
            default=str,
        )
    else:
        lines = [
            f"# Session {rt.session.id}",
            f"Project: {rt.root}",
            f"Exported: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## Tasks",
            "",
            rt.ledger.render(),
            "",
            "## Conversation",
            "",
        ]
        for m in main:
            if m["role"] == "tool":
                lines.append(f"**tool**: `{' '.join(m['content'].split())[:200]}`")
            else:
                lines.append(f"**{m['role']}**: {m['content']}")
            lines.append("")
        if side:
            lines += ["## Side chat (@btw)", ""] + [
                f"**{m['role']}**: {m['content']}\n" for m in side if m["role"] in ("user", "assistant")
            ]
        body = "\n".join(lines)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, redact(body))
    c.ok(f"exported {len(main)} messages to {target} (secrets redacted)")


def cmd_branch(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    git = rt.git
    assert git is not None
    if not GitRepo.available() or not git.is_repo():
        raise CommandError(
            "this project is not a git repository", hint="run `git init` (or @new with git) first"
        )
    sub = ctx.sub
    if sub in (None, "list"):
        for name, current in git.branches():
            c.line(f"  {'*' if current else ' '} {name}")
        dirty = git.dirty_paths()
        if dirty:
            c.dim(f"{len(dirty)} uncommitted path(s)")
        return
    if sub in ("create", "new"):
        name = ctx.need(2, "branch create <name>")[1]
        git.create_branch(name, switch=True)
        c.ok(f"created and switched to {name}; uncommitted changes came along unchanged")
        return
    name = ctx.positional[1] if sub == "switch" and len(ctx.positional) > 1 else ctx.positional[0]
    dirty = git.dirty_paths()
    if dirty and not ctx.interaction.confirm(
        f"{len(dirty)} uncommitted path(s) exist. Git may refuse the switch or carry them over. Try anyway?",
        False,
    ):
        return
    git.switch_branch(name)
    c.ok(f"on branch {name}")


def cmd_diff(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    git = rt.git
    assert git is not None and rt.files is not None
    if GitRepo.available() and git.is_repo():
        paths = ctx.positional or None
        staged = bool(ctx.flag("staged"))
        if ctx.flag("stat"):
            c.line(git.diff(staged=staged, paths=paths, stat=True) or "no changes")
            return
        text = git.diff(staged=staged, paths=paths)
        untracked = [e.path for e in git.status() if e.untracked]
        if text.strip():
            c.diff(text)
        else:
            c.dim("no tracked changes" + (" in the index" if staged else ""))
        if untracked and not staged:
            c.line(
                c.p.paint("untracked: ", "muted")
                + ", ".join(untracked[:20])
                + (" ..." if len(untracked) > 20 else "")
            )
        return
    records = rt.files.journal(200)
    if not records:
        c.dim("not a git repository and the agent has not changed any file yet")
        return
    c.info("not a git repository: showing the agent's own edits from its change journal")
    seen: set[str] = set()
    for rec in records:
        if rec.path in seen:
            continue
        seen.add(rec.path)
        real = Path(rec.path)
        rel = rt.guard.relative(real)
        before = (
            Path(rec.backup_path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if rec.backup_path and Path(rec.backup_path).is_file()
            else []
        )
        after = (
            real.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if real.is_file()
            else []
        )
        c.diff("".join(difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}")))


def cmd_undo(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    assert rt.files is not None
    if rt.worker_running:
        raise CommandError("a task is running; @cancel it first")
    count = int(ctx.positional[0]) if ctx.positional and ctx.positional[0].isdigit() else 1
    pending = rt.files.journal(count)
    if not pending:
        c.dim("nothing to undo: the agent has not changed any file in this session")
        return
    for rec in pending:
        c.line(f"  would undo: {rec.op} {rt.guard.relative(Path(rec.path))}")
    if not ctx.flag("yes") and not ctx.interaction.confirm(
        f"undo the last {len(pending)} agent change(s)?", False
    ):
        return
    for r in rt.files.undo_last(count):
        (c.warn if r.startswith("SKIPPED") else c.ok)(r)


def register(reg: CommandRegistry) -> None:
    add = lambda *a, **k: reg.register(Command(*a, **k))
    add(
        "plan",
        cmd_plan,
        "show the plan, or have the agent plan a goal (read-only)",
        "[goal] | add <task> | clear",
        "planning",
        needs_project=True,
    )
    add(
        "tasks",
        cmd_tasks,
        "task ledger: status, validation, manual overrides",
        "[list|show n|add text|start n|done n [--force]|verify n|block n why|fail n why|cancel n|retry n|next]",
        "planning",
        ("t",),
        needs_project=True,
    )
    add(
        "checkpoint",
        cmd_checkpoint,
        "save a checkpoint of agent changes and task state",
        "[label] | list",
        "planning",
        needs_project=True,
    )
    add(
        "rollback",
        cmd_rollback,
        "revert the agent's own edits since a checkpoint",
        "[checkpoint-id] [--patch] [--yes]",
        "planning",
        needs_project=True,
    )
    add(
        "compact",
        cmd_compact,
        "compact conversation context deterministically",
        group="planning",
        needs_project=True,
    )
    add(
        "permissions",
        cmd_permissions,
        "inspect or change the permission policy",
        "[show|<mode> [--save]|allow <glob>|deny <glob>|check <command>|reset]",
        "planning",
    )
    add(
        "export",
        cmd_export,
        "export the session transcript (secrets redacted)",
        "[path] [--format md|json]",
        "planning",
        needs_project=True,
    )
    add(
        "branch",
        cmd_branch,
        "list, create or switch git branches",
        "[list|create <name>|switch <name>]",
        "planning",
        needs_project=True,
    )
    add(
        "diff",
        cmd_diff,
        "show real uncommitted changes",
        "[paths] [--staged] [--stat]",
        "planning",
        needs_project=True,
    )
    add(
        "undo",
        cmd_undo,
        "undo the agent's last file change(s) safely",
        "[n] [--yes]",
        "planning",
        needs_project=True,
    )
