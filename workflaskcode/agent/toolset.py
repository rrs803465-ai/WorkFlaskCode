from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from workflaskcode.agent.registry import (
    ACCESS_EXEC,
    ACCESS_META,
    ACCESS_READ,
    ACCESS_WRITE,
    ToolContext,
    ToolDef,
    ToolRegistry,
)
from workflaskcode.errors import PermissionDenied, ToolError
from workflaskcode.security.permissions import ApprovalRequest
from workflaskcode.tasks.ledger import TaskStatus
from workflaskcode.testing.runner import parse_test_summary
from workflaskcode.tools.process import run_process


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


S = {"type": "string"}
INT = {"type": "integer"}
B = {"type": "boolean"}


def _clip(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def run_checked(
    ctx: ToolContext, *, argv: list[str] | None, shell: str | None, cwd: Path, timeout: float | None
) -> Any:
    settings = ctx.settings()
    perms = settings.permissions
    decision = ctx.policy.evaluate(argv=argv, shell=shell, cwd=cwd)
    display = shell if shell is not None else " ".join(shlex.quote(a) for a in argv or [])
    if decision.action == "deny":
        ctx.db.audit("command_denied", {"command": display, "reason": decision.reason}, ctx.session_id)
        raise PermissionDenied(f"command blocked: {decision.reason}")
    if decision.action == "ask":
        request = ApprovalRequest(
            "command",
            display,
            decision.reason,
            cwd=str(cwd),
            effects=decision.effects,
            risk=decision.risk,
            destructive=decision.risk.name == "DESTRUCTIVE",
        )
        answer = ctx.approver(request)
        ctx.db.audit(
            "command_approval",
            {"command": display, "approved": answer.approved, "risk": decision.risk.name},
            ctx.session_id,
        )
        if not answer.approved:
            raise PermissionDenied(f"command was not approved by the user: {display}")
        if answer.remember and decision.risk.name != "DESTRUCTIVE":
            ctx.policy.remember(display.strip())
    ctx.db.audit(
        "command_run", {"command": display, "cwd": str(cwd), "risk": decision.risk.name}, ctx.session_id
    )
    return run_process(
        argv=argv,
        shell=shell,
        cwd=cwd,
        timeout=min(timeout or perms.command_timeout_seconds, perms.command_timeout_seconds * 5),
        max_output=perms.max_output_bytes,
        cancel=ctx.cancel,
    )


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError as exc:
        raise ToolError(f"could not parse command: {exc}; pass shell=true for shell syntax") from exc


def _h_list_dir(ctx: ToolContext, a: dict[str, Any]) -> str:
    return ctx.files.list_dir(a.get("path", "."), depth=int(a.get("depth", 1)))


def _h_read_file(ctx: ToolContext, a: dict[str, Any]) -> str:
    return ctx.files.read_file(a["path"], start=int(a.get("start", 1)), end=a.get("end"))


def _h_search(ctx: ToolContext, a: dict[str, Any]) -> str:
    return ctx.files.search(
        a["pattern"],
        a.get("path", "."),
        glob=a.get("glob"),
        regex=bool(a.get("regex", False)),
        ignore_case=bool(a.get("ignore_case", False)),
    )


def _h_find_files(ctx: ToolContext, a: dict[str, Any]) -> str:
    return ctx.files.find_files(a["glob"], a.get("path", "."))


def _h_find_symbol(ctx: ToolContext, a: dict[str, Any]) -> str:
    return ctx.files.find_symbol(a["name"], a.get("path", "."))


def _h_git_status(ctx: ToolContext, a: dict[str, Any]) -> str:
    if not ctx.git.is_repo():
        return "not a git repository"
    entries = ctx.git.status()
    head = f"branch: {ctx.git.current_branch() or '(detached)'}  head: {(ctx.git.head() or 'none')[:8]}"
    if not entries:
        return head + "\nworking tree clean"
    return head + "\n" + "\n".join(f"{e.label:16} {e.path}" for e in entries)


def _h_git_diff(ctx: ToolContext, a: dict[str, Any]) -> str:
    if not ctx.git.is_repo():
        return "not a git repository"
    paths = [a["path"]] if a.get("path") else None
    out = ctx.git.diff(staged=bool(a.get("staged", False)), paths=paths)
    return out or "no differences"


def _h_create_file(ctx: ToolContext, a: dict[str, Any]) -> str:
    summary = ctx.files.create_file(a["path"], a["content"], overwrite=bool(a.get("overwrite", False)))
    ctx.console.file_event(summary.op, summary.path, summary.added, summary.removed)
    _track_file(ctx, summary.path)
    return f"{summary}"


def _h_edit_file(ctx: ToolContext, a: dict[str, Any]) -> str:
    summary = ctx.files.edit_file(
        a["path"],
        a["old_text"],
        a["new_text"],
        replace_all=bool(a.get("replace_all", False)),
        expected_hash=a.get("expected_hash"),
    )
    ctx.console.file_event(summary.op, summary.path, summary.added, summary.removed)
    _track_file(ctx, summary.path)
    return f"{summary}"


def _h_apply_patch(ctx: ToolContext, a: dict[str, Any]) -> str:
    results = ctx.files.apply_patch(a["patch"])
    for s in results:
        ctx.console.file_event(s.op, s.path, s.added, s.removed)
        _track_file(ctx, s.path)
    return "\n".join(str(s) for s in results)


def _h_move_file(ctx: ToolContext, a: dict[str, Any]) -> str:
    summary = ctx.files.move_file(a["source"], a["destination"])
    ctx.console.file_event("move", summary.path)
    return str(summary)


def _h_delete_file(ctx: ToolContext, a: dict[str, Any]) -> str:
    summary = ctx.files.delete_file(a["path"])
    ctx.console.file_event("delete", summary.path)
    return str(summary)


def _track_file(ctx: ToolContext, rel_path: str) -> None:
    task = ctx.ledger.current()
    if task and rel_path not in task.relevant_files:
        ctx.ledger.set_files(task.id, [*task.relevant_files, rel_path])


def _cwd(ctx: ToolContext, a: dict[str, Any]) -> Path:
    cwd = ctx.guard.resolve(a.get("cwd", "."))
    if not cwd.is_dir():
        raise ToolError(f"cwd is not a directory: {a.get('cwd')}")
    return cwd


def _h_run_command(ctx: ToolContext, a: dict[str, Any]) -> str:
    command = a["command"]
    use_shell = bool(a.get("shell", False))
    argv = None if use_shell else _split_command(command)
    if argv and argv[0] in ("cd",):
        raise ToolError("'cd' has no effect between calls; pass cwd instead")
    ctx.console.tool("run_command", _clip(command, 90), status="run")
    result = run_checked(
        ctx,
        argv=argv,
        shell=command if use_shell else None,
        cwd=_cwd(ctx, a),
        timeout=float(a["timeout"]) if a.get("timeout") else None,
    )
    return result.render()


def _h_run_tests(ctx: ToolContext, a: dict[str, Any]) -> str:
    info = ctx.project_info()
    if a.get("command"):
        argv = _split_command(a["command"])
    elif info.test_commands:
        argv = list(info.test_commands[0])
        if a.get("target"):
            argv += _split_command(a["target"])
    else:
        raise ToolError("no test command detected for this project; pass command explicitly")
    ctx.console.tool("run_tests", _clip(" ".join(argv), 90), status="run")
    result = run_checked(
        ctx, argv=argv, shell=None, cwd=ctx.project_root, timeout=float(a.get("timeout", 600))
    )
    summary = parse_test_summary(result.output, result.exit_code)
    task = ctx.ledger.current()
    if (
        task
        and not result.timed_out
        and not result.cancelled
        and result.exit_code is not None
        and result.error is None
    ):
        ctx.ledger.record_validation(task.id, "tests: " + " ".join(argv), result.ok, summary.headline)
    ctx.extra["last_test"] = {
        "command": " ".join(argv),
        "ok": result.ok,
        "summary": summary.headline,
        "failures": summary.failures,
    }
    return f"{summary.render()}\n\n{result.render()}"


def _h_plan(ctx: ToolContext, a: dict[str, Any]) -> str:
    steps = a["steps"]
    if not steps or not all(isinstance(s, str) and s.strip() for s in steps):
        raise ToolError("steps must be a non-empty list of strings")
    existing = {t.description for t in ctx.ledger.list()}
    created = []
    previous = None
    for step in steps:
        if step in existing:
            continue
        deps = [previous.id] if previous and a.get("sequential", True) else []
        previous = ctx.ledger.add(step, dependencies=deps)
        created.append(previous)
    return f"added {len(created)} tasks\n{ctx.ledger.render()}"


def _h_task_update(ctx: ToolContext, a: dict[str, Any]) -> str:
    task = ctx.ledger.resolve(str(a["task"]))
    if task is None:
        raise ToolError(f"unknown task '{a['task']}'")
    status = a["status"]
    if status == "completed":
        task = ctx.ledger.complete(task.id)
    else:
        target = TaskStatus(status)
        task = ctx.ledger.set_status(task.id, target, failure=a.get("note"))
        if target is TaskStatus.IN_PROGRESS:
            model = ctx.extra.get("active_model")
            if model:
                ctx.ledger.set_model(task.id, model)
    if a.get("next_action"):
        ctx.ledger.set_next_action(task.id, a["next_action"])
    return ctx.ledger.render()


def _h_verify_task(ctx: ToolContext, a: dict[str, Any]) -> str:
    task = ctx.ledger.resolve(str(a["task"]))
    if task is None:
        raise ToolError(f"unknown task '{a['task']}'")
    kind = a["check"]
    if kind == "file_exists":
        real = ctx.guard.resolve(a["path"])
        passed, detail = real.is_file(), f"{a['path']} {'exists' if real.is_file() else 'does not exist'}"
    elif kind == "file_contains":
        real = ctx.guard.resolve(a["path"])
        text = real.read_text(encoding="utf-8", errors="replace") if real.is_file() else ""
        passed = a.get("text", "") in text and bool(a.get("text"))
        detail = f"{a['path']} {'contains' if passed else 'does not contain'} the expected text"
    elif kind == "command":
        result = run_checked(
            ctx, argv=_split_command(a["command"]), shell=None, cwd=ctx.project_root, timeout=300
        )
        passed, detail = result.ok, f"`{a['command']}` exit {result.exit_code}: {_clip(result.output, 300)}"
    else:
        raise ToolError("check must be file_exists, file_contains or command")
    ctx.ledger.record_validation(task.id, f"verify:{kind}", passed, detail)
    return f"verification {'PASSED' if passed else 'FAILED'}: {detail}"


def _h_ask_user(ctx: ToolContext, a: dict[str, Any]) -> str:
    answer = ctx.asker(a["question"])
    if answer is None:
        return "no answer available (the user is not reachable); continue with a reasonable default and state the assumption"
    return f"user answered: {answer}"


def _h_finish(ctx: ToolContext, a: dict[str, Any]) -> str:
    open_tasks = [t for t in ctx.ledger.list() if t.status in (TaskStatus.IN_PROGRESS,)]
    if open_tasks:
        raise ToolError(
            "tasks are still in progress: "
            + ", ".join(f"{t.ordinal}. {t.description}" for t in open_tasks)
            + ". Verify and complete them, or mark them blocked/failed with a note."
        )
    ctx.extra["finish_summary"] = a["summary"]
    return "finish recorded"


def register_core_tools(registry: ToolRegistry) -> None:
    path_arg = {"path": S}
    tools = [
        ToolDef(
            "list_dir",
            "List files and directories (ignores build/vendor folders). depth defaults to 1.",
            _obj({"path": S, "depth": INT}),
            _h_list_dir,
            ACCESS_READ,
            lambda a: a.get("path", "."),
        ),
        ToolDef(
            "read_file",
            "Read a text file with line numbers. Use start/end for large files.",
            _obj({"path": S, "start": INT, "end": INT}, ["path"]),
            _h_read_file,
            ACCESS_READ,
            lambda a: a.get("path", ""),
        ),
        ToolDef(
            "search",
            "Search file contents for a literal string (or regex). Returns file:line: text.",
            _obj({"pattern": S, "path": S, "glob": S, "regex": B, "ignore_case": B}, ["pattern"]),
            _h_search,
            ACCESS_READ,
            lambda a: a.get("pattern", ""),
        ),
        ToolDef(
            "find_files",
            "Find files by name glob such as '*.py' or 'src/**/test_*.py'.",
            _obj({"glob": S, "path": S}, ["glob"]),
            _h_find_files,
            ACCESS_READ,
            lambda a: a.get("glob", ""),
        ),
        ToolDef(
            "find_symbol",
            "Heuristically locate where a function/class/constant is defined.",
            _obj({"name": S, "path": S}, ["name"]),
            _h_find_symbol,
            ACCESS_READ,
            lambda a: a.get("name", ""),
        ),
        ToolDef("git_status", "Show branch and working-tree status.", _obj({}), _h_git_status, ACCESS_READ),
        ToolDef(
            "git_diff",
            "Show uncommitted changes (optionally for one path or staged only).",
            _obj({"path": S, "staged": B}),
            _h_git_diff,
            ACCESS_READ,
            lambda a: a.get("path", ""),
        ),
        ToolDef(
            "create_file",
            "Create a new file (fails if it exists unless overwrite=true).",
            _obj({**path_arg, "content": S, "overwrite": B}, ["path", "content"]),
            _h_create_file,
            ACCESS_WRITE,
            lambda a: a.get("path", ""),
        ),
        ToolDef(
            "edit_file",
            "Replace exact text in an existing file. old_text must be unique unless replace_all=true. Read the file first.",
            _obj(
                {**path_arg, "old_text": S, "new_text": S, "replace_all": B, "expected_hash": S},
                ["path", "old_text", "new_text"],
            ),
            _h_edit_file,
            ACCESS_WRITE,
            lambda a: a.get("path", ""),
        ),
        ToolDef(
            "apply_patch",
            "Apply a unified diff (---/+++/@@ hunks) atomically across files.",
            _obj({"patch": S}, ["patch"]),
            _h_apply_patch,
            ACCESS_WRITE,
            lambda a: "unified diff",
        ),
        ToolDef(
            "move_file",
            "Move or rename a file (refuses to overwrite).",
            _obj({"source": S, "destination": S}, ["source", "destination"]),
            _h_move_file,
            ACCESS_WRITE,
            lambda a: f"{a.get('source')} -> {a.get('destination')}",
        ),
        ToolDef(
            "delete_file",
            "Delete a file (requires user approval; a backup is kept).",
            _obj(path_arg, ["path"]),
            _h_delete_file,
            ACCESS_WRITE,
            lambda a: a.get("path", ""),
        ),
        ToolDef(
            "run_command",
            "Run a command in the workspace. Passed as an argument list unless shell=true. Subject to the permission policy and may need user approval.",
            _obj({"command": S, "cwd": S, "timeout": INT, "shell": B}, ["command"]),
            _h_run_command,
            ACCESS_EXEC,
            lambda a: _clip(a.get("command", ""), 90),
        ),
        ToolDef(
            "run_tests",
            "Run the project's tests (auto-detected) and record the result as task validation.",
            _obj({"command": S, "target": S, "timeout": INT}),
            _h_run_tests,
            ACCESS_EXEC,
            lambda a: a.get("command") or a.get("target", ""),
        ),
        ToolDef(
            "plan",
            "Create ordered tasks for this job. Do this first for any multi-step request.",
            _obj({"steps": {"type": "array", "items": S}, "sequential": B}, ["steps"]),
            _h_plan,
            ACCESS_META,
            lambda a: f"{len(a.get('steps', []))} steps",
        ),
        ToolDef(
            "task_update",
            "Change a task's status (in_progress, completed, failed, blocked, cancelled). Completion requires prior verification.",
            _obj(
                {
                    "task": S,
                    "status": {"type": "string", "enum": [s.value for s in TaskStatus]},
                    "note": S,
                    "next_action": S,
                },
                ["task", "status"],
            ),
            _h_task_update,
            ACCESS_META,
            lambda a: f"{a.get('task')} -> {a.get('status')}",
        ),
        ToolDef(
            "verify_task",
            "Run a real check that proves a task is done: file_exists, file_contains, or command (exit 0). Required before completing a task unless run_tests passed.",
            _obj(
                {
                    "task": S,
                    "check": {"type": "string", "enum": ["file_exists", "file_contains", "command"]},
                    "path": S,
                    "text": S,
                    "command": S,
                },
                ["task", "check"],
            ),
            _h_verify_task,
            ACCESS_EXEC,
            lambda a: f"task {a.get('task')} {a.get('check')}",
        ),
        ToolDef(
            "ask_user",
            "Ask the user a clarifying question when a requirement materially affects the result.",
            _obj({"question": S}, ["question"]),
            _h_ask_user,
            ACCESS_META,
            lambda a: _clip(a.get("question", ""), 60),
        ),
        ToolDef(
            "finish",
            "Declare the whole job done with a short summary of what changed and what is unverified.",
            _obj({"summary": S}, ["summary"]),
            _h_finish,
            ACCESS_META,
            lambda a: _clip(a.get("summary", ""), 60),
        ),
    ]
    for tool in tools:
        registry.register(tool)


READ_TOOLS = {"list_dir", "read_file", "search", "find_files", "find_symbol", "git_status", "git_diff"}
