from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING

from workflaskcode.commands.registry import CommandContext, CommandError
from workflaskcode.errors import PermissionDenied
from workflaskcode.tools.process import CommandResult, run_process
from workflaskcode.workspace.project import ProjectInfo

if TYPE_CHECKING:
    pass


def ago(ts: float) -> str:
    delta = max(time.time() - ts, 0)
    for limit, unit, div in ((90, "s", 1), (5400, "m", 60), (129600, "h", 3600)):
        if delta < limit:
            return f"{int(delta / div)}{unit} ago"
    return f"{int(delta / 86400)}d ago"


def run_user_command(
    ctx: CommandContext,
    argv: list[str],
    *,
    timeout: float | None = None,
    cwd: Path | None = None,
    confirm: bool = True,
) -> CommandResult:
    rt = ctx.rt
    root = rt.require_project()
    assert rt.policy is not None
    decision = rt.policy.evaluate(argv=argv, cwd=cwd or root)
    display = " ".join(shlex.quote(a) for a in argv)
    if decision.action == "deny":
        raise PermissionDenied(f"blocked by the permission policy: {decision.reason}")
    if decision.action == "ask" and confirm:
        if rt.interactive:
            approved = ctx.interaction.confirm(f"run `{display}`? ({decision.reason})", False)
        else:
            approved = rt.broker.auto_approve and decision.risk.name != "DESTRUCTIVE"
        if not approved:
            raise CommandError("not run: you did not approve the command")
    ctx.console.tool("run", display, status="run")
    result = run_process(
        argv=argv,
        cwd=cwd or root,
        timeout=timeout or rt.settings.permissions.command_timeout_seconds,
        max_output=rt.settings.permissions.max_output_bytes,
        cancel=rt.cancel_event,
    )
    return result


def agent_task(ctx: CommandContext, prompt: str, *, mode: str = "coding") -> None:
    rt = ctx.rt
    if rt.model_for(mode) is None:
        raise CommandError("no model is configured", hint="run @setup")
    if mode == "coding":
        if rt.mode != "coding":
            raise CommandError("this command lets the agent change files; switch with @codingagent first")
        if rt.policy is not None and rt.policy.mode.value == "read_only":
            raise CommandError("permission mode is read-only; change it with @permissions approve_risky")
    state = rt.submit(prompt, None if mode == rt.mode else mode)
    ctx.console.info(f"agent {state}" + (" (read-only analysis)" if mode == "chat" else ""))


def info_lines(info: ProjectInfo) -> list[tuple[str, str]]:
    return [
        ("languages", ", ".join(info.languages) or "none detected"),
        ("frameworks", ", ".join(info.frameworks) or "none detected"),
        ("package managers", ", ".join(info.package_managers) or "none"),
        ("test command", " ".join(info.test_commands[0]) if info.test_commands else "not detected"),
        ("format command", " ".join(info.format_commands[0]) if info.format_commands else "not detected"),
        ("type-check command", " ".join(info.type_commands[0]) if info.type_commands else "not detected"),
        ("git repository", "yes" if info.is_git else "no"),
    ]
