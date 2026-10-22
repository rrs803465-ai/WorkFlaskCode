from __future__ import annotations

import hashlib
import importlib.util
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workflaskcode.errors import WorkFlaskError
from workflaskcode.state.db import Database

PERMISSIONS = {
    "commands": "add @commands",
    "tools": "add read-only agent tools",
    "tools:write": "add agent tools that modify files",
    "tools:exec": "add agent tools that run commands",
    "templates": "add project templates",
    "audit": "add audit checks (they read project files)",
    "hooks": "run code before/after agent tasks",
}
HOOK_EVENTS = {"before_task", "after_task"}
NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,39}$"


class PluginError(WorkFlaskError):
    pass


@dataclass
class PluginInfo:
    name: str
    version: str
    description: str
    path: Path
    entry: Path
    permissions: list[str]
    digest: str
    consented: bool = False
    enabled: bool = False
    loaded: bool = False
    error: str | None = None
    registered: dict[str, list[str]] = field(default_factory=dict)


class PluginAPI:
    def __init__(self, manager: PluginManager, info: PluginInfo) -> None:
        self._m = manager
        self._info = info

    def _need(self, permission: str) -> None:
        if permission not in self._info.permissions:
            raise PluginError(
                f"plugin '{self._info.name}' did not declare permission '{permission}' in plugin.toml"
            )

    def _note(self, kind: str, name: str) -> None:
        self._info.registered.setdefault(kind, []).append(name)

    def add_command(
        self, name: str, handler: Callable[..., Any], help: str = "", aliases: tuple[str, ...] = ()
    ) -> None:
        self._need("commands")
        self._m.commands.append(
            {
                "name": name,
                "handler": handler,
                "help": help,
                "aliases": aliases,
                "source": f"plugin:{self._info.name}",
            }
        )
        self._note("commands", name)

    def add_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., str],
        access: str = "read",
    ) -> None:
        needed = {"read": "tools", "write": "tools:write", "exec": "tools:exec"}.get(access)
        if needed is None:
            raise PluginError("tool access must be 'read', 'write' or 'exec'")
        self._need(needed)
        self._m.tools.append(
            {
                "name": f"{self._info.name}_{name}",
                "description": description,
                "parameters": parameters,
                "handler": handler,
                "access": access,
                "source": f"plugin:{self._info.name}",
            }
        )
        self._note("tools", f"{self._info.name}_{name}")

    def add_template(self, spec: Any) -> None:
        self._need("templates")
        self._m.templates.append(spec)
        self._note("templates", getattr(spec, "name", "?"))

    def add_audit_check(self, name: str, fn: Callable[[Path], list[dict[str, Any]]]) -> None:
        self._need("audit")
        self._m.audit_checks.append((f"plugin:{self._info.name}/{name}", fn))
        self._note("audit", name)

    def add_hook(self, event: str, fn: Callable[..., None]) -> None:
        self._need("hooks")
        if event not in HOOK_EVENTS:
            raise PluginError(f"unknown hook '{event}'; choose from {sorted(HOOK_EVENTS)}")
        self._m.hooks.setdefault(event, []).append((self._info.name, fn))
        self._note("hooks", event)


class PluginManager:
    def __init__(self, plugins_dir: Path, db: Database) -> None:
        self.dir = plugins_dir
        self.db = db
        self.commands: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.templates: list[Any] = []
        self.audit_checks: list[tuple[str, Callable[[Path], list[dict[str, Any]]]]] = []
        self.hooks: dict[str, list[tuple[str, Callable[..., None]]]] = {}
        self.infos: dict[str, PluginInfo] = {}
        self.errors: dict[str, str] = {}

    def discover(self) -> list[PluginInfo]:
        self.infos = {}
        if not self.dir.is_dir():
            return []
        import re

        for folder in sorted(self.dir.iterdir()):
            manifest = folder / "plugin.toml"
            if not folder.is_dir() or not manifest.is_file():
                continue
            try:
                raw = tomllib.loads(manifest.read_text(encoding="utf-8"))
                name = str(raw["name"])
                if not re.match(NAME_RE, name):
                    raise PluginError("name must be a lowercase slug")
                entry = (folder / str(raw.get("entry", "plugin.py"))).resolve()
                if folder.resolve() not in entry.parents:
                    raise PluginError("entry must be inside the plugin folder")
                if not entry.is_file():
                    raise PluginError(f"entry file {entry.name} not found")
                perms = [str(p) for p in raw.get("permissions", [])]
                unknown = [p for p in perms if p not in PERMISSIONS]
                if unknown:
                    raise PluginError(f"unknown permissions: {', '.join(unknown)}")
                digest = hashlib.sha256(manifest.read_bytes() + entry.read_bytes()).hexdigest()
                record = self.db.kv_get("plugins", name) or {}
                info = PluginInfo(
                    name,
                    str(raw.get("version", "0")),
                    str(raw.get("description", "")),
                    folder,
                    entry,
                    perms,
                    digest,
                    consented=record.get("digest") == digest and bool(record.get("consented")),
                    enabled=bool(record.get("enabled")) and record.get("digest") == digest,
                )
                self.infos[name] = info
            except (KeyError, OSError, ValueError, PluginError) as exc:
                self.errors[folder.name] = f"{type(exc).__name__}: {exc}"
        return list(self.infos.values())

    def describe(self, name: str) -> str:
        info = self._get(name)
        lines = [
            f"{info.name} {info.version} - {info.description}",
            f"code: {info.entry}",
            "requested permissions:",
        ]
        lines += [f"  {p}: {PERMISSIONS[p]}" for p in info.permissions] or ["  (none)"]
        lines.append(
            "Plugins run as Python code inside this process with your user's privileges. Only enable plugins you have read."
        )
        return "\n".join(lines)

    def _get(self, name: str) -> PluginInfo:
        if name not in self.infos:
            raise PluginError(f"unknown plugin '{name}'", hint="run @plugins to list discovered plugins")
        return self.infos[name]

    def enable(self, name: str, *, consent: bool) -> PluginInfo:
        info = self._get(name)
        if not consent:
            raise PluginError("enabling a plugin requires explicit consent to its permissions")
        self.db.kv_set("plugins", name, {"digest": info.digest, "consented": True, "enabled": True})
        info.consented = info.enabled = True
        return info

    def disable(self, name: str) -> PluginInfo:
        info = self._get(name)
        record = self.db.kv_get("plugins", name) or {}
        self.db.kv_set("plugins", name, {**record, "enabled": False})
        info.enabled = False
        self.unload(name)
        return info

    def unload(self, name: str) -> None:
        self.commands = [c for c in self.commands if c["source"] != f"plugin:{name}"]
        self.tools = [t for t in self.tools if t["source"] != f"plugin:{name}"]
        self.audit_checks = [c for c in self.audit_checks if not c[0].startswith(f"plugin:{name}/")]
        self.hooks = {k: [h for h in v if h[0] != name] for k, v in self.hooks.items()}
        info = self.infos.get(name)
        if info:
            info.loaded = False
            info.registered = {}
        sys.modules.pop(f"workflaskcode_plugin_{name}", None)

    def load_enabled(self) -> list[str]:
        loaded: list[str] = []
        for info in self.discover():
            if not (info.enabled and info.consented):
                continue
            try:
                self._load(info)
                loaded.append(info.name)
            except Exception as exc:
                self.unload(info.name)
                info.error = f"{type(exc).__name__}: {exc}"
                self.errors[info.name] = info.error
        return loaded

    def _load(self, info: PluginInfo) -> None:
        module_name = f"workflaskcode_plugin_{info.name}"
        spec = importlib.util.spec_from_file_location(module_name, info.entry)
        if spec is None or spec.loader is None:
            raise PluginError("cannot import plugin entry")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        register = getattr(module, "register", None)
        if not callable(register):
            raise PluginError("plugin must define register(api)")
        register(PluginAPI(self, info))
        info.loaded = True
        info.error = None

    def fire(self, event: str, **payload: Any) -> list[str]:
        problems: list[str] = []
        for name, fn in list(self.hooks.get(event, [])):
            try:
                fn(**payload)
            except Exception as exc:
                problems.append(f"{name}: {type(exc).__name__}: {exc}")
        return problems
