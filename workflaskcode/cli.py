from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from workflaskcode import __version__
from workflaskcode import doctor as doctor_module
from workflaskcode.app import Runtime
from workflaskcode.commands.registry import CommandRegistry, build_registry
from workflaskcode.config.paths import AgentHome
from workflaskcode.config.store import ConfigStore
from workflaskcode.errors import WorkFlaskError
from workflaskcode.logs import setup_logging
from workflaskcode.ui.console import Console
from workflaskcode.ui.picker import pick
from workflaskcode.ui.reader import LineReader
from workflaskcode.wizard import SetupWizard
from workflaskcode.workspace.project import detect_project

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_PAUSED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130
RESERVED = {"run", "doctor"}


class TerminalInteraction:
    def __init__(self, console: Console, reader: LineReader | None, interactive: bool) -> None:
        self.console = console
        self.reader = reader
        self.interactive = interactive

    def ask(self, prompt: str, *, secret: bool = False, default: str | None = None) -> str | None:
        if not self.interactive:
            return default
        suffix = f" [{default}]" if default else ""
        text = f"{self.console.p.paint('?', 'accent', bold=True)} {prompt}{suffix}: "
        try:
            if secret:
                value = getpass.getpass(f"? {prompt}: ")
            elif self.reader is not None:
                value = self.reader.read(text)
                if value is None:
                    return None
            else:
                value = input(text)
        except (EOFError, KeyboardInterrupt):
            self.console.line()
            return None
        value = value.strip()
        return value if value else default

    def confirm(self, question: str, default: bool = False) -> bool:
        if not self.interactive:
            return default
        hint = "Y/n" if default else "y/N"
        for _ in range(3):
            answer = self.ask(f"{question} [{hint}]")
            if answer is None:
                return False
            if not answer:
                return default
            if answer.lower() in ("y", "yes"):
                return True
            if answer.lower() in ("n", "no"):
                return False
            self.console.dim("please answer y or n")
        return default

    def choose(
        self, title: str, items: Sequence[Any], render: Callable[[Any], str] | None = None
    ) -> Any | None:
        if not self.interactive or not items:
            return None
        reader = (lambda q: self.reader.read(q)) if self.reader is not None else None
        try:
            return pick(self.console, items, title=title, render=render, reader=reader)
        except (EOFError, KeyboardInterrupt):
            return None


def build_parser(registry: CommandRegistry) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 workflaskcode.py",
        description="WorkFlaskCode: a terminal AI coding agent. Run with no command for the interactive session.",
        epilog="Any @command works as a subcommand, e.g. `python3 workflaskcode.py audit --json` or `python3 workflaskcode.py models --free`. "
        "Inside a session, type @help.",
    )
    parser.add_argument("--version", action="version", version=f"WorkFlaskCode {__version__}")
    parser.add_argument(
        "--home",
        metavar="DIR",
        help="agent home directory (default: platform-specific, or $WORKFLASKCODE_HOME)",
    )
    parser.add_argument(
        "--project",
        "-p",
        metavar="DIR",
        help="project directory to open (default: the current directory if it looks like a project)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="non-interactive runs: approve non-destructive actions automatically",
    )
    parser.add_argument("--plain", action="store_true", help="no colors, no animation, ASCII only")
    parser.add_argument("--verbose", "-v", action="store_true", help="show extra diagnostics")
    parser.add_argument("--mode", choices=["chat", "coding", "plan"], default=None, help="mode for `run`")
    parser.add_argument(
        "command",
        nargs="?",
        help="run | doctor | any @command name without the @ (setup, models, audit, ...)",
    )
    parser.add_argument(
        "rest", nargs=argparse.REMAINDER, help="arguments for the command or the prompt for `run`"
    )
    return parser


def _sync_plugin_commands(
    rt: Runtime, registry: CommandRegistry, seen: tuple[str, ...]
) -> tuple[CommandRegistry, tuple[str, ...]]:
    names = tuple(sorted(c["source"] + ":" + c["name"] for c in rt.plugins.commands))
    if names == seen:
        return registry, seen
    return build_registry(rt.plugins.commands), names


def _open_initial_project(rt: Runtime, console: Console, requested: str | None) -> None:
    if requested:
        rec = rt.open_project(requested)
        console.ok(f"opened {rec.name}")
        return
    cwd = Path.cwd().resolve()
    if (
        cwd == rt.home.root
        or rt.home.root in cwd.parents
        or cwd == Path(cwd.anchor)
        or cwd == Path.home().resolve()
    ):
        return
    if detect_project(cwd).is_existing_project:
        rt.open_project(cwd)


def _banner(rt: Runtime, console: Console, notes: list[str]) -> None:
    model = rt.model_for(rt.mode)
    pairs = [
        ("model", str(model) if model else "not configured (run @setup)"),
        ("permissions", rt.settings.permissions.mode.value.replace("_", " ")),
    ]
    if rt.root:
        pairs.insert(0, ("project", f"{rt.root.name}  {rt.root}"))
        pairs.append(("git", rt.git_line()))
        info = rt.info()
        pairs.append(("detected", info.summary()))
        if rt.ledger and any(
            t.status.value in ("pending", "in_progress", "paused") for t in rt.ledger.list()
        ):
            notes.append("unfinished tasks from an earlier session; @status to view, @resume to continue")
    else:
        pairs.insert(0, ("project", "none open. @open <path> or @new <description>"))
    pairs.append(("data", f"{rt.home.root}"))
    console.show_banner(__version__, pairs, notes)


def _first_run(rt_home: AgentHome, console: Console, interaction: TerminalInteraction) -> bool:
    store = ConfigStore(rt_home)
    if store.exists():
        return False
    console.line()
    console.info(
        "first run: no configuration found. The setup wizard takes about a minute and can be repeated with @setup."
    )
    if not interaction.confirm("run setup now?", True):
        console.dim("skipped. Run @setup before giving the agent work.")
        return False
    result = SetupWizard(rt_home, console, interaction).run()
    return result.saved


def interactive_session(
    rt: Runtime,
    console: Console,
    interaction: TerminalInteraction,
    reader: LineReader,
    registry: CommandRegistry,
) -> int:
    seen: tuple[str, ...] = ()
    registry, seen = _sync_plugin_commands(rt, registry, seen)
    last_interrupt = 0.0
    while not rt.exit_requested:
        registry, seen = _sync_plugin_commands(rt, registry, seen)
        reader.completer = registry.complete
        try:
            line = reader.read(console.prompt_text(approving=rt.broker.has_pending()))
        except KeyboardInterrupt:
            console.line()
            if rt.worker_running:
                rt.cancel("Ctrl+C")
                console.dim("cancelling the running task; progress is saved")
                continue
            if time.monotonic() - last_interrupt < 2.0:
                break
            last_interrupt = time.monotonic()
            console.dim("press Ctrl+C again or type @exit to leave")
            continue
        if line is None:
            console.line()
            break
        text = line.strip()
        if not text:
            continue
        if rt.broker.has_pending() and not text.startswith("@"):
            if rt.broker.answer(text):
                continue
        if registry.dispatch(rt, console, interaction, text):
            continue
        if text.startswith("/"):
            console.dim(
                "commands start with @ (try @help); send a leading '/' as text with a space before it"
            )
            continue
        try:
            state = rt.submit(text)
        except WorkFlaskError as exc:
            console.error(str(exc), hint=exc.hint)
            continue
        if state != "started":
            console.dim(state)
    return EXIT_OK


def run_headless(rt: Runtime, console: Console, prompt: str, mode: str | None) -> int:
    if not prompt.strip():
        console.error("run needs a prompt", hint='python3 workflaskcode.py run "describe the task"')
        return EXIT_USAGE
    if rt.root is None:
        console.error("no project: run from a project directory or pass --project DIR")
        return EXIT_USAGE
    try:
        rt.set_mode("chat" if mode == "chat" else "coding")
        rt.submit(prompt, "plan" if mode == "plan" else None)
    except WorkFlaskError as exc:
        console.error(str(exc), hint=exc.hint)
        return EXIT_USAGE
    try:
        while not rt.wait(0.5):
            pass
    except KeyboardInterrupt:
        rt.cancel("Ctrl+C")
        rt.wait(15)
        return EXIT_INTERRUPTED
    result = rt.last_result
    if result is None:
        return EXIT_FAILED
    return {
        "completed": EXIT_OK,
        "cancelled": EXIT_INTERRUPTED,
        "paused": EXIT_PAUSED,
        "limit": EXIT_PAUSED,
    }.get(result.status, EXIT_FAILED)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser(build_registry())
    args = parser.parse_args(list(argv) if argv is not None else None)
    plain = args.plain or bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM") == "dumb"
    console = Console(plain=plain, verbose=args.verbose)
    try:
        home = AgentHome.at(args.home)
    except OSError as exc:
        console.error(f"cannot use that home directory: {exc}")
        return EXIT_USAGE
    stdin_tty = sys.stdin.isatty() and sys.stdout.isatty()
    if args.command == "doctor":
        online = "--online" in args.rest
        return doctor_module.render(console, doctor_module.collect(home, console, rt=None, online=online))
    prompt = " ".join(args.rest) if args.command == "run" else ""
    one_shot = args.command is not None
    interactive = stdin_tty and not (args.command == "run")
    try:
        home.ensure()
    except OSError as exc:
        console.error(f"cannot create the agent home at {home.root}: {exc}")
        return EXIT_FAILED
    setup_logging(home, ConfigStore(home).load_or_default().logging_level)
    reader = LineReader(console, history_path=home.state_dir / "input_history") if stdin_tty else None
    interaction = TerminalInteraction(console, reader, interactive=stdin_tty and args.command != "run")
    if not one_shot and stdin_tty:
        _first_run(home, console, interaction)
    rt = Runtime(home, console, interactive=interactive, auto_approve=args.yes)
    if reader is not None:
        reader.toolbar = lambda: console.status_line(rt.status_info())
    code = EXIT_OK
    try:
        try:
            _open_initial_project(rt, console, args.project)
        except WorkFlaskError as exc:
            console.error(str(exc), hint=exc.hint)
            if args.project:
                return EXIT_USAGE
        registry = build_registry(rt.plugins.commands)
        if args.command == "run":
            code = run_headless(rt, console, prompt, args.mode)
        elif args.command:
            if args.command not in {c.name for c in registry.all()} | {
                a for c in registry.all() for a in c.aliases
            }:
                hints = registry.suggest(args.command)
                console.error(
                    f"unknown command '{args.command}'",
                    hint=("did you mean " + ", ".join(hints) + "?")
                    if hints
                    else "see: python3 workflaskcode.py --help",
                )
                return EXIT_USAGE
            line = "@" + args.command + (" " + " ".join(_quote(a) for a in args.rest) if args.rest else "")
            registry.dispatch(rt, console, interaction, line)
            rt.wait()
        else:
            if not stdin_tty:
                console.error(
                    "the interactive session needs a terminal",
                    hint='for scripts use: python3 workflaskcode.py run "task" --yes',
                )
                return EXIT_USAGE
            _banner(rt, console, [])
            code = interactive_session(rt, console, interaction, reader, registry)
    finally:
        rt.shutdown()
        if reader is not None:
            reader.close()
    return code


def _quote(token: str) -> str:
    import shlex

    return shlex.quote(token)
