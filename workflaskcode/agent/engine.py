from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from workflaskcode.agent.context import ContextManager
from workflaskcode.agent.registry import (
    ACCESS_EXEC,
    ACCESS_META,
    ACCESS_READ,
    ACCESS_WRITE,
    ToolExecutor,
    args_hash,
)
from workflaskcode.agent.router import ModelRouter
from workflaskcode.config.models import ModelRef, Settings
from workflaskcode.errors import AllModelsExhausted, Cancelled, ProviderError, WorkFlaskError
from workflaskcode.providers.types import Message
from workflaskcode.state.sessions import SessionStore
from workflaskcode.tasks.ledger import TaskStatus
from workflaskcode.ui.console import Console
from workflaskcode.usage.tracker import BudgetExceeded

MAX_NUDGES = 3
REPEAT_LIMIT = 3


@dataclass
class RunResult:
    status: str
    summary: str = ""
    model: str | None = None
    requests: int = 0
    tool_calls: int = 0
    seconds: float = 0.0
    error: str | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "completed"


class AgentLoop:
    def __init__(
        self,
        *,
        router: ModelRouter,
        executor: ToolExecutor,
        console: Console,
        settings: Callable[[], Settings],
        store: SessionStore,
        session_id: str,
        context: ContextManager,
        state_summary: Callable[[], str],
        system_prompt: Callable[[str], str],
        model_for: Callable[[str], ModelRef | None],
        allowed_access: Callable[[str], set[str]],
    ) -> None:
        self.router = router
        self.executor = executor
        self.console = console
        self.settings = settings
        self.store = store
        self.session_id = session_id
        self.context = context
        self.state_summary = state_summary
        self.system_prompt = system_prompt
        self.model_for = model_for
        self.allowed_access = allowed_access
        self.history: list[Message] = []
        self.lock = threading.RLock()
        self.active_model: ModelRef | None = None

    def load_history(self) -> int:
        self.history = []
        for row in self.store.messages(self.session_id, "main"):
            payload = row.get("payload")
            if payload and "message" in payload:
                self.history.append(Message.from_dict(payload["message"]))
        return len(self.history)

    def _persist(self, message: Message) -> None:
        self.store.add_message(
            self.session_id, "main", message.role, message.content, {"message": message.to_dict()}
        )

    def _append(self, message: Message) -> None:
        self.history.append(message)
        self._persist(message)

    def reset_history(self) -> None:
        self.history = []

    def compact_now(self, force: bool = True) -> tuple[int, int]:
        before = len(self.history)
        self.history = self.context.compact(self.history, self.state_summary(), force=force)
        return before, len(self.history)

    def run(self, request: str, *, mode: str, cancel: threading.Event) -> RunResult:
        started = time.monotonic()
        ctx = self.executor.ctx
        s = self.settings()
        ctx.extra.pop("finish_summary", None)
        ctx.extra["recent_errors"] = []
        self._append(Message("user", request))
        requests = tool_calls = nudges = failed_tests = 0
        last_signature: tuple[str, str] | None = None
        repeats = 0
        final_text = ""
        model = self.model_for(mode)
        result = RunResult("completed", model=str(model) if model else None)
        self.console.dim(f"model {model}  ·  mode {mode}")
        try:
            while True:
                if cancel.is_set():
                    raise Cancelled("cancelled by user")
                if time.monotonic() - started > s.limits.max_task_seconds:
                    result.status, result.error = (
                        "limit",
                        f"time limit of {s.limits.max_task_seconds:.0f}s reached",
                    )
                    break
                if requests >= s.limits.max_requests_per_task:
                    result.status, result.error = (
                        "limit",
                        f"request limit of {s.limits.max_requests_per_task} reached",
                    )
                    break
                self.context.update_budget(self.settings().context_budget_tokens)
                if self.context.needs_compaction(self.history):
                    self.console.dim(
                        "compacting context (deterministic summary; files and ledger stay authoritative)"
                    )
                    self.history = self.context.compact(self.history, self.state_summary())
                access = self.allowed_access(mode)
                tools = self.executor.registry.specs(access)
                system = Message("system", self.system_prompt(mode))
                task = ctx.ledger.current()
                writer = self.console.stream_writer("main", "agent")
                route = self.router.call(
                    [system, *self.history],
                    tools=tools or None,
                    primary=self.model_for(mode),
                    task_id=task.id if task else None,
                    channel="main",
                    cancel=cancel,
                    on_text=writer.write,
                    on_reset=writer.reset,
                    compact=lambda msgs: [
                        msgs[0],
                        *self.context.compact(msgs[1:], self.state_summary(), force=True),
                    ],
                )
                writer.close()
                requests += 1
                self.active_model = route.ref
                ctx.extra["active_model"] = str(route.ref)
                result.model = str(route.ref)
                response = route.response
                assistant = response.as_message()
                self._append(assistant)
                if response.content.strip():
                    final_text = response.content.strip()
                if not response.tool_calls:
                    summary = ctx.extra.get("finish_summary")
                    if summary or mode in ("chat", "plan"):
                        break
                    open_tasks = [
                        t
                        for t in ctx.ledger.list()
                        if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
                    ]
                    if open_tasks and nudges < MAX_NUDGES and mode == "coding":
                        nudges += 1
                        self._append(
                            Message(
                                "user",
                                "[system notice] Tasks are still open:\n"
                                + ctx.ledger.render()
                                + "\nContinue working, or verify and complete them, or mark them blocked/failed with a reason. "
                                "Call `finish` when the whole job is done.",
                            )
                        )
                        continue
                    break
                for call in response.tool_calls:
                    if cancel.is_set():
                        self._append(
                            Message(
                                "tool", "cancelled before the tool ran", tool_call_id=call.id, name=call.name
                            )
                        )
                        continue
                    if tool_calls >= s.limits.max_tool_calls_per_task:
                        self._append(
                            Message(
                                "tool",
                                "error: tool-call limit reached for this task",
                                tool_call_id=call.id,
                                name=call.name,
                            )
                        )
                        result.status, result.error = (
                            "limit",
                            f"tool-call limit of {s.limits.max_tool_calls_per_task} reached",
                        )
                        continue
                    tool_calls += 1
                    outcome = self.executor.execute(call)
                    self.console.tool(
                        call.name,
                        outcome.summary,
                        status="ok" if outcome.ok else "err",
                        ms=outcome.duration_ms,
                    )
                    self._append(Message("tool", outcome.content, tool_call_id=call.id, name=call.name))
                    if not outcome.ok:
                        ctx.extra.setdefault("recent_errors", []).append(
                            f"{call.name}: {outcome.content[:200]}"
                        )
                    signature = (
                        call.name,
                        args_hash(call.name, call.arguments) + ("ok" if outcome.ok else outcome.content[:80]),
                    )
                    repeats = repeats + 1 if signature == last_signature and not outcome.ok else 1
                    last_signature = signature
                    if repeats >= REPEAT_LIMIT:
                        self._append(
                            Message(
                                "user",
                                f"[system notice] `{call.name}` failed {repeats} times in a row with the same arguments. "
                                "Do not retry it again; change approach or report the blocker.",
                            )
                        )
                        repeats = 0
                    if call.name == "run_tests":
                        last = ctx.extra.get("last_test") or {}
                        failed_tests = 0 if last.get("ok") else failed_tests + 1
                        if failed_tests > s.limits.max_test_repair_attempts:
                            self._append(
                                Message(
                                    "user",
                                    f"[system notice] The repair-attempt limit ({s.limits.max_test_repair_attempts}) was reached while tests are still "
                                    "failing. Stop editing. Report exactly what fails and what you tried via `finish`; do not weaken or skip tests.",
                                )
                            )
                            failed_tests = -999
                if result.status == "limit":
                    break
                if ctx.extra.get("finish_summary"):
                    break
        except Cancelled as exc:
            result.status, result.error = "cancelled", str(exc)
        except BudgetExceeded as exc:
            result.status, result.error = "limit", str(exc)
        except AllModelsExhausted as exc:
            result.status, result.error, result.failures = "paused", str(exc), list(exc.failures)
        except ProviderError as exc:
            result.status, result.error = "failed", f"{exc.kind.value}: {exc}"
        except WorkFlaskError as exc:
            result.status, result.error = "failed", str(exc)
        result.summary = ctx.extra.get("finish_summary") or final_text
        result.requests, result.tool_calls = requests, tool_calls
        result.seconds = time.monotonic() - started
        if result.status in ("cancelled", "paused", "limit", "failed"):
            ctx.ledger.pause_in_progress()
        return result


ACCESS_BY_MODE = {
    "chat": {ACCESS_READ},
    "coding": {ACCESS_READ, ACCESS_WRITE, ACCESS_EXEC, ACCESS_META},
}
