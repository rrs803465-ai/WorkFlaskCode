from __future__ import annotations

import difflib
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from workflaskcode.errors import WorkFlaskError

if TYPE_CHECKING:
    from workflaskcode.app import Runtime
    from workflaskcode.ui.console import Console

COMMAND_RE = re.compile(r"^@([a-z][a-z0-9-]*)(?:\s+(.*))?$", re.S)


class CommandError(WorkFlaskError):
    pass


class Interaction(Protocol):
    def ask(self, prompt: str, *, secret: bool = False, default: str | None = None) -> str | None: ...

    def confirm(self, question: str, default: bool = False) -> bool: ...

    def choose(
        self, title: str, items: Sequence[Any], render: Callable[[Any], str] | None = None
    ) -> Any | None: ...


class ScriptedInteraction:
    def __init__(self, answers: Sequence[Any] = (), *, default_confirm: bool = False) -> None:
        self.answers = list(answers)
        self.default_confirm = default_confirm
        self.log: list[str] = []

    def _next(self) -> Any:
        return self.answers.pop(0) if self.answers else None

    def ask(self, prompt: str, *, secret: bool = False, default: str | None = None) -> str | None:
        self.log.append(f"ask: {prompt}")
        value = self._next()
        return default if value in (None, "") and default is not None else value

    def confirm(self, question: str, default: bool = False) -> bool:
        self.log.append(f"confirm: {question}")
        value = self._next()
        if value is None:
            return self.default_confirm if default is False else default
        return bool(value) if not isinstance(value, str) else value.lower() in ("y", "yes", "true")

    def choose(
        self, title: str, items: Sequence[Any], render: Callable[[Any], str] | None = None
    ) -> Any | None:
        self.log.append(f"choose: {title}")
        value = self._next()
        if isinstance(value, int) and 0 <= value < len(items):
            return items[value]
        return value if value in items else None


@dataclass
class Command:
    name: str
    handler: Callable[[CommandContext], None]
    help: str
    usage: str = ""
    group: str = "general"
    aliases: tuple[str, ...] = ()
    source: str = "core"
    needs_project: bool = False
    examples: tuple[str, ...] = ()


@dataclass
class CommandContext:
    rt: Runtime
    console: Console
    interaction: Interaction
    raw: str
    args: list[str]
    registry: CommandRegistry
    flags: dict[str, str | bool] = field(default_factory=dict)
    positional: list[str] = field(default_factory=list)

    @property
    def sub(self) -> str | None:
        return self.positional[0].lower() if self.positional else None

    def rest_after_sub(self) -> str:
        parts = self.raw.split(None, 1)
        return parts[1] if len(parts) > 1 else ""

    def flag(self, name: str, default: Any = None) -> Any:
        return self.flags.get(name, default)

    def need(self, count: int, usage: str) -> list[str]:
        if len(self.positional) < count:
            raise CommandError(f"missing argument. usage: {usage}")
        return self.positional

    def project(self):
        return self.rt.require_project()

    def int_flag(self, name: str, default: int) -> int:
        value = self.flags.get(name)
        if value in (None, True):
            return default
        try:
            return int(str(value))
        except ValueError as exc:
            raise CommandError(f"--{name} expects a number") from exc


def split_args(raw: str) -> tuple[list[str], dict[str, str | bool], list[str]]:
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    flags: dict[str, str | bool] = {}
    positional: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and len(tok) > 2:
            key, eq, value = tok[2:].partition("=")
            if eq:
                flags[key] = value
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--") and key in VALUE_FLAGS:
                flags[key] = tokens[i + 1]
                i += 1
            else:
                flags[key] = True
        else:
            positional.append(tok)
        i += 1
    return tokens, flags, positional


VALUE_FLAGS = {
    "cmd",
    "timeout",
    "runs",
    "warmup",
    "keyword",
    "file",
    "due",
    "tag",
    "version",
    "out",
    "format",
    "base",
    "limit",
    "since",
    "sort",
    "min-context",
    "provider",
    "label",
    "depth",
    "path",
    "glob",
    "category",
    "severity",
    "to",
    "name",
    "template",
    "addons",
    "title",
    "stack",
    "max",
    "role",
    "model",
    "key",
    "env",
    "base-url",
    "kind",
    "bump",
    "date",
    "description",
    "branch",
}


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._aliases: dict[str, str] = {}

    def register(self, command: Command) -> None:
        for name in (command.name, *command.aliases):
            owner = self._aliases.get(name) or (name if name in self._commands else None)
            if owner and self._commands[owner].source != command.source:
                raise CommandError(
                    f"command @{name} already exists (provided by {self._commands[owner].source})"
                )
        self._commands[command.name] = command
        for alias in command.aliases:
            self._aliases[alias] = command.name

    def unregister_source(self, source: str) -> None:
        for name in [n for n, c in self._commands.items() if c.source == source]:
            for alias in self._commands[name].aliases:
                self._aliases.pop(alias, None)
            del self._commands[name]

    def get(self, name: str) -> Command | None:
        name = name.lower()
        return self._commands.get(self._aliases.get(name, name))

    def all(self) -> list[Command]:
        return sorted(self._commands.values(), key=lambda c: (c.group, c.name))

    def names(self) -> list[str]:
        return sorted(self._commands)

    def suggest(self, name: str) -> list[str]:
        return difflib.get_close_matches(name.lower(), [*self._commands, *self._aliases], n=3, cutoff=0.55)

    def complete(self, text: str, line: str) -> list[tuple[str, str]]:
        stripped = line.lstrip()
        if not stripped.startswith("@") or " " in stripped:
            sub = self._subcompletions(stripped)
            return sub
        prefix = stripped[1:].lower()
        out = []
        for cmd in self.all():
            for n in (cmd.name, *cmd.aliases):
                if n.startswith(prefix):
                    out.append((f"@{n}", cmd.help))
        return out

    def _subcompletions(self, stripped: str) -> list[tuple[str, str]]:
        head, _, tail = stripped.partition(" ")
        cmd = self.get(head.lstrip("@"))
        if cmd is None or not cmd.usage:
            return []
        subs = re.findall(r"[\[<(|]\s*([a-z][a-z-]+)", cmd.usage)
        last = tail.split(" ")[-1] if tail else ""
        return [(s, "") for s in dict.fromkeys(subs) if s.startswith(last) and s != cmd.name]

    @staticmethod
    def parse(line: str) -> tuple[str, str] | None:
        stripped = line.strip()
        if stripped.startswith("\\@"):
            return None
        m = COMMAND_RE.match(stripped)
        if not m:
            return None
        return m.group(1), (m.group(2) or "").strip()

    def dispatch(self, rt: Runtime, console: Console, interaction: Interaction, line: str) -> bool:
        parsed = self.parse(line)
        if parsed is None:
            return False
        name, raw = parsed
        cmd = self.get(name)
        if cmd is None:
            hint = self.suggest(name)
            console.error(
                f"unknown command @{name}",
                hint=("did you mean " + ", ".join("@" + h for h in hint) + "?")
                if hint
                else "type @help for the list of commands",
            )
            return True
        tokens, flags, positional = split_args(raw)
        ctx = CommandContext(rt, console, interaction, raw, tokens, self, flags, positional)
        try:
            if cmd.needs_project:
                rt.require_project()
            cmd.handler(ctx)
        except WorkFlaskError as exc:
            console.error(str(exc), hint=exc.hint)
        except KeyboardInterrupt:
            console.dim("interrupted")
        except Exception as exc:
            console.error(
                f"@{cmd.name} failed unexpectedly: {type(exc).__name__}: {exc}",
                hint="run with --verbose or see @doctor; this is a bug if it persists",
            )
        return True


def build_registry(extra: Sequence[dict[str, Any]] = ()) -> CommandRegistry:
    from workflaskcode.commands import (
        design_cmds,
        extras,
        general,
        intel,
        models_cmds,
        planning,
        project_cmds,
        update_cmd,
    )

    registry = CommandRegistry()
    for module in (general, models_cmds, planning, intel, project_cmds, design_cmds, extras, update_cmd):
        module.register(registry)
    for item in extra:
        handler = item["handler"]

        def wrapped(ctx: CommandContext, _h=handler) -> None:
            _h(ctx.rt, ctx.raw)

        registry.register(
            Command(
                item["name"],
                wrapped,
                item.get("help", ""),
                group="plugin",
                aliases=tuple(item.get("aliases", ())),
                source=item["source"],
            )
        )
    return registry
