from __future__ import annotations

import shutil
import time

from workflaskcode import __version__
from workflaskcode.commands.registry import Command, CommandContext, CommandError, CommandRegistry
from workflaskcode.errors import WorkFlaskError
from workflaskcode.update import (
    UpdateError,
    apply_tree,
    check_for_update,
    download_archive,
    extract_archive,
    git_pull,
    install_root,
    latest_backup,
    validate_tree,
)
from workflaskcode.update.checker import fetch_changelog, write_state
from workflaskcode.update.installer import apply_with_rollback


def _info(ctx: CommandContext):
    rt = ctx.rt
    try:
        with ctx.console.spinner(f"checking {rt.settings.updates.repo}"):
            return check_for_update(
                rt.http, rt.settings.updates, rt.home.root, rt.update_endpoints, timeout=10.0
            )
    except WorkFlaskError as exc:
        raise CommandError(str(exc)) from exc


def _show(ctx: CommandContext, info) -> None:
    ctx.console.kv(
        [
            ("installed", info.local),
            (
                "upstream",
                f"{info.remote}  ({info.repo} @ {info.branch}{', ' + info.sha[:10] if info.sha else ''})",
            ),
            ("status", "update available" if info.newer else "up to date"),
        ],
        title="update",
    )


def _rollback(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    backup = latest_backup(rt.home.backups_dir)
    if backup is None:
        raise CommandError("no update backup found; nothing to roll back")
    if not ctx.flag("yes") and not ctx.interaction.confirm(
        f"restore the files saved in {backup.name}?", False
    ):
        return
    try:
        changed = apply_tree(backup, rt.update_root or install_root())
    except (UpdateError, OSError) as exc:
        raise CommandError(f"rollback failed: {exc}") from exc
    write_state(rt.home.root, sha=None)
    c.ok(f"restored {len(changed)} file(s) from {backup.name}. Restart WorkFlaskCode to load them.")


def cmd_update(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    sub = ctx.sub or "install"
    if sub == "rollback":
        _rollback(ctx)
        return
    if sub not in ("install", "check"):
        raise CommandError("usage: @update [check|rollback] [--yes] [--force]")
    if sub == "install" and rt.worker_running:
        raise CommandError("a task is running; wait for it or @cancel before updating")
    info = _info(ctx)
    _show(ctx, info)
    if sub == "check":
        c.dim("run @update to install" if info.newer else "nothing to do")
        return
    if not info.newer and not ctx.flag("force"):
        c.ok(f"already on the latest version ({info.local})")
        return
    notes = fetch_changelog(rt.http, rt.settings.updates, rt.update_endpoints, info.branch, info.local)
    for line in notes:
        c.line("  " + line)
    root = rt.update_root or install_root()
    if not ctx.flag("yes") and not ctx.interaction.confirm(
        "download and install it? Current files are backed up first and can be restored with @update rollback.",
        False,
    ):
        c.dim("update cancelled")
        return
    if (root / ".git").exists():
        try:
            with c.spinner("git pull --ff-only"):
                out = git_pull(root)
        except (UpdateError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        write_state(
            rt.home.root, version=info.remote, sha=info.sha, installed_at=time.strftime("%Y-%m-%dT%H:%M:%S")
        )
        c.ok("updated this git checkout")
        c.dim(out.splitlines()[-1] if out else "")
        c.warn(
            "restart WorkFlaskCode to load the new version (run pip install -r requirements.txt if requirements changed)"
        )
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = rt.home.cache_dir / f"update-{stamp}"
    ref = info.sha or f"refs/heads/{info.branch}"
    old_requirements = (
        (root / "requirements.txt").read_text(encoding="utf-8")
        if (root / "requirements.txt").is_file()
        else ""
    )
    try:
        with c.spinner("downloading"):
            data = download_archive(rt.http, f"{rt.update_endpoints.archive}/{info.repo}/tar.gz/{ref}")
        new_root, commit = extract_archive(data, work)
        version = validate_tree(new_root)
        backup = rt.home.backups_dir / f"update-{stamp}"
        with c.spinner("installing"):
            changed = apply_with_rollback(new_root, root, backup)
    except (UpdateError, OSError) as exc:
        raise CommandError(str(exc)) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    write_state(
        rt.home.root,
        version=version,
        sha=info.sha or commit,
        installed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        backup=str(backup),
    )
    c.ok(f"updated {__version__} -> {version}: {len(changed)} file(s) changed. Backup: {backup.name}")
    new_requirements = (
        (root / "requirements.txt").read_text(encoding="utf-8")
        if (root / "requirements.txt").is_file()
        else ""
    )
    if new_requirements != old_requirements:
        c.warn("requirements changed: run  pip install -r requirements.txt")
    c.warn("restart WorkFlaskCode to load the new version")


def cmd_tokens(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    sub = ctx.sub or "status"
    if sub in ("on", "off"):
        rt.apply_settings(
            lambda s: s.model_copy(update={"tokens": s.tokens.model_copy(update={"saver": sub == "on"})})
        )
        c.ok(f"token saver {'enabled' if sub == 'on' else 'disabled'}")
        return
    if sub != "status":
        raise CommandError("usage: @tokens [status|on|off]")
    cfg = rt.settings.tokens
    stats = rt.loop.saver.stats if rt.loop else None
    rows = [
        ("token saver", "on" if cfg.saver else "off"),
        ("recent turns kept in full", str(cfg.keep_recent_turns)),
        ("older tool output limit", f"{cfg.old_output_chars} chars"),
        ("web tools sent only when relevant", "yes" if cfg.gate_web_tools else "no"),
    ]
    if stats and stats.requests:
        rows += [
            ("requests this session", str(stats.requests)),
            ("input tokens before saver (est.)", f"{stats.raw_tokens + stats.trimmed_chars // 4:,}"),
            ("input tokens sent (est.)", f"{stats.sent_tokens:,}"),
            ("saved (est.)", f"{stats.saved_tokens:,}  ({stats.percent:.0f}%)"),
            ("tool schemas withheld", str(stats.tools_gated)),
        ]
    else:
        rows.append(("this session", "no requests yet"))
    c.kv(rows, title="tokens")
    c.dim("estimates use ~4 characters per token; @usage shows the provider-reported numbers")


def register(reg: CommandRegistry) -> None:
    reg.register(
        Command(
            "update",
            cmd_update,
            "check the GitHub repository for a new version and install it (with backup)",
            "[check|rollback] [--yes] [--force]",
            "system",
        )
    )
    reg.register(
        Command(
            "tokens",
            cmd_tokens,
            "show how many input tokens the context saver is cutting; turn it on or off",
            "[status|on|off]",
            "models",
        )
    )
