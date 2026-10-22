from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflaskcode import __version__
from workflaskcode.config.credentials import CredentialStore
from workflaskcode.config.paths import AgentHome
from workflaskcode.config.store import ConfigStore
from workflaskcode.errors import ConfigError, WorkFlaskError
from workflaskcode.state.db import Database
from workflaskcode.ui.console import Console

if TYPE_CHECKING:
    from workflaskcode.app import Runtime

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _module(name: str) -> tuple[bool, str]:
    try:
        mod = importlib.import_module(name)
    except ImportError:
        return False, ""
    return True, str(getattr(mod, "__version__", getattr(mod, "VERSION", "")))


def _tool_version(argv: list[str]) -> str | None:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return "installed"
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0][:60] if text else "installed"


def collect(
    home: AgentHome, console: Console | None = None, *, rt: Runtime | None = None, online: bool = False
) -> list[Check]:
    checks: list[Check] = []
    add = checks.append
    py_ok = sys.version_info >= (3, 11)
    add(
        Check(
            "python",
            OK if py_ok else FAIL,
            f"{sys.version.split()[0]} ({sys.executable})",
            "" if py_ok else "install Python 3.11 or newer",
        )
    )
    ok, ver = _module("pydantic")
    add(
        Check(
            "pydantic", OK if ok else FAIL, ver or "missing", "" if ok else "pip install -r requirements.txt"
        )
    )
    for mod, why in (
        ("rich", "extra rendering"),
        ("prompt_toolkit", "multi-line editing and completion menu"),
        ("keyring", "OS credential store"),
        ("playwright", "browser preview/testing"),
    ):
        ok, ver = _module(mod)
        add(
            Check(
                mod,
                OK if ok else WARN,
                (ver or "installed") if ok else f"not installed ({why}; optional)",
                "" if ok else f"pip install {mod}",
            )
        )
    git_v = _tool_version(["git", "--version"])
    add(
        Check(
            "git",
            OK if git_v else WARN,
            git_v or "not found; git features disabled",
            "" if git_v else "install git",
        )
    )
    for tool in ("ruff", "bandit", "semgrep", "pip-audit", "node", "npm"):
        v = _tool_version([tool, "--version"])
        add(Check(tool, OK if v else WARN, v or "not installed (optional)", ""))
    add(_home_check(home))
    settings = None
    store = ConfigStore(home)
    if store.exists():
        try:
            settings = store.load()
            add(
                Check(
                    "config",
                    OK,
                    f"{store.path} valid; {len(settings.providers)} provider(s); coding model {settings.coding_model or 'unset'}",
                )
            )
        except ConfigError as exc:
            add(Check("config", FAIL, str(exc), exc.hint or "run: python3 workflaskcode.py setup"))
    else:
        add(Check("config", WARN, "no config.toml yet", "run: python3 workflaskcode.py setup"))
    if settings is not None:
        creds = CredentialStore(home)
        for name, cfg in settings.providers.items():
            cred = creds.get(name, cfg)
            add(
                Check(
                    f"credential:{name}",
                    OK if cred else WARN,
                    f"{cred.masked} from {cred.source}" if cred else "no API key found",
                    ""
                    if cred
                    else f"set it with @providers key {name} or export {cfg.env_var or name.upper() + '_API_KEY'}",
                )
            )
            if online and cred:
                add(_online_check(rt, settings, name))
        if not creds.keyring_available:
            add(
                Check(
                    "keyring",
                    WARN,
                    "no usable OS keyring backend; keys come from environment variables or an explicit 0600 file",
                    "",
                )
            )
    add(_db_check(home))
    add(_terminal_check(console))
    if rt is not None and rt.root is not None:
        add(_workspace_check(rt))
    return checks


def _home_check(home: AgentHome) -> Check:
    if not home.root.is_dir():
        return Check("agent home", WARN, f"{home.root} does not exist yet (created on first run)")
    try:
        with tempfile.NamedTemporaryFile(dir=home.root, delete=True):
            pass
    except OSError as exc:
        return Check(
            "agent home",
            FAIL,
            f"{home.root} is not writable: {exc}",
            "fix the permissions or set WORKFLASKCODE_HOME",
        )
    if os.name == "posix":
        mode = home.root.stat().st_mode & 0o777
        if mode & 0o077:
            return Check(
                "agent home",
                WARN,
                f"{home.root} is accessible by other users (mode {oct(mode)})",
                f"chmod 700 {home.root}",
            )
    return Check("agent home", OK, str(home.root))


def _db_check(home: AgentHome) -> Check:
    if not home.db_path.exists():
        return Check("state database", WARN, "not created yet")
    try:
        db = Database(home.db_path)
        row = db.one("PRAGMA integrity_check")
        version = db.version
        db.close()
    except Exception as exc:
        return Check(
            "state database",
            FAIL,
            f"{type(exc).__name__}: {exc}",
            "move state/*.db aside to start fresh (task history will be lost)",
        )
    ok = row is not None and row[0] == "ok"
    return Check(
        "state database", OK if ok else FAIL, f"integrity {row[0] if row else '?'}; schema v{version}"
    )


def _terminal_check(console: Console | None) -> Check:
    if console is None:
        return Check("terminal", OK, "not inspected")
    caps = console.caps
    depth = {0: "no colour", 1: "16 colours", 2: "256 colours", 3: "truecolor"}.get(
        caps.color, str(caps.color)
    )
    return Check(
        "terminal",
        OK if caps.tty else WARN,
        f"{depth}, {'unicode' if caps.unicode else 'ascii'}, {caps.width}x{caps.height}, {'tty' if caps.tty else 'not a tty'}",
    )


def _workspace_check(rt: Runtime) -> Check:
    root = rt.root
    assert root is not None
    problems = []
    if not os.access(root, os.W_OK):
        problems.append("not writable")
    info = rt.info()
    detail = f"{root} ({info.summary()})"
    if problems:
        return Check("workspace", FAIL, f"{detail}; {', '.join(problems)}")
    return Check("workspace", OK, detail)


def _online_check(rt: Runtime | None, settings, name: str) -> Check:

    creds = CredentialStore(rt.home) if rt else None
    registry = rt.providers if rt else None
    if registry is None or creds is None:
        return Check(f"online:{name}", WARN, "runtime not available")
    try:
        provider = registry.get(name)
        result = provider.validate_credentials()
    except WorkFlaskError as exc:
        return Check(f"online:{name}", FAIL, str(exc))
    except Exception as exc:
        return Check(f"online:{name}", FAIL, f"{type(exc).__name__}: {exc}")
    return Check(
        f"online:{name}", OK if result.ok else (WARN if result.inconclusive else FAIL), result.detail
    )


def render(console: Console, checks: list[Check]) -> int:
    c = console
    marks = {OK: ("ok", c.r.c["ok"]), WARN: ("warn", c.r.c["warn"]), FAIL: ("err", c.r.c["err"])}
    c.heading("WorkFlaskCode doctor", f"v{__version__}")
    for chk in checks:
        token, glyph = marks[chk.status]
        c.line(
            f"  {c.p.paint(glyph, token, bold=True)} {c.p.paint(chk.name.ljust(22), 'text')} {c.p.paint(chk.detail, 'muted')}"
        )
        if chk.fix and chk.status != OK:
            c.line(f"      {c.p.paint(c.r.c['arrow'], 'dim')} {chk.fix}")
    fails = sum(1 for k in checks if k.status == FAIL)
    warns = sum(1 for k in checks if k.status == WARN)
    c.line()
    if fails:
        c.error(f"{fails} problem(s) need attention", hint="fix the failed checks above")
    else:
        c.ok(f"no blocking problems ({warns} optional item(s) not available)")
    return 1 if fails else 0
