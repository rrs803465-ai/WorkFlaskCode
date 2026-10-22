from __future__ import annotations

import dataclasses
import itertools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from workflaskcode.agent.prompts import SIDE
from workflaskcode.agent.registry import ACCESS_READ, ToolContext, ToolExecutor, ToolRegistry
from workflaskcode.agent.router import ModelRouter
from workflaskcode.config.models import ModelRef, Settings
from workflaskcode.errors import AllModelsExhausted, Cancelled, WorkFlaskError
from workflaskcode.providers.types import Message
from workflaskcode.security.permissions import deny_all_approver
from workflaskcode.state.sessions import SessionStore
from workflaskcode.ui.console import Console
from workflaskcode.usage.tracker import BudgetExceeded

MAX_SIDE_STEPS = 6
SIDE_HISTORY_LIMIT = 24


@dataclass
class SideJob:
    number: int
    question: str
    cancel: threading.Event = field(default_factory=threading.Event)
    status: str = "queued"
    answer: str = ""
    error: str | None = None
    started: float = field(default_factory=time.time)
    thread: threading.Thread | None = None


class SideChatManager:
    def __init__(
        self,
        *,
        router: ModelRouter,
        registry: ToolRegistry,
        base_ctx: Callable[[], ToolContext],
        console: Console,
        settings: Callable[[], Settings],
        store: SessionStore,
        session_id: str,
        model: Callable[[], ModelRef | None],
        project_block: Callable[[], str],
        main_state: Callable[[], str],
    ) -> None:
        self.router = router
        self.registry = registry
        self.base_ctx = base_ctx
        self.console = console
        self.settings = settings
        self.store = store
        self.session_id = session_id
        self.model = model
        self.project_block = project_block
        self.main_state = main_state
        self.history: list[Message] = []
        self.jobs: dict[int, SideJob] = {}
        self._counter = itertools.count(1)
        self._lock = threading.RLock()
        self._slots = threading.Semaphore(self.settings().limits.max_side_chats)
        self._print_lock = threading.Lock()

    def restore(self) -> None:
        self.history = []
        for row in self.store.messages(self.session_id, "side"):
            payload = row.get("payload")
            if payload and "message" in payload:
                self.history.append(Message.from_dict(payload["message"]))

    def reset(self) -> None:
        with self._lock:
            self.history = []

    def active(self) -> list[SideJob]:
        with self._lock:
            return [j for j in self.jobs.values() if j.status in ("queued", "running")]

    def ask(self, question: str) -> SideJob:
        question = question.strip()
        if not question:
            raise WorkFlaskError("usage: @btw <question>")
        job = SideJob(next(self._counter), question)
        with self._lock:
            self.jobs[job.number] = job
        limit = self.settings().limits.max_side_chats
        running = sum(1 for j in self.active() if j.status == "running")
        if running >= limit:
            self.console.dim(
                f"  btw #{job.number} queued ({running} side question(s) already running; limit {limit})"
            )
        thread = threading.Thread(target=self._run, args=(job,), name=f"btw-{job.number}", daemon=True)
        job.thread = thread
        thread.start()
        return job

    def cancel(self, number: int | None = None) -> list[int]:
        cancelled = []
        with self._lock:
            targets = (
                [self.jobs[number]]
                if number is not None and number in self.jobs
                else list(self.jobs.values())
            )
        for job in targets:
            if job.status in ("queued", "running"):
                job.cancel.set()
                cancelled.append(job.number)
        return cancelled

    def join(self, timeout: float | None = None) -> None:
        for job in list(self.jobs.values()):
            if job.thread is not None:
                job.thread.join(timeout)

    def _tool_context(self, job: SideJob) -> ToolContext:
        return dataclasses.replace(
            self.base_ctx(), cancel=job.cancel, approver=deny_all_approver, asker=lambda q: None, extra={}
        )

    def _run(self, job: SideJob) -> None:
        while not self._slots.acquire(timeout=0.2):
            if job.cancel.is_set():
                job.status = "cancelled"
                self._emit(job, "cancelled before it started")
                return
        try:
            job.status = "running"
            self._answer(job)
        finally:
            self._slots.release()

    def _answer(self, job: SideJob) -> None:
        s = self.settings()
        use_tools = s.privacy.side_chat_may_read_files
        ctx = self._tool_context(job)
        executor = ToolExecutor(self.registry, ctx, lambda: {ACCESS_READ})
        specs = self.registry.specs({ACCESS_READ}) if use_tools else None
        snapshot = (
            self.main_state() if s.privacy.send_project_context else "(project context sharing is disabled)"
        )
        system = Message(
            "system",
            f"{SIDE}\n\n{self.project_block()}\n\nMain task snapshot (read-only, may be stale):\n{snapshot}",
        )
        with self._lock:
            context = list(self.history[-SIDE_HISTORY_LIMIT:])
        while context and context[0].role == "tool":
            context.pop(0)
        turn: list[Message] = [Message("user", job.question)]
        try:
            for _ in range(MAX_SIDE_STEPS):
                if job.cancel.is_set():
                    raise Cancelled("cancelled")
                route = self.router.call(
                    [system, *context, *turn],
                    tools=specs,
                    primary=self.model(),
                    channel="side",
                    cancel=job.cancel,
                    allow_fallback=True,
                )
                if job.cancel.is_set():
                    raise Cancelled("cancelled")
                reply = route.response
                turn.append(reply.as_message())
                if not reply.tool_calls:
                    job.answer = reply.content.strip()
                    break
                for call in reply.tool_calls:
                    outcome = executor.execute(call)
                    turn.append(Message("tool", outcome.content, tool_call_id=call.id, name=call.name))
            else:
                job.answer = (
                    next(
                        (m.content for m in reversed(turn) if m.role == "assistant" and m.content.strip()), ""
                    )
                    or "(no answer within the tool-step limit)"
                )
            job.status = "done"
            with self._lock:
                self.history.extend(turn)
            for message in turn:
                self.store.add_message(
                    self.session_id,
                    "side",
                    message.role,
                    message.content,
                    {"message": message.to_dict(), "job": job.number},
                )
            self._emit(job, job.answer or "(empty answer)")
        except Cancelled:
            job.status = "cancelled"
            self._emit(job, "cancelled")
        except AllModelsExhausted as exc:
            job.status, job.error = "failed", str(exc)
            self._emit_error(job, f"no model could answer: {exc}")
        except (BudgetExceeded, WorkFlaskError) as exc:
            job.status, job.error = "failed", str(exc)
            self._emit_error(job, str(exc))
        except Exception as exc:
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
            self._emit_error(job, job.error)

    def _emit(self, job: SideJob, text: str) -> None:
        label = f"#{job.number} · {job.question[:48]}" + ("…" if len(job.question) > 48 else "")
        with self._print_lock:
            if job.status == "cancelled":
                self.console.dim(f"  btw #{job.number} {text}")
            else:
                self.console.markdown(text, "side", label)

    def _emit_error(self, job: SideJob, text: str) -> None:
        with self._print_lock:
            self.console.warn(f"btw #{job.number} failed: {text}")
