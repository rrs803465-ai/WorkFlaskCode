from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workflaskcode.config.models import Settings
from workflaskcode.errors import Cancelled, PathSecurityError, PermissionDenied, ToolError, WorkFlaskError
from workflaskcode.git import GitRepo
from workflaskcode.providers.types import ToolCall, ToolSpec
from workflaskcode.security.permissions import Approver, PermissionPolicy
from workflaskcode.state.db import Database
from workflaskcode.tasks.ledger import TaskLedger
from workflaskcode.tools.files import FileTools
from workflaskcode.ui.console import Console
from workflaskcode.utils.fs import sha256_file, short_id
from workflaskcode.utils.redact import redact
from workflaskcode.workspace.guard import WorkspaceGuard
from workflaskcode.workspace.project import ProjectInfo

ACCESS_READ = "read"
ACCESS_WRITE = "write"
ACCESS_EXEC = "exec"
ACCESS_META = "meta"

MAX_TOOL_OUTPUT = 24_000


@dataclass
class ToolContext:
    guard: WorkspaceGuard
    files: FileTools
    policy: PermissionPolicy
    approver: Approver
    db: Database
    session_id: str
    project_root: Path
    settings: Callable[[], Settings]
    ledger: TaskLedger
    git: GitRepo
    console: Console
    cancel: threading.Event
    project_info: Callable[[], ProjectInfo]
    asker: Callable[[str], str | None] = lambda q: None
    extra: dict[str, Any] = field(default_factory=dict)

    def current_task_id(self) -> str | None:
        task = self.ledger.current()
        return task.id if task else None


Handler = Callable[[ToolContext, dict[str, Any]], str]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    access: str
    summarize: Callable[[dict[str, Any]], str] = lambda a: ""
    source: str = "core"

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    props = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in args:
            return f"missing required argument '{name}'"
    for name, value in args.items():
        spec = props.get(name)
        if spec is None:
            return f"unknown argument '{name}'"
        expected = _TYPES.get(spec.get("type", ""))
        if expected is None:
            continue
        if spec.get("type") in ("integer", "number") and isinstance(value, bool):
            return f"argument '{name}' must be {spec['type']}"
        if not isinstance(value, expected):
            return f"argument '{name}' must be {spec['type']}"
        if "enum" in spec and value not in spec["enum"]:
            return f"argument '{name}' must be one of {spec['enum']}"
    return None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        if tool.name in self._tools and self._tools[tool.name].source != tool.source:
            raise WorkFlaskError(
                f"tool name '{tool.name}' is already registered by {self._tools[tool.name].source}"
            )
        self._tools[tool.name] = tool

    def unregister_source(self, source: str) -> None:
        for name in [n for n, t in self._tools.items() if t.source == source]:
            del self._tools[name]

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, allowed_access: set[str]) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values() if t.access in allowed_access]


def truncate_output(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n...[{len(text) - limit} characters omitted]...\n{tail}"


def args_hash(tool: str, args: dict[str, Any]) -> str:
    blob = json.dumps({"t": tool, "a": args}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


@dataclass
class ToolOutcome:
    call_id: str
    name: str
    content: str
    ok: bool
    duration_ms: float
    summary: str = ""
    skipped_duplicate: bool = False


class ToolExecutor:
    def __init__(
        self, registry: ToolRegistry, ctx: ToolContext, allowed_access: Callable[[], set[str]]
    ) -> None:
        self.registry = registry
        self.ctx = ctx
        self.allowed_access = allowed_access

    def reconcile_interrupted(self) -> list[str]:
        db = self.ctx.db
        rows = db.query(
            "SELECT * FROM tool_executions WHERE session_id=? AND status='started'", (self.ctx.session_id,)
        )
        notes = []
        for row in rows:
            verdict = self._reconcile_one(row["tool"], json.loads(row["args"]), row["expected"])
            status = (
                "done_reconciled"
                if verdict is True
                else "failed_reconciled"
                if verdict is False
                else "unknown"
            )
            db.execute(
                "UPDATE tool_executions SET status=?, finished=?, result=? WHERE id=?",
                (status, time.time(), f"reconciled after interruption: {status}", row["id"]),
            )
            notes.append(f"{row['tool']}: {status}")
        return notes

    def _reconcile_one(self, tool: str, args: dict[str, Any], expected: str | None) -> bool | None:
        try:
            if tool == "create_file":
                real = self.ctx.guard.resolve(args["path"])
                return real.is_file() and sha256_file(real) == expected
            if tool == "edit_file":
                real = self.ctx.guard.resolve(args["path"])
                if not real.is_file():
                    return False
                text = real.read_text(encoding="utf-8", errors="replace")
                return args["new_text"] in text and (
                    args["old_text"] not in text or args["old_text"] in args["new_text"]
                )
            if tool == "delete_file":
                return not self.ctx.guard.resolve(args["path"]).exists()
        except (PathSecurityError, KeyError, OSError):
            return None
        return None

    def _expected_for(self, tool: str, args: dict[str, Any]) -> str | None:
        if tool == "create_file" and isinstance(args.get("content"), str):
            return hashlib.sha256(args["content"].encode("utf-8")).hexdigest()
        return None

    def execute(self, call: ToolCall) -> ToolOutcome:
        started = time.monotonic()
        tool = self.registry.get(call.name)
        if tool is None:
            known = ", ".join(self.registry.names())
            return self._fail(call, started, f"unknown tool '{call.name}'. Available tools: {known}")
        if call.parse_error:
            return self._fail(call, started, f"could not parse arguments for {call.name}: {call.parse_error}")
        if tool.access not in self.allowed_access():
            return self._fail(
                call, started, f"tool '{call.name}' is not available in the current mode or permission policy"
            )
        problem = validate_args(tool.parameters, call.arguments)
        if problem:
            return self._fail(call, started, f"invalid arguments for {call.name}: {problem}")
        if self.ctx.cancel.is_set():
            return self._fail(call, started, "cancelled before the tool ran")
        ahash = args_hash(call.name, call.arguments)
        mutating = tool.access in (ACCESS_WRITE, ACCESS_EXEC)
        exec_id = short_id("x")
        if mutating:
            prior = self.ctx.db.one(
                "SELECT status FROM tool_executions WHERE session_id=? AND tool=? AND args_hash=? AND status='started'",
                (self.ctx.session_id, call.name, ahash),
            )
            if prior is not None:
                verdict = self._reconcile_one(
                    call.name, call.arguments, self._expected_for(call.name, call.arguments)
                )
                self.ctx.db.execute(
                    "UPDATE tool_executions SET status=? WHERE session_id=? AND tool=? AND args_hash=? AND status='started'",
                    ("done_reconciled" if verdict else "unknown", self.ctx.session_id, call.name, ahash),
                )
                if verdict:
                    return ToolOutcome(
                        call.id,
                        call.name,
                        "this exact operation was already applied by an earlier interrupted attempt; no action taken",
                        True,
                        0,
                        tool.summarize(call.arguments),
                        True,
                    )
                if verdict is None:
                    return self._fail(
                        call,
                        started,
                        "an earlier identical attempt was interrupted and its outcome cannot be determined; "
                        "inspect the current state (read_file / git_status) before retrying",
                    )
            self.ctx.db.execute(
                "INSERT INTO tool_executions(id,session_id,task_id,tool,args_hash,args,status,expected,started) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    exec_id,
                    self.ctx.session_id,
                    self.ctx.current_task_id(),
                    call.name,
                    ahash,
                    json.dumps(call.arguments, default=str),
                    "started",
                    self._expected_for(call.name, call.arguments),
                    time.time(),
                ),
            )
        try:
            result = tool.handler(self.ctx, call.arguments)
            ok = True
        except Cancelled as exc:
            result, ok = f"cancelled: {exc}", False
        except (ToolError, PermissionDenied, PathSecurityError) as exc:
            result, ok = f"error: {exc}", False
        except WorkFlaskError as exc:
            result, ok = f"error: {exc}", False
        except OSError as exc:
            result, ok = f"error: {type(exc).__name__}: {exc}", False
        except Exception as exc:
            result, ok = f"internal error in {call.name}: {type(exc).__name__}: {exc}", False
        result = truncate_output(redact(result))
        duration = (time.monotonic() - started) * 1000
        if mutating:
            self.ctx.db.execute(
                "UPDATE tool_executions SET status=?, result=?, finished=? WHERE id=?",
                ("done" if ok else "failed", result[:4000], time.time(), exec_id),
            )
        return ToolOutcome(call.id, call.name, result, ok, duration, tool.summarize(call.arguments))

    def _fail(self, call: ToolCall, started: float, message: str) -> ToolOutcome:
        return ToolOutcome(
            call.id, call.name, f"error: {message}", False, (time.monotonic() - started) * 1000
        )
