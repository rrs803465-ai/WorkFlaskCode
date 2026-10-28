from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from workflaskcode.agent.context import estimate_tokens
from workflaskcode.providers.types import Message, ToolCall

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ERROR_LINE = re.compile(
    r"(?i)\b(error|fail(ed|ure)?|exception|traceback|assert(ion)?|panic|fatal|denied|cannot|unable|not found)\b"
)
WRITERS = {"create_file", "edit_file", "apply_patch", "move_file", "delete_file"}
PATH_WRITERS = {"create_file", "edit_file", "delete_file"}
IDEMPOTENT = {
    "list_dir",
    "search",
    "find_files",
    "find_symbol",
    "git_status",
    "git_diff",
    "repo_map",
    "read_symbol",
    "web_fetch",
    "web_search",
    "browser_check",
}
WEB_TOOLS = {"web_search", "web_fetch", "browser_check", "start_preview", "stop_preview"}
BULKY_ARGS = ("content", "new_text", "old_text", "patch", "diff")
BULKY_MIN = 400
LONG_LINE = 300
WEB_HINT = re.compile(
    r"(?i)\b(web|websites?|sites?|urls?|https?|browser|internet|online|google|search the|look ?up|landing|front-?end|html|css|"
    r"react|vue|svelte|angular|next\.?js|vite|tailwind|ui|ux|pages?|responsive|mobile|screenshot|preview|latest|current|docs|documentation)\b"
)
STACK_HINT = re.compile(r"(?i)react|vue|svelte|angular|next|vite|static-web|html|node")


def normalize_output(name: str, text: str, limit: int) -> str:
    if name == "read_file":
        return text
    text = ANSI.sub("", text).replace("\r\n", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    run = 0
    blank = 0
    for i, line in enumerate(lines):
        if not line:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        if out and line and line == out[-1] and i > 0:
            run += 1
            continue
        if run >= 2:
            out[-1] = f"{out[-1]}  [repeated x{run + 1}]"
        elif run == 1:
            out.append(out[-1])
        run = 0
        out.append(line)
    if run >= 2:
        out[-1] = f"{out[-1]}  [repeated x{run + 1}]"
    elif run == 1:
        out.append(out[-1])
    return smart_cut("\n".join(out).strip("\n"), limit)


def smart_cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    lines = [
        ln if len(ln) <= LONG_LINE else f"{ln[:LONG_LINE]} ...[+{len(ln) - LONG_LINE} chars]"
        for ln in text.split("\n")
    ]
    if len("\n".join(lines)) <= limit:
        return "\n".join(lines)
    head_budget, tail_budget, err_budget = int(limit * 0.35), int(limit * 0.35), int(limit * 0.3)
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head) :]):
        if used + len(line) + 1 > tail_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()
    if not head and not tail:
        flat = "\n".join(lines)
        return (
            flat[: int(limit * 0.6)]
            + f"\n[... {len(flat) - int(limit * 0.95)} chars omitted ...]\n"
            + flat[-int(limit * 0.35) :]
        )
    middle = lines[len(head) : len(lines) - len(tail)]
    errors: list[str] = []
    used = 0
    for line in middle:
        if ERROR_LINE.search(line) and used + len(line) + 1 <= err_budget:
            errors.append(line[:300])
            used += min(len(line), 300) + 1
    omitted = len(middle) - len(errors)
    marker = f"[... {omitted} line(s) omitted"
    marker += f"; {len(errors)} error-like line(s) kept ...]" if errors else " ...]"
    return "\n".join(
        [
            *head,
            marker,
            *errors,
            *([marker.replace("[... ", "[... end of kept errors; ")] if errors else []),
            *tail,
        ]
    )


def _short_args(args: dict[str, Any]) -> str:
    for key in ("path", "pattern", "name", "glob", "query", "url", "command"):
        if key in args and isinstance(args[key], str):
            return args[key][:80]
    return ""


@dataclass
class SaverStats:
    requests: int = 0
    raw_tokens: int = 0
    sent_tokens: int = 0
    trimmed_chars: int = 0
    tools_gated: int = 0
    runs: list[tuple[int, int]] = field(default_factory=list)

    @property
    def saved_tokens(self) -> int:
        return max(self.raw_tokens - self.sent_tokens, 0) + self.trimmed_chars // 4

    @property
    def percent(self) -> float:
        total = self.raw_tokens + self.trimmed_chars // 4
        return 100.0 * self.saved_tokens / total if total else 0.0


class TokenSaver:
    def __init__(
        self,
        *,
        enabled: bool = True,
        keep_turns: int = 4,
        old_chars: int = 700,
        max_output_chars: int = 12_000,
        gate_web_tools: bool = True,
    ) -> None:
        self.enabled = enabled
        self.keep_turns = keep_turns
        self.old_chars = old_chars
        self.max_output_chars = max_output_chars
        self.gate_web_tools = gate_web_tools
        self.stats = SaverStats()

    def configure(self, tokens) -> None:
        self.enabled = tokens.saver
        self.keep_turns = tokens.keep_recent_turns
        self.old_chars = tokens.old_output_chars
        self.max_output_chars = tokens.max_output_chars
        self.gate_web_tools = tokens.gate_web_tools

    def clean_output(self, name: str, text: str) -> str:
        if not self.enabled:
            return text
        cleaned = normalize_output(name, text, self.max_output_chars)
        self.stats.trimmed_chars += max(len(text) - len(cleaned), 0)
        return cleaned

    def _window_start(self, history: list[Message]) -> int:
        seen = 0
        for index in range(len(history) - 1, -1, -1):
            if history[index].role == "assistant":
                seen += 1
                if seen >= self.keep_turns:
                    return index
        return 0

    def view(self, history: list[Message]) -> list[Message]:
        raw = estimate_tokens(history)
        out = self._compress(history) if self.enabled else history
        self.stats.requests += 1
        self.stats.raw_tokens += raw
        self.stats.sent_tokens += estimate_tokens(out)
        return out

    def _compress(self, history: list[Message]) -> list[Message]:
        boundary = self._window_start(history)
        if boundary <= 0:
            return history
        calls: dict[str, tuple[str, dict[str, Any]]] = {}
        last_write: dict[str, int] = {}
        last_patch = -1
        last_read: dict[str, int] = {}
        last_same: dict[tuple[str, str], int] = {}
        for index, message in enumerate(history):
            for call in message.tool_calls:
                calls[call.id] = (call.name, call.arguments)
                if call.name in PATH_WRITERS and isinstance(call.arguments.get("path"), str):
                    last_write[call.arguments["path"]] = index
                elif call.name == "move_file":
                    for key in ("source", "src", "from", "destination", "dst", "to", "path"):
                        if isinstance(call.arguments.get(key), str):
                            last_write[call.arguments[key]] = index
                elif call.name == "apply_patch":
                    last_patch = index
                elif call.name == "read_file" and isinstance(call.arguments.get("path"), str):
                    last_read[call.arguments["path"]] = index
                elif call.name in IDEMPOTENT:
                    last_same[(call.name, json.dumps(call.arguments, sort_keys=True, default=str))] = index
        out: list[Message] = []
        for index, message in enumerate(history):
            if index >= boundary:
                out.append(message)
            elif message.role == "tool":
                out.append(
                    self._old_tool(index, message, calls, last_write, last_patch, last_read, last_same)
                )
            elif message.role == "assistant" and message.tool_calls:
                out.append(self._old_assistant(message))
            else:
                out.append(message)
        return out

    def _old_tool(self, index, message, calls, last_write, last_patch, last_read, last_same) -> Message:
        name, args = calls.get(message.tool_call_id or "", (message.name or "", {}))
        stub = None
        if name == "read_file":
            path = args.get("path")
            if (
                last_patch > index
                or (isinstance(path, str) and last_write.get(path, -1) > index)
                or (isinstance(path, str) and last_read.get(path, -1) > index)
            ):
                stub = (
                    f"[read_file {path} omitted: the file was changed or read again later; re-read if needed]"
                )
        elif name in IDEMPOTENT:
            key = (name, json.dumps(args, sort_keys=True, default=str))
            if last_same.get(key, -1) > index:
                stub = f"[{name} {_short_args(args)} omitted: the same call was repeated later]"
        if stub is not None:
            return Message("tool", stub, tool_call_id=message.tool_call_id, name=message.name)
        limit = self.old_chars
        if len(message.content) > limit:
            return Message(
                "tool",
                smart_cut(message.content, limit) + "\n[older output shortened]",
                tool_call_id=message.tool_call_id,
                name=message.name,
            )
        return message

    def _old_assistant(self, message: Message) -> Message:
        changed = False
        calls: list[ToolCall] = []
        for call in message.tool_calls:
            if call.name in WRITERS and any(
                isinstance(call.arguments.get(k), str) and len(call.arguments[k]) > BULKY_MIN
                for k in BULKY_ARGS
            ):
                arguments = dict(call.arguments)
                for key in BULKY_ARGS:
                    value = arguments.get(key)
                    if isinstance(value, str) and len(value) > BULKY_MIN:
                        arguments[key] = (
                            f"[{len(value)} chars omitted; already applied, see the file on disk]"
                        )
                calls.append(ToolCall(call.id, call.name, arguments, json.dumps(arguments), call.parse_error))
                changed = True
            else:
                calls.append(call)
        if not changed:
            return message
        return Message(message.role, message.content, calls, message.tool_call_id, message.name)

    def select_tools(self, specs: list, history: list[Message], project_hint: str = "") -> list:
        if not (self.enabled and self.gate_web_tools):
            return specs
        if any(s.name in WEB_TOOLS for s in specs) and not self._web_relevant(history, project_hint):
            kept = [s for s in specs if s.name not in WEB_TOOLS]
            self.stats.tools_gated += len(specs) - len(kept)
            return kept
        return specs

    @staticmethod
    def _web_relevant(history: list[Message], project_hint: str) -> bool:
        if STACK_HINT.search(project_hint or ""):
            return True
        users = [m.content for m in history if m.role == "user" and not m.content.startswith("[")][-4:]
        if any(WEB_HINT.search(text) for text in users):
            return True
        return any(m.role == "tool" and m.name in WEB_TOOLS for m in history)
