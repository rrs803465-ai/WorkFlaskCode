from __future__ import annotations

from workflaskcode.commands.registry import Command, CommandContext, CommandError, CommandRegistry
from workflaskcode.design.lock import DesignLock, scan_project
from workflaskcode.utils.redact import redact

SEVERITY_TOKEN = {"critical": "err", "high": "err", "medium": "warn", "low": "muted", "info": "muted"}


def _lock(ctx: CommandContext) -> DesignLock:
    ctx.project()
    assert ctx.rt.design is not None
    return ctx.rt.design


def _summary(ctx: CommandContext, data: dict) -> None:
    c = ctx.console
    scan = data["scan"]
    rows = [
        ("languages", ", ".join(scan["languages"]) or "-"),
        ("frameworks", ", ".join(scan["frameworks"]) or "-"),
        (
            "design tokens",
            f"{len(scan['tokens'])} ({', '.join(list(scan['tokens'])[:6])})"
            if scan["tokens"]
            else "none found",
        ),
        ("colors", ", ".join(scan["colors"][:10]) or "none found"),
        ("fonts", ", ".join(scan["fonts"][:6]) or "none found"),
        ("breakpoints", ", ".join(scan["breakpoints"][:8]) or "none found"),
        ("component dirs", ", ".join(scan["component_dirs"][:6]) or "none"),
        ("routes", str(len(scan["routes"]))),
        ("tables", ", ".join(scan["tables"][:8]) or "none"),
        ("naming", ", ".join(f"{k} {v}" for k, v in scan["naming"].items()) or "-"),
        ("tests", ", ".join(scan["test_commands"]) or "none"),
    ]
    c.kv(rows, title="design lock")
    for key, label in (("rules", "rules"), ("prohibited", "prohibited"), ("decisions", "decisions")):
        if data[key]:
            c.line(c.p.paint(label, "accent"))
            c.lines(f"  - {item}" for item in data[key])


def cmd_design_lock(ctx: CommandContext) -> None:
    c = ctx.console
    lock = _lock(ctx)
    sub = ctx.sub
    info = ctx.rt.info()
    if sub in ("rule", "prohibit", "decision"):
        text = " ".join(ctx.positional[1:])
        if not text:
            raise CommandError(f"usage: @design-lock {sub} <text>")
        try:
            lock.add_rule({"rule": "rules", "prohibit": "prohibited", "decision": "decisions"}[sub], text)
        except ValueError as exc:
            raise CommandError(str(exc), hint="run @design-lock first") from exc
        c.ok(f"{sub} recorded; the agent sees it in every task")
        return
    if sub == "unlock":
        if not lock.locked:
            c.dim("the design is not locked")
            return
        if ctx.interaction.confirm(
            "remove the design lock? The agent will no longer be told to preserve the current design. The file is kept with an .unlocked suffix.",
            False,
        ):
            lock.unlock()
            c.ok("design unlocked")
        return
    existing = lock.load()
    if sub == "show" or (existing and not ctx.flag("update")):
        if not existing:
            c.dim("the design is not locked. Run @design-lock to capture the current design.")
            return
        _summary(ctx, existing)
        c.dim(
            "@design-lock --update rescans and keeps your rules; @design-diff shows drift; @design-audit checks code against the lock"
        )
        return
    scan = scan_project(ctx.project(), info)
    if not scan.has_ui and not scan.routes and not scan.tables:
        c.warn("no UI tokens, routes or tables were detected; the lock will record structure and naming only")
    if existing:
        drift = lock.diff(info)
        c.heading("scan since the last lock", f"{len(drift)} difference(s)")
        c.lines(f"  {line}" for line in drift)
        if not ctx.interaction.confirm(
            "replace the locked snapshot with the current state? Your rules are kept.", False
        ):
            return
    data = lock.lock(info)
    c.ok("design locked. Rules are in PROJECT.md (managed block) and sent to the agent with each task.")
    _summary(ctx, data)
    c.dim(
        "Add rules with @design-lock rule <text>, @design-lock prohibit <text>, @design-lock decision <text>."
    )


def cmd_design_diff(ctx: CommandContext) -> None:
    c = ctx.console
    lock = _lock(ctx)
    if not lock.locked:
        raise CommandError("no design lock to compare against", hint="run @design-lock first")
    changes = lock.diff(ctx.rt.info())
    if not changes:
        c.ok("no design drift: the project still matches the locked snapshot")
        return
    c.heading("design drift", f"{len(changes)} difference(s)")
    for line in changes:
        c.line("  " + line)
    c.dim(
        "If the changes are intended: @design-lock --update. Otherwise revert them (@diff shows what changed)."
    )


def cmd_design_audit(ctx: CommandContext) -> None:
    c = ctx.console
    lock = _lock(ctx)
    if not lock.locked:
        raise CommandError("no design lock to audit against", hint="run @design-lock first")
    findings = lock.audit(ctx.rt.info())
    c.heading("design audit", f"{len(findings)} finding(s)")
    if not findings:
        c.ok("no violations of the locked design were found")
        return
    for f in findings[: ctx.int_flag("limit", 40)]:
        c.line(f"{c.p.paint(f.severity.upper().ljust(8), SEVERITY_TOKEN[f.severity])} {f.id}  {f.location}")
        c.line(f"         {redact(f.evidence)[:150]}")
        c.line(c.p.paint(f"         fix: {f.fix[:150]}", "muted"))
    c.dim(
        "Checks compare literal colors, fonts, breakpoints, naming and prohibited patterns; they cannot judge visual quality."
    )


def register(reg: CommandRegistry) -> None:
    add = lambda *a, **k: reg.register(Command(*a, **k))
    add(
        "design-lock",
        cmd_design_lock,
        "lock the current design/architecture so the agent preserves it",
        "[show|--update|rule <t>|prohibit <t>|decision <t>|unlock]",
        "design",
        needs_project=True,
    )
    add(
        "design-audit",
        cmd_design_audit,
        "check the code against the locked design",
        "[--limit n]",
        "design",
        needs_project=True,
    )
    add("design-diff", cmd_design_diff, "show drift from the locked design", "", "design", needs_project=True)
