from __future__ import annotations

from pathlib import Path

from workflaskcode import __version__, doctor
from workflaskcode.commands.helpers import ago, info_lines
from workflaskcode.commands.registry import Command, CommandContext, CommandError, CommandRegistry
from workflaskcode.config.models import PermissionMode
from workflaskcode.config.store import set_dotted
from workflaskcode.templates import generate, get_template, list_templates
from workflaskcode.wizard import SetupWizard

GROUP_TITLES = {
    "general": "General",
    "models": "Models and providers",
    "planning": "Planning, tasks and safety",
    "intel": "Code intelligence",
    "project": "Project and release",
    "design": "Design lock",
    "modes": "Modes",
    "plugin": "Plugins",
    "extras": "Extensions and data",
}


def cmd_help(ctx: CommandContext) -> None:
    c = ctx.console
    if ctx.positional:
        cmd = ctx.registry.get(ctx.positional[0].lstrip("@"))
        if cmd is None:
            raise CommandError(f"no such command: {ctx.positional[0]}")
        c.heading(f"@{cmd.name}", cmd.help)
        if cmd.aliases:
            c.line(f"  aliases: {', '.join('@' + a for a in cmd.aliases)}")
        if cmd.usage:
            c.line(f"  usage:   @{cmd.name} {cmd.usage}")
        for ex in cmd.examples:
            c.line(f"  example: {ex}")
        return
    c.heading("WorkFlaskCode commands", f"v{__version__}")
    current = None
    for cmd in ctx.registry.all():
        if cmd.group != current:
            current = cmd.group
            c.line()
            c.line(c.p.paint(GROUP_TITLES.get(current, current.title()), "brand", bold=True))
        c.line(f"  {c.p.paint(('@' + cmd.name).ljust(18), 'accent')} {c.p.paint(cmd.help, 'muted')}")
    c.line()
    c.dim(
        "Plain text goes to the agent. @btw <question> asks without interrupting. Ctrl+C cancels the running task. @help <command> shows usage."
    )
    c.dim("Start a line with \\@ to send literal text that begins with @.")


def cmd_setup(ctx: CommandContext) -> None:
    if ctx.rt.worker_running:
        raise CommandError("a task is running; cancel it before changing configuration")
    result = SetupWizard(
        ctx.rt.home, ctx.console, ctx.interaction, http=ctx.rt.http, existing=ctx.rt.global_settings
    ).run()
    if result.saved:
        ctx.rt.reload_settings()
        ctx.rt.router.reset_exhaustion()
        ctx.console.ok("configuration reloaded")


def cmd_status(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    pairs = [
        ("mode", rt.mode),
        ("model", str(rt.model_for(rt.mode) or "not configured")),
        ("permissions", rt.policy.mode.value if rt.policy else rt.settings.permissions.mode.value),
    ]
    if rt.root:
        pairs += [("project", f"{rt.root.name}  ({rt.root})"), ("git", rt.git_line())]
    pairs.append(("worker", "running" if rt.worker_running else "idle"))
    if rt.side is not None:
        active = rt.side.active()
        pairs.append(("side chats", f"{len(active)} active" if active else "none active"))
    if rt.last_result is not None:
        r = rt.last_result
        pairs.append(("last task", f"{r.status}" + (f" ({r.error})" if r.error else "")))
    c.kv(pairs, title="status")
    if rt.ledger is not None:
        tasks = rt.ledger.list()
        if tasks:
            c.task_list(tasks)
        else:
            c.dim("no tasks yet")
    if rt.router.exhausted_providers:
        c.warn(
            "providers marked unavailable: "
            + ", ".join(f"{k} ({v})" for k, v in rt.router.exhausted_providers.items())
        )
    if rt.tracker.session_id:
        c.dim(rt.tracker.format_totals(rt.tracker.totals(session=True)))


def cmd_where(ctx: CommandContext) -> None:
    rt = ctx.rt
    home = rt.home
    pairs = [
        ("current directory", str(Path.cwd())),
        ("agent home", str(home.root)),
        ("config", str(home.config_file)),
        ("database", str(home.db_path)),
        ("logs", str(home.logs_dir)),
        ("backups", str(home.backups_dir)),
        ("reports", str(home.reports_dir)),
        ("plugins", str(home.plugins_dir)),
        ("skills", str(home.skills_dir)),
        ("cache", str(home.cache_dir)),
    ]
    if rt.root and rt.meta:
        pairs += [
            ("project root", str(rt.root)),
            ("project metadata", str(rt.meta.directory)),
            ("PROJECT.md", str(rt.meta.project_md)),
        ]
    ctx.console.kv(pairs, title="paths")


def cmd_workspace(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    root = rt.require_project()
    assert rt.guard is not None
    if ctx.sub == "add":
        target = Path(ctx.need(2, "workspace add <path>")[1]).expanduser()
        if not target.is_dir():
            raise CommandError(f"not a directory: {target}")
        resolved = target.resolve()
        if not ctx.interaction.confirm(
            f"allow the agent to read and write inside {resolved} in addition to the project?", False
        ):
            c.info("unchanged")
            return
        rt.apply_settings(
            lambda s: s.model_copy(
                update={
                    "permissions": s.permissions.model_copy(
                        update={"extra_roots": sorted({*s.permissions.extra_roots, str(resolved)})}
                    )
                }
            )
        )
        rt.guard.authorize_extra_root(resolved)
        c.ok(f"added {resolved}")
        return
    if ctx.sub == "remove":
        target = str(Path(ctx.need(2, "workspace remove <path>")[1]).expanduser().resolve())
        rt.apply_settings(
            lambda s: s.model_copy(
                update={
                    "permissions": s.permissions.model_copy(
                        update={"extra_roots": [p for p in s.permissions.extra_roots if p != target]}
                    )
                }
            )
        )
        rt.guard.extra_roots = [p for p in rt.guard.extra_roots if str(p) != target]
        c.ok(f"removed {target}")
        return
    pairs = [
        ("project", rt.project.name if rt.project else ""),
        ("root", str(root)),
        ("extra roots", ", ".join(map(str, rt.guard.extra_roots)) or "none"),
        ("protected", ", ".join(map(str, rt.guard.protected)) or "none"),
        (
            "metadata",
            f"{rt.project.metadata_location if rt.project else '?'}: {rt.meta.directory if rt.meta else ''}",
        ),
    ]
    pairs += info_lines(rt.info())
    c.kv(pairs, title="workspace")
    c.dim(
        "File tools resolve every path inside these roots; '..', absolute paths outside them, and symlinks that escape are refused. .git and the agent home are never writable."
    )


def cmd_projects(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    if ctx.sub == "remove":
        ref = ctx.need(2, "projects remove <id|name>")[1]
        rec = rt.projects.find(ref)
        if rec is None:
            raise CommandError(f"unknown project '{ref}'")
        if rt.project and rec.id == rt.project.id:
            raise CommandError("that project is currently open")
        if ctx.interaction.confirm(
            f"forget {rec.name} ({rec.path})? Files on disk are not touched; saved sessions for it are removed.",
            False,
        ):
            rt.projects.remove(rec.id)
            c.ok("removed from the registry")
        return
    rows = []
    for rec in rt.projects.list():
        mark = "*" if rt.project and rec.id == rt.project.id else " "
        rows.append(
            [
                mark,
                rec.id,
                rec.name,
                str(rec.path) + ("" if rec.path.is_dir() else "  (missing)"),
                ago(rec.last_opened),
            ]
        )
    if not rows:
        c.dim("no projects registered yet; use @open <path> or @new")
        return
    c.table(["", "id", "name", "path", "opened"], rows)


def cmd_open(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    ref = ctx.rest_after_sub() if False else " ".join(ctx.positional)
    if not ref:
        projects = [p for p in rt.projects.list() if p.path.is_dir()]
        pick = ctx.interaction.choose("Open which project?", projects, lambda p: f"{p.name}  ·  {p.path}")
        if pick is None:
            raise CommandError("usage: @open <path|id|name>")
        ref = str(pick.path)
    record = rt.projects.find(ref)
    target = record.path if record else Path(ref).expanduser()
    if not target.exists():
        raise CommandError(f"path does not exist: {target}", hint="use @projects to see registered projects")
    if not target.is_dir():
        raise CommandError(f"not a directory: {target}")
    rec = rt.open_project(target, new_session=bool(ctx.flag("new-session")))
    info = rt.info()
    c.ok(f"opened {rec.name}  ({rt.root})")
    c.dim(info.summary())
    if rt.ledger and rt.ledger.list():
        c.info(f"{len(rt.ledger.list())} saved task(s); @status to view, @resume to continue")


def cmd_new(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    if ctx.sub == "list":
        c.table(
            ["template", "description", "tests"],
            [
                [t.name, t.description, " ".join(t.test_command) if t.test_command else "none"]
                for t in list_templates()
            ],
        )
        return
    description = " ".join(ctx.positional)
    name = ctx.flag("name") or ""
    if not name:
        name = ctx.interaction.ask("project name", default=None) or ""
    if not name:
        raise CommandError("a project name is required (use --name)")
    template_name = ctx.flag("template")
    if template_name is None or template_name is True:
        words = {w.lower().strip(".,") for w in description.split()}
        matches = [t for t in list_templates() if words & set(t.keywords)]
        pool = matches or list_templates()
        spec = ctx.interaction.choose(
            "Template" + (" (suggested from your description)" if matches else ""),
            pool,
            lambda t: f"{t.name}  ·  {t.description}",
        )
        if spec is None:
            raise CommandError("no template chosen")
    else:
        spec = get_template(str(template_name))
    base = (
        Path(str(ctx.flag("path"))).expanduser()
        if ctx.flag("path") and ctx.flag("path") is not True
        else Path(rt.settings.default_project_root or Path.cwd())
    )
    dest = (base / name).resolve()
    if rt.home.root in dest.parents or dest == rt.home.root:
        raise CommandError("projects cannot be created inside the agent home")
    addons = [a for a in str(ctx.flag("addons", "")).split(",") if a] if ctx.flag("addons") else []
    dest.mkdir(parents=True, exist_ok=True)
    result = generate(spec, dest, name, {"title": name}, addons=addons)
    if result.conflicts:
        raise CommandError(
            f"{dest} already contains files the template would overwrite: {', '.join(result.conflicts[:5])}",
            hint="choose an empty folder; nothing was written",
        )
    c.ok(f"created {len(result.written)} files in {dest}")
    for rel in result.written[:25]:
        c.line(f"  {c.r.c['ok']} {rel}")
    from workflaskcode.git import GitRepo

    if (
        rt.settings.git.enabled
        and GitRepo.available()
        and ctx.interaction.confirm("initialise a git repository (no commit is made)?", True)
    ):
        GitRepo(dest).init()
    rt.open_project(dest, new_session=True)
    if spec.unverified:
        c.warn("not verified by WorkFlaskCode: " + "; ".join(spec.unverified))
    if spec.test_command:
        c.dim("test command: " + " ".join(spec.test_command))
    if spec.run_hint:
        c.dim("run: " + spec.run_hint)
    if (
        description
        and rt.model_for(rt.mode)
        and rt.mode == "coding"
        and ctx.interaction.confirm("have the agent customise it to your description now?", True)
    ):
        rt.submit(
            f"The project was scaffolded from the '{spec.name}' template. Adapt it to this request without rewriting unrelated files, then run the tests:\n\n{description}"
        )
        c.info("agent started")


def cmd_resume(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    if ctx.sub == "list":
        rows = [
            [s.id, s.title or "(untitled)", s.mode, s.status, ago(s.updated)]
            for s in rt.sessions.list(rt.project.id if rt.project else None, 15)
        ]
        if not rows:
            c.dim("no saved sessions")
            return
        c.table(["session", "title", "mode", "status", "updated"], rows)
        return
    if ctx.positional and rt.sessions.get(ctx.positional[0]):
        s = rt.resume_session(ctx.positional[0])
        c.ok(f"switched to session {s.id}")
        c.info("run @resume to continue unfinished tasks")
        return
    c.info(f"resume: {rt.resume_task()}")


def cmd_history(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    query = " ".join(ctx.positional)
    if not query:
        cmd_resume_list(ctx)
        return
    hits = rt.sessions.search(
        query, rt.project.id if rt.project and not ctx.flag("all") else None, ctx.int_flag("limit", 20)
    )
    if not hits:
        c.dim("no matches")
        return
    for h in hits:
        snippet = " ".join(h["content"].split())
        pos = snippet.lower().find(query.lower())
        start = max(pos - 40, 0)
        c.line(
            f"  {c.p.paint(h['session_id'], 'dim')} {c.p.paint(h['role'], 'accent')}  {snippet[start : start + 110]}"
        )


def cmd_resume_list(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    rows = [
        [s.id, s.title or "(untitled)", s.mode, s.status, ago(s.updated)]
        for s in rt.sessions.list(rt.project.id if rt.project else None, 15)
    ]
    c.table(["session", "title", "mode", "status", "updated"], rows) if rows else c.dim("no saved sessions")


def cmd_clear(ctx: CommandContext) -> None:
    ctx.console.clear()
    ctx.console.dim("screen cleared; nothing was deleted (history is kept, @history searches it)")


def cmd_exit(ctx: CommandContext) -> None:
    ctx.rt.exit_requested = True


def cmd_doctor(ctx: CommandContext) -> None:
    doctor.render(
        ctx.console, doctor.collect(ctx.rt.home, ctx.console, rt=ctx.rt, online=bool(ctx.flag("online")))
    )


def cmd_chat(ctx: CommandContext) -> None:
    rt = ctx.rt
    rt.set_mode("chat")
    ctx.console.ok(
        "chat mode: read-only questions. Your project and unfinished tasks are untouched. Return with @codingagent."
    )
    if rt.settings.chat_model is None and rt.settings.coding_model:
        ctx.console.dim(f"no separate chat model configured; using {rt.settings.coding_model}")
    if rt.worker_running:
        ctx.console.warn("a coding task is still running; new prompts will queue behind it")
    text = " ".join(ctx.positional)
    if text:
        ctx.console.info(f"chat {rt.submit(text)}")


def cmd_codingagent(ctx: CommandContext) -> None:
    rt = ctx.rt
    rt.set_mode("coding")
    mode = rt.policy.mode if rt.policy else rt.settings.permissions.mode
    ctx.console.ok(f"coding agent mode. Permission mode: {mode.value}")
    if mode is PermissionMode.READ_ONLY:
        ctx.console.warn(
            "permission mode is read-only: the agent can inspect but not change anything. Use @permissions to change it."
        )
    text = " ".join(ctx.positional)
    if text:
        ctx.console.info(f"agent {rt.submit(text)}")


def cmd_btw(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    rt.require_project()
    assert rt.side is not None
    if ctx.sub == "cancel":
        number = int(ctx.positional[1]) if len(ctx.positional) > 1 and ctx.positional[1].isdigit() else None
        done = rt.side.cancel(number)
        c.info(
            f"cancelled side question(s): {', '.join('#' + str(n) for n in done)}"
            if done
            else "no side question to cancel"
        )
        return
    if ctx.sub == "list":
        rows = [[f"#{j.number}", j.status, j.question[:60]] for j in rt.side.jobs.values()]
        c.table(["id", "status", "question"], rows) if rows else c.dim("no side questions yet")
        return
    if ctx.sub == "clear":
        rt.side.reset()
        c.ok("side conversation history cleared (the main task is unaffected)")
        return
    job = rt.ask_side(ctx.raw)
    c.dim(f"btw #{job.number} asked; the main task is not interrupted" + ("" if rt.worker_running else ""))


def cmd_cancel(ctx: CommandContext) -> None:
    if ctx.rt.cancel("cancel"):
        ctx.console.info("cancelling the task; state will be saved")
    else:
        ctx.console.dim("no task is running")


def cmd_pause(ctx: CommandContext) -> None:
    if ctx.rt.cancel("pause"):
        ctx.console.info(
            "pausing: the current step stops safely and progress is saved. Continue with @resume."
        )
    else:
        ctx.console.dim("no task is running")


def cmd_config(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    sub = ctx.sub or "show"
    if sub == "path":
        c.line(str(rt.config.path))
    elif sub == "show":
        from workflaskcode.config import toml_writer

        c.panel(
            "effective configuration (no credentials)",
            toml_writer.dumps(rt.settings.to_toml_dict()).splitlines(),
        )
    elif sub == "get":
        key = ctx.need(2, "config get <dotted.key>")[1]
        cursor = rt.settings.to_toml_dict()
        for part in key.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                raise CommandError(f"no such setting: {key}")
            cursor = cursor[part]
        c.line(str(cursor))
    elif sub == "set":
        pos = ctx.need(3, "config set <dotted.key> <value>")
        key, value = pos[1], " ".join(pos[2:])
        if key.startswith("providers.") and (
            "key" in key.lower() or "token" in key.lower() or "secret" in key.lower()
        ):
            raise CommandError("credentials cannot be stored in configuration; use @providers key")
        try:
            updated = set_dotted(rt.global_settings, key, value)
        except (ValueError, KeyError) as exc:
            raise CommandError(f"cannot set {key}: {exc}") from exc
        rt.config.save(updated)
        rt.reload_settings()
        c.ok(f"{key} = {value}")
    else:
        raise CommandError("usage: @config [show|get <key>|set <key> <value>|path]")


def cmd_theme(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    names = ["auto", "dark", "light", "plain"]
    if not ctx.positional:
        c.line("theme: " + rt.settings.ui.theme + "   available: " + ", ".join(names))
        return
    name = ctx.positional[0].lower()
    if name not in names:
        raise CommandError(f"unknown theme; choose from {', '.join(names)}")
    c.set_theme(name if name != "auto" else "dark")
    rt.apply_settings(lambda s: s.model_copy(update={"ui": s.ui.model_copy(update={"theme": name})}))
    c.ok(f"theme set to {name}")


def register(reg: CommandRegistry) -> None:
    add = lambda *a, **k: reg.register(Command(*a, **k))
    add(
        "help",
        cmd_help,
        "list commands and examples",
        "[command]",
        "general",
        ("?",),
        examples=("@help audit",),
    )
    add("setup", cmd_setup, "reopen the configuration wizard", group="general")
    add("status", cmd_status, "current work, tasks, model and usage", group="general", aliases=("st",))
    add("where", cmd_where, "print every relevant absolute path", group="general")
    add(
        "workspace",
        cmd_workspace,
        "active project and workspace boundaries",
        "[add <path>|remove <path>]",
        "general",
    )
    add("projects", cmd_projects, "list registered projects", "[remove <id>]", "general")
    add(
        "open",
        cmd_open,
        "switch to another project after validating it",
        "<path|id|name> [--new-session]",
        "general",
        examples=("@open ~/code/site",),
    )
    add(
        "new",
        cmd_new,
        "create a project from a template or description",
        "[list] [description] --name N [--template T] [--path DIR] [--addons docker,ci]",
        "general",
        examples=("@new a landing page --name site --template static-web",),
    )
    add(
        "resume",
        cmd_resume,
        "continue the saved task, or switch to a saved session",
        "[list|<session-id>]",
        "general",
        needs_project=True,
    )
    add("history", cmd_history, "search previous sessions", "<text> [--all] [--limit N]", "general")
    add("clear", cmd_clear, "clear the visible terminal (keeps all data)", group="general", aliases=("cls",))
    add("exit", cmd_exit, "save state and quit", group="general", aliases=("quit",))
    add(
        "doctor",
        cmd_doctor,
        "diagnose configuration, dependencies, workspace and providers",
        "[--online]",
        "general",
    )
    add("chat", cmd_chat, "read-only conversation mode", "[question]", "modes")
    add("codingagent", cmd_codingagent, "enable the coding-agent workflow", "[request]", "modes", ("code",))
    add(
        "btw",
        cmd_btw,
        "ask a side question without interrupting the main task",
        "<question> | list | cancel [n] | clear",
        "modes",
        needs_project=True,
        examples=("@btw why SQLite here?",),
    )
    add("cancel", cmd_cancel, "stop the running task and save its state", group="modes")
    add("pause", cmd_pause, "pause the running task safely", group="modes")
    add(
        "config",
        cmd_config,
        "show or change configuration",
        "[show|get <key>|set <key> <value>|path]",
        "general",
    )
    add("theme", cmd_theme, "change the colour theme", "[auto|dark|light|plain]", "general")
