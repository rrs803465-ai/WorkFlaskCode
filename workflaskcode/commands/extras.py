from __future__ import annotations

import shlex
import shutil
import time
from pathlib import Path

from workflaskcode.browser.preview import BrowserUnavailable, DevServer, browser_check, playwright_status
from workflaskcode.commands.registry import Command, CommandContext, CommandError, CommandRegistry
from workflaskcode.plugins.manager import PluginError

_SERVERS: dict[str, DevServer] = {}


def cmd_plugins(ctx: CommandContext) -> None:
    c = ctx.console
    manager = ctx.rt.plugins
    sub = ctx.sub or "list"
    if sub == "list":
        infos = manager.discover()
        if not infos and not manager.errors:
            c.dim(f"no plugins found in {ctx.rt.home.plugins_dir}")
            c.dim(
                "A plugin is a folder with plugin.toml and plugin.py exposing register(api). See docs/plugins.md."
            )
            return
        rows = [
            [
                i.name,
                i.version,
                "enabled" if i.enabled else "disabled",
                "yes" if i.consented else "no",
                ", ".join(i.permissions) or "-",
                i.description[:40],
            ]
            for i in infos
        ]
        c.table(["plugin", "version", "state", "consented", "permissions", "description"], rows)
        for name, problem in manager.errors.items():
            c.warn(f"{name}: {problem}")
        c.dim(
            "Plugins run inside this process with your privileges. They are permission-gated, not sandboxed."
        )
        return
    name = ctx.need(2, f"plugins {sub} <name>")[1]
    manager.discover()
    try:
        if sub in ("describe", "info", "show"):
            c.line(manager.describe(name))
        elif sub == "enable":
            c.line(manager.describe(name))
            if not ctx.interaction.confirm(
                f"enable '{name}' and grant these permissions? Changing the plugin's code later revokes consent.",
                False,
            ):
                c.info("not enabled")
                return
            manager.enable(name, consent=True)
            manager.load_enabled()
            info = manager.infos[name]
            if info.error:
                c.warn(f"enabled but failed to load: {info.error}")
            else:
                c.ok(
                    f"{name} enabled; registered: "
                    + (", ".join(f"{k} {','.join(v)}" for k, v in info.registered.items()) or "nothing")
                )
        elif sub == "disable":
            manager.disable(name)
            c.ok(f"{name} disabled and unloaded")
        else:
            raise CommandError("usage: @plugins [list|describe <name>|enable <name>|disable <name>]")
    except PluginError as exc:
        raise CommandError(str(exc)) from exc
    if sub in ("enable", "disable"):
        ctx.rt.refresh_info()


def cmd_skills(ctx: CommandContext) -> None:
    c = ctx.console
    skills = ctx.rt.skills
    sub = ctx.sub or "list"
    if sub == "show":
        name = ctx.need(2, "skills show <name>")[1]
        skill = skills.get(name)
        if skill is None:
            raise CommandError(f"unknown skill '{name}'")
        c.markdown(skill.read(), "main", f"{skill.name} ({skill.source})")
        return
    if sub != "list":
        raise CommandError("usage: @skills [list|show <name>]")
    items = skills.list()
    if not items:
        c.dim("no skills installed")
        return
    c.table(["skill", "source", "description"], [[s.name, s.source, s.description[:70]] for s in items])
    c.dim(
        f"Skills are instruction files the agent can read with `read_skill`; they cannot run code. Add your own as <name>/SKILL.md in {ctx.rt.home.skills_dir} or .workflaskcode/skills."
    )


def _detect_dev_command(ctx: CommandContext) -> tuple[list[str], int]:
    info = ctx.rt.info()
    if ctx.flag("cmd") not in (None, True):
        argv = shlex.split(str(ctx.flag("cmd")))
    elif info.dev_commands:
        argv = list(info.dev_commands[0])
    else:
        raise CommandError(
            "no dev server command detected",
            hint="pass one: @preview start --cmd 'python3 -m http.server 8000' --port 8000",
        )
    port = ctx.int_flag("port", 0)
    if not port:
        joined = " ".join(argv)
        port = (
            8000
            if "http.server" in joined or "flask" in joined or "uvicorn" in joined
            else 5000
            if "flask" in joined
            else 5173
            if "vite" in joined
            else 3000
        )
        for tok in argv:
            if tok.isdigit() and 1024 <= int(tok) <= 65535:
                port = int(tok)
    return argv, port


def cmd_preview(ctx: CommandContext) -> None:
    c = ctx.console
    root = ctx.project()
    key = str(root)
    server = _SERVERS.get(key)
    sub = ctx.sub or "status"
    if sub == "status":
        ok, message = playwright_status()
        c.kv(
            [
                ("dev server", f"running at {server.url}" if server and server.running else "not running"),
                ("browser testing", message if not ok else "Playwright ready"),
            ],
            title="preview",
        )
        c.dim("@preview start | stop | check [url] [--selector css]... [--screenshot]")
        return
    if sub == "start":
        if server and server.running:
            c.ok(f"already running at {server.url}")
            return
        argv, port = _detect_dev_command(ctx)
        decision = ctx.rt.policy.evaluate(argv=argv, cwd=root)
        if decision.action == "deny":
            raise CommandError(f"blocked by the permission policy: {decision.reason}")
        if not ctx.interaction.confirm(
            f"start `{shlex.join(argv)}` and wait for 127.0.0.1:{port}? It runs until @preview stop or exit.",
            False,
        ):
            return
        server = DevServer(root, argv, port=port)
        with c.spinner("starting dev server"):
            url = server.start(ctx.rt.cancel_event)
        _SERVERS[key] = server
        c.ok(f"serving at {url} (loopback only)")
        return
    if sub == "stop":
        if not server:
            c.dim("no dev server was started from this session")
            return
        server.stop()
        _SERVERS.pop(key, None)
        c.ok("dev server stopped")
        return
    if sub == "check":
        url = (
            ctx.positional[1]
            if len(ctx.positional) > 1
            else (server.url if server and server.running else None)
        )
        if not url:
            raise CommandError("no URL: pass one or start a server with @preview start")
        if not url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")) and not ctx.flag(
            "external"
        ):
            raise CommandError(
                "browser checks are limited to localhost by default",
                hint="add --external to check another URL",
            )
        selectors = [s for s in str(ctx.flag("selector", "")).split("|") if s] if ctx.flag("selector") else []
        shot = (
            ctx.rt.home.reports_dir / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
            if ctx.flag("screenshot")
            else None
        )
        try:
            with c.spinner("loading page in headless Chromium"):
                report = browser_check(url, selectors=selectors, screenshot=shot)
        except BrowserUnavailable as exc:
            raise CommandError(str(exc)) from exc
        for line in report.render().splitlines():
            c.line("  " + line)
        (c.ok if report.ok else c.warn)("page check passed" if report.ok else "page check found problems")
        return
    raise CommandError("usage: @preview [status|start|stop|check [url]]")


def cmd_data(ctx: CommandContext) -> None:
    c = ctx.console
    home = ctx.rt.home
    sub = ctx.sub or "paths"
    if sub == "paths":
        c.kv(
            [
                ("agent home", str(home.root)),
                ("config", str(home.config_file)),
                ("database", str(home.db_path)),
                ("reports", str(home.reports_dir)),
                ("logs", str(home.logs_dir)),
                ("project metadata", str(ctx.rt.meta.directory) if ctx.rt.meta else "-"),
                ("telemetry", "none; nothing is sent anywhere except your configured model provider"),
            ],
            title="where WorkFlaskCode stores data",
        )
        return
    if sub == "export":
        target = Path(
            ctx.positional[1]
            if len(ctx.positional) > 1
            else home.reports_dir / f"data-export-{time.strftime('%Y%m%d')}.zip"
        ).expanduser()
        staging = home.cache_dir / "export-staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for name in ("sessions", "memory", "reports"):
            src = getattr(home, f"{name}_dir")
            if src.is_dir():
                shutil.copytree(src, staging / name, dirs_exist_ok=True)
        if home.config_file.is_file():
            shutil.copy2(home.config_file, staging / "config.toml")
        archive = shutil.make_archive(str(target.with_suffix("")), "zip", staging)
        shutil.rmtree(staging, ignore_errors=True)
        c.ok(
            f"exported to {archive}. API keys are never included: they live in your environment, keyring or credentials file."
        )
        return
    if sub == "delete":
        what = ctx.positional[1] if len(ctx.positional) > 1 else ""
        if what == "sessions":
            if ctx.interaction.confirm(
                "delete ALL stored conversations, tasks, checkpoints and usage records in the agent database? Project files are untouched. This cannot be undone.",
                False,
            ):
                ctx.rt.sessions.delete_all()
                c.ok("session data deleted; restart to begin a fresh session")
            return
        if what == "reports":
            if ctx.interaction.confirm(f"delete everything in {home.reports_dir}?", False):
                shutil.rmtree(home.reports_dir, ignore_errors=True)
                home.reports_dir.mkdir(parents=True, exist_ok=True)
                c.ok("reports deleted")
            return
        if what == "cache":
            shutil.rmtree(home.cache_dir, ignore_errors=True)
            home.cache_dir.mkdir(parents=True, exist_ok=True)
            c.ok("cache deleted (model lists will be fetched again)")
            return
        raise CommandError(
            "usage: @data delete sessions|reports|cache",
            hint="credentials are removed with @providers key remove <provider>",
        )
    raise CommandError("usage: @data [paths|export [file]|delete sessions|reports|cache]")


def register(reg: CommandRegistry) -> None:
    add = lambda *a, **k: reg.register(Command(*a, **k))
    add(
        "plugins",
        cmd_plugins,
        "list, inspect, enable (with consent) or disable plugins",
        "[list|describe <n>|enable <n>|disable <n>]",
        "extensions",
    )
    add("skills", cmd_skills, "list or show skills the agent can read", "[list|show <name>]", "extensions")
    add(
        "preview",
        cmd_preview,
        "run the dev server and check the page in headless Chromium",
        "[status|start|stop|check [url] [--selector 'a|b'] [--screenshot]]",
        "extensions",
        ("browser",),
        needs_project=True,
    )
    add(
        "data",
        cmd_data,
        "where data lives; export or delete it",
        "[paths|export [file]|delete sessions|reports|cache]",
        "extensions",
    )
