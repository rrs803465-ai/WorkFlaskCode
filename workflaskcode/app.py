from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workflaskcode.agent.btw import SideChatManager
from workflaskcode.agent.context import ContextManager
from workflaskcode.agent.engine import AgentLoop, RunResult
from workflaskcode.agent.handoff import load_handoff, next_step_text, state_summary, write_handoff
from workflaskcode.agent.prompts import build_system_prompt, project_section
from workflaskcode.agent.registry import (
    ACCESS_EXEC,
    ACCESS_META,
    ACCESS_READ,
    ACCESS_WRITE,
    ToolContext,
    ToolDef,
    ToolExecutor,
    ToolRegistry,
)
from workflaskcode.agent.router import ModelRouter
from workflaskcode.agent.toolset import register_core_tools
from workflaskcode.browser.preview import stop_all as stop_dev_servers
from workflaskcode.config.credentials import CredentialStore
from workflaskcode.config.models import ModelRef, PermissionMode, Settings
from workflaskcode.config.paths import AgentHome
from workflaskcode.config.store import ConfigStore
from workflaskcode.design import DesignLock
from workflaskcode.errors import ConfigError, WorkFlaskError
from workflaskcode.git import GitRepo
from workflaskcode.http import HttpClient
from workflaskcode.plugins import PluginManager, SkillIndex
from workflaskcode.providers.catalog import ModelCatalog
from workflaskcode.providers.registry import ProviderRegistry
from workflaskcode.security.permissions import ApprovalRequest, ApprovalResult, PermissionPolicy
from workflaskcode.state.db import Database
from workflaskcode.state.sessions import ProjectRecord, ProjectRegistry, SessionRecord, SessionStore
from workflaskcode.tasks.checkpoints import CheckpointManager
from workflaskcode.tasks.ledger import TaskLedger, TaskStatus
from workflaskcode.tools.files import FileTools
from workflaskcode.tools.process import GLOBAL_TRACKER
from workflaskcode.ui.console import Console
from workflaskcode.update.checker import Endpoints
from workflaskcode.usage.tracker import UsageTracker
from workflaskcode.utils.redact import register_secret
from workflaskcode.workspace.guard import WorkspaceGuard
from workflaskcode.workspace.metadata import ProjectMetadata
from workflaskcode.workspace.project import ProjectInfo, detect_project

log = logging.getLogger("workflaskcode.app")


class ApprovalBroker:
    def __init__(
        self,
        console: Console,
        *,
        interactive: bool,
        auto_approve: bool = False,
        cancel: threading.Event | None = None,
    ) -> None:
        self.console = console
        self.interactive = interactive
        self.auto_approve = auto_approve
        self.cancel = cancel
        self._serial = threading.Lock()
        self._cond = threading.Condition()
        self._pending: dict[str, Any] | None = None

    def has_pending(self) -> bool:
        with self._cond:
            return self._pending is not None

    def pending_kind(self) -> str | None:
        with self._cond:
            return self._pending["kind"] if self._pending else None

    def _wait(self, slot: dict[str, Any]) -> None:
        with self._cond:
            self._pending = slot
            while slot["result"] is None and not slot.get("abort"):
                if self.cancel is not None and self.cancel.is_set():
                    slot["abort"] = True
                    break
                self._cond.wait(0.25)
            self._pending = None

    def approve(self, request: ApprovalRequest) -> ApprovalResult:
        if not self.interactive:
            if self.auto_approve and not request.destructive:
                return ApprovalResult(True, note="auto-approved (--yes)")
            return ApprovalResult(False, note="no interactive user is available to approve this")
        with self._serial:
            slot: dict[str, Any] = {"kind": "approval", "request": request, "result": None}
            self.console.lines(self.console.approval_panel(request))
            self._wait(slot)
            return slot["result"] or ApprovalResult(False, note="no answer (cancelled)")

    def ask(self, question: str) -> str | None:
        if not self.interactive:
            return None
        with self._serial:
            slot: dict[str, Any] = {"kind": "question", "result": None}
            self.console.lines(
                self.console.r.box(
                    [question, "", "type your answer and press Enter (or /skip)"],
                    title="agent question",
                    token="info",
                    title_token="info",
                )
            )
            self._wait(slot)
            result = slot["result"]
            return None if result in (None, "/skip") else str(result)

    def answer(self, line: str) -> bool:
        with self._cond:
            slot = self._pending
            if slot is None:
                return False
            if slot["kind"] == "approval":
                word = line.strip().lower()
                if word in ("y", "yes"):
                    slot["result"] = ApprovalResult(True)
                elif word in ("a", "always"):
                    slot["result"] = ApprovalResult(True, remember=True)
                elif word in ("n", "no", ""):
                    slot["result"] = ApprovalResult(False, note="denied by user")
                else:
                    self.console.warn("answer y (once), a (remember for this session) or n (deny)")
                    return True
            else:
                slot["result"] = line
            self._cond.notify_all()
            return True

    def abort(self) -> None:
        with self._cond:
            if self._pending is not None:
                self._pending["abort"] = True
            self._cond.notify_all()


@dataclass
class SwitchReport:
    old: str | None
    new: str
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resumed: bool = False


class Runtime:
    def __init__(
        self,
        home: AgentHome,
        console: Console,
        *,
        interactive: bool = True,
        auto_approve: bool = False,
        http: HttpClient | None = None,
    ) -> None:
        self.home = home.ensure()
        self.console = console
        self.interactive = interactive
        self.http = http
        self.update_endpoints = Endpoints()
        self.update_root: Path | None = None
        self.config = ConfigStore(self.home)
        self.global_settings: Settings = self.config.load_or_default()
        self.session_overrides: dict[str, Any] = {}
        self.settings: Settings = self.global_settings
        self.credentials = CredentialStore(self.home)
        self.providers = ProviderRegistry(self.settings, self.credentials, http)
        self.catalog = ModelCatalog(self.home.cache_dir)
        self.db = Database(self.home.db_path)
        self.projects = ProjectRegistry(self.db)
        self.sessions = SessionStore(self.db)
        self.tracker = UsageTracker(self.db)
        self.cancel_event = threading.Event()
        self.broker = ApprovalBroker(
            console, interactive=interactive, auto_approve=auto_approve, cancel=self.cancel_event
        )
        self.router = ModelRouter(self.providers, lambda: self.settings, self.tracker, console)
        self.plugins = PluginManager(self.home.plugins_dir, self.db)
        self.skills = SkillIndex(self.home.skills_dir)
        self.skills.install_builtin()
        self.mode: str = self.settings.mode.value
        self.project: ProjectRecord | None = None
        self.session: SessionRecord | None = None
        self.root: Path | None = None
        self.meta: ProjectMetadata | None = None
        self.guard: WorkspaceGuard | None = None
        self.git: GitRepo | None = None
        self.files: FileTools | None = None
        self.ledger: TaskLedger | None = None
        self.checkpoints: CheckpointManager | None = None
        self.policy: PermissionPolicy | None = None
        self.ctx: ToolContext | None = None
        self.registry = ToolRegistry()
        self.executor: ToolExecutor | None = None
        self.loop: AgentLoop | None = None
        self.side: SideChatManager | None = None
        self.design: DesignLock | None = None
        self.context = ContextManager(self.settings.context_budget_tokens)
        self._info: ProjectInfo | None = None
        self._worker: threading.Thread | None = None
        self._queue: list[tuple[str, str | None]] = []
        self.run_mode: str | None = None
        self.exit_requested = False
        self._qlock = threading.RLock()
        self._stop_reason: str | None = None
        self.last_result: RunResult | None = None
        self.started_at = time.time()
        self.extra_commands: list[dict[str, Any]] = []
        self._load_plugins()
        self._register_secrets()

    def _register_secrets(self) -> None:
        for name, cfg in self.settings.providers.items():
            cred = self.credentials.get(name, cfg)
            if cred:
                register_secret(cred.value)

    def _load_plugins(self) -> None:
        loaded = self.plugins.load_enabled()
        if loaded:
            self.console.dim("plugins loaded: " + ", ".join(loaded))
        for name, problem in self.plugins.errors.items():
            self.console.warn(f"plugin {name} not loaded: {problem}")

    def reload_settings(self) -> Settings:
        self.global_settings = self.config.load_or_default()
        project_config = self.meta.directory / "config.toml" if self.meta else None
        self.settings = (
            self.config.effective(project_config, self.session_overrides)
            if self.config.exists()
            else self.global_settings
        )
        self.providers.settings = self.settings
        self.providers.invalidate()
        self.context.update_budget(self.settings.context_budget_tokens)
        if self.policy is not None:
            self.policy.settings = self.settings.permissions
        self.console.verbose = self.settings.ui.verbose
        self._register_secrets()
        return self.settings

    def apply_settings(self, mutate: Callable[[Settings], Settings]) -> Settings:
        updated = mutate(self.config.load_or_default())
        self.config.save(updated)
        return self.reload_settings()

    def override_session(self, dotted: str, value: Any) -> None:
        cursor = self.session_overrides
        parts = dotted.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
        self.reload_settings()

    def model_for(self, mode: str) -> ModelRef | None:
        return self.settings.effective_chat_model() if mode == "chat" else self.settings.coding_model

    def access_for(self, mode: str) -> set[str]:
        if mode == "chat":
            return {ACCESS_READ}
        if mode == "plan":
            return {ACCESS_READ, ACCESS_META}
        if self.policy is not None and self.policy.mode is PermissionMode.READ_ONLY:
            return {ACCESS_READ, ACCESS_META}
        return {ACCESS_READ, ACCESS_WRITE, ACCESS_EXEC, ACCESS_META}

    def refresh_info(self) -> ProjectInfo:
        assert self.root is not None
        self._info = detect_project(self.root)
        return self._info

    def info(self) -> ProjectInfo:
        if self._info is None:
            self.refresh_info()
        assert self._info is not None
        return self._info

    def require_project(self) -> Path:
        if self.root is None or self.ctx is None:
            raise WorkFlaskError("no project is open", hint="use @open <path> or @new")
        return self.root

    def open_project(
        self, path: Path | str, *, resume: bool = True, new_session: bool = False
    ) -> ProjectRecord:
        if self.worker_running:
            raise WorkFlaskError("a task is running; cancel it first (Ctrl+C) or wait for it to finish")
        target = Path(path).expanduser()
        if not target.exists():
            raise WorkFlaskError(f"path does not exist: {target}")
        if not target.is_dir():
            raise WorkFlaskError(f"not a directory: {target}")
        resolved = target.resolve()
        if resolved == self.home.root or self.home.root in resolved.parents:
            raise WorkFlaskError(
                "the agent home directory cannot be used as a project",
                hint="choose a separate folder for your project",
            )
        if self.project is not None:
            self._persist_state("switching project")
        record = self.projects.register(resolved, metadata_location=self.settings.metadata_location)
        self.project = record
        self.root = resolved
        self.meta = ProjectMetadata.for_project(
            resolved, record.metadata_location, self.home.project_dir(record.id)
        )
        inspection = self.meta.ensure()
        if inspection.unknown:
            self.console.dim(
                f"metadata folder contains files WorkFlaskCode does not manage ({len(inspection.unknown)}); left untouched"
            )
        if inspection.corrupt:
            self.console.warn("corrupt metadata files were quarantined: " + ", ".join(inspection.corrupt))
        session = self.sessions.latest_for_project(record.id) if resume and not new_session else None
        if session is None:
            session = self.sessions.create(record.id, self.mode)
        else:
            self.mode = session.mode if session.mode in ("chat", "coding") else self.mode
        self.reload_settings()
        self._info = None
        self._bind_session(session)
        return record

    def resume_session(self, session_id: str) -> SessionRecord:
        session = self.sessions.get(session_id)
        if session is None:
            raise WorkFlaskError(f"unknown session '{session_id}'", hint="list sessions with @resume list")
        project = self.projects.get(session.project_id)
        if project is None or not project.path.is_dir():
            raise WorkFlaskError("the project of that session no longer exists on disk")
        self.open_project(project.path, resume=False, new_session=False)
        self._bind_session(session)
        self.mode = session.mode if session.mode in ("chat", "coding") else self.mode
        return session

    def _bind_session(self, session: SessionRecord) -> None:
        assert self.root is not None and self.project is not None and self.meta is not None
        self.session = session
        self.tracker.session_id = session.id
        root = self.root
        self.guard = WorkspaceGuard(
            root,
            extra_roots=[Path(p) for p in self.settings.permissions.extra_roots],
            protected=[self.home.root],
        )
        self.git = GitRepo(root)
        self.policy = PermissionPolicy(self.settings.permissions, root)
        self.cancel_event = threading.Event()
        self.broker.cancel = self.cancel_event
        git = self.git
        self.files = FileTools(
            self.guard,
            self.db,
            self.home.backups_dir / session.id,
            session_id=session.id,
            approver=self.broker.approve,
            can_write=lambda: self.policy.can_write_files() and self.effective_mode == "coding",
            write_needs_approval=lambda: self.policy.mode is PermissionMode.APPROVE_ALL,
            dirty_paths=(lambda: git.dirty_paths())
            if self.settings.git.enabled and GitRepo.available() and git.is_repo()
            else None,
            task_id_getter=lambda: (
                self.ledger.current().id if self.ledger and self.ledger.current() else None
            ),
        )
        self.ledger = TaskLedger(self.db, session.id, self.project.id)
        self.checkpoints = CheckpointManager(self.db, session.id, self.project.id, self.files)
        self.design = DesignLock(root, self.meta)
        self.skills.project_dir = self.meta.directory / "skills"
        self.ctx = ToolContext(
            guard=self.guard,
            files=self.files,
            policy=self.policy,
            approver=self.broker.approve,
            db=self.db,
            session_id=session.id,
            project_root=root,
            settings=lambda: self.settings,
            ledger=self.ledger,
            git=self.git,
            console=self.console,
            cancel=self.cancel_event,
            project_info=self.info,
            asker=self.broker.ask,
            extra={"reports_dir": self.home.reports_dir},
        )
        self.registry = ToolRegistry()
        register_core_tools(self.registry)
        self._register_extension_tools()
        self.executor = ToolExecutor(self.registry, self.ctx, lambda: self.access_for(self.effective_mode))
        self.loop = AgentLoop(
            router=self.router,
            executor=self.executor,
            console=self.console,
            settings=lambda: self.settings,
            store=self.sessions,
            session_id=session.id,
            context=self.context,
            state_summary=self.state_text,
            system_prompt=self.system_prompt,
            model_for=self.model_for,
            allowed_access=self.access_for,
        )
        restored = self.loop.load_history()
        self.side = SideChatManager(
            router=self.router,
            registry=self.registry,
            base_ctx=lambda: self.ctx,
            console=self.console,
            settings=lambda: self.settings,
            store=self.sessions,
            session_id=session.id,
            model=lambda: self.model_for("chat"),
            project_block=self.project_block,
            main_state=self.state_text,
        )
        self.side.restore()
        notes = self.executor.reconcile_interrupted()
        if notes:
            self.console.dim("reconciled interrupted tool operations: " + "; ".join(notes))
        if restored:
            self.console.dim(f"restored {restored} message(s) from the previous session")

    def _register_extension_tools(self) -> None:
        skills = self.skills

        def read_skill(ctx: ToolContext, args: dict[str, Any]) -> str:
            skill = skills.get(args["name"])
            if skill is None:
                return "unknown skill; available: " + ", ".join(s.name for s in skills.list())
            return skill.read()

        self.registry.register(
            ToolDef(
                "read_skill",
                "Read the full instructions of a named skill from the skills index.",
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                read_skill,
                ACCESS_READ,
                lambda a: a.get("name", ""),
                source="core",
            )
        )
        access_map = {"read": ACCESS_READ, "write": ACCESS_WRITE, "exec": ACCESS_EXEC}
        for tool in self.plugins.tools:
            try:
                self.registry.register(
                    ToolDef(
                        tool["name"],
                        tool["description"],
                        tool["parameters"],
                        tool["handler"],
                        access_map[tool["access"]],
                        lambda a: "",
                        source=tool["source"],
                    )
                )
            except WorkFlaskError as exc:
                self.console.warn(f"plugin tool skipped: {exc}")

    def design_rules(self) -> str | None:
        return self.design.rules_text() if self.design else None

    def git_line(self) -> str:
        if self.git is None or not self.settings.git.enabled or not GitRepo.available():
            return "disabled or git not installed"
        if not self.git.is_repo():
            return "not a repository"
        dirty = len(self.git.dirty_paths())
        return f"branch {self.git.current_branch() or '?'}, {dirty} changed path(s)"

    def project_block(self) -> str:
        root = self.require_project()
        assert self.meta is not None and self.policy is not None
        return project_section(
            root,
            self.info().summary(),
            self.meta.read_project_md(),
            self.design_rules(),
            self.policy.mode.value,
            self.git_line(),
            self.skills.index_text(),
        )

    def system_prompt(self, mode: str) -> str:
        if mode == "plan":
            extra = "PLANNING ONLY: inspect the project with read tools, then call `plan` with ordered, verifiable steps. Do not modify files or run commands. Finish with `finish` and a short description of the plan."
            return build_system_prompt("coding", self.project_block(), extra)
        return build_system_prompt(mode, self.project_block())

    def state_text(self) -> str:
        assert self.ctx is not None
        return state_summary(
            self.ctx,
            model=str(self.model_for(self.mode) or "unset"),
            mode=self.mode,
            design_rules=self.design_rules(),
        )

    def _persist_state(self, reason: str) -> None:
        if self.ctx is None:
            return
        write_handoff(
            self.ctx,
            self.meta,
            model=str(self.model_for(self.mode) or "unset"),
            mode=self.mode,
            reason=reason,
            design_rules=self.design_rules(),
        )

    def save_state(self, reason: str = "manual save") -> str:
        self.require_project()
        assert self.ctx is not None
        return write_handoff(
            self.ctx,
            self.meta,
            model=str(self.model_for(self.mode) or "unset"),
            mode=self.mode,
            reason=reason,
            design_rules=self.design_rules(),
        )

    @property
    def effective_mode(self) -> str:
        return self.run_mode or self.mode

    @property
    def worker_running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def set_mode(self, mode: str) -> None:
        if mode not in ("chat", "coding"):
            raise WorkFlaskError("mode must be chat or coding")
        self.mode = mode
        if self.session is not None:
            self.sessions.set_mode(self.session.id, mode)

    def set_permission_mode(self, mode: PermissionMode, *, persist: bool = False) -> None:
        if persist:
            self.apply_settings(
                lambda s: s.model_copy(
                    update={"permissions": s.permissions.model_copy(update={"mode": mode})}
                )
            )
        self.override_session("permissions.mode", mode.value)
        if self.ctx is not None:
            self.ctx.db.audit(
                "permission_mode", {"mode": mode.value, "persist": persist}, self.ctx.session_id
            )

    def submit(self, text: str, mode: str | None = None) -> str:
        self.require_project()
        if self.model_for(mode or self.mode) is None:
            raise ConfigError("no model is configured", hint="run @setup or @changemodel")
        with self._qlock:
            if self.worker_running:
                self._queue.append((text, mode))
                return f"queued behind the running task ({len(self._queue)} waiting); use @btw for a side question"
            self._stop_reason = None
            self._worker = threading.Thread(
                target=self._work, args=(text, mode), name="agent-worker", daemon=True
            )
            self._worker.start()
            return "started"

    def _work(self, text: str, mode: str | None) -> None:
        while True:
            try:
                self._run_one(text, mode)
            except Exception as exc:
                self.console.error(
                    f"internal error while running the task: {type(exc).__name__}: {exc}",
                    hint="state was saved; try @resume",
                )
                self.last_result = RunResult("failed", error=str(exc))
            finally:
                self.run_mode = None
            with self._qlock:
                if not self._queue or self._stop_reason:
                    dropped = len(self._queue)
                    self._queue.clear()
                    if dropped and self._stop_reason:
                        self.console.dim(f"dropped {dropped} queued request(s) because the task was stopped")
                    self._worker = None
                    return
                text, mode = self._queue.pop(0)

    def _run_one(self, text: str, mode_override: str | None = None) -> None:
        assert self.loop is not None and self.ctx is not None and self.executor is not None
        mode = mode_override or self.mode
        self.run_mode = mode_override
        self.cancel_event.clear()
        self.ctx.extra.pop("last_test", None)
        journal_before = len(self.ctx.files.journal(10_000))
        if mode == "coding" and self.settings.git.auto_checkpoint and self.checkpoints and self.git:
            head = self.git.head() if self.git.is_repo() else None
            cp = self.checkpoints.create(
                f"before: {text[:60]}", git_head=head, tasks=self.ledger.snapshot() if self.ledger else []
            )
            self.console.dim(f"checkpoint {cp.id} saved")
        for problem in self.plugins.fire("before_task", request=text, mode=mode):
            self.console.warn(f"plugin hook failed: {problem}")
        result = self.loop.run(text, mode=mode, cancel=self.cancel_event)
        self.last_result = result
        self._report(result, mode, journal_before)
        self._persist_state(f"task {result.status}")
        if self.session:
            self.sessions.set_status(
                self.session.id, "active" if result.status in ("completed",) else result.status
            )
        for problem in self.plugins.fire("after_task", result=result, mode=mode):
            self.console.warn(f"plugin hook failed: {problem}")

    def _report(self, result: RunResult, mode: str, journal_before: int) -> None:
        c = self.console
        assert self.ctx is not None
        changed = len(self.ctx.files.journal(10_000)) - journal_before
        usage = self.tracker.totals(session=True)
        log.info(
            "task finished status=%s mode=%s model=%s requests=%d tools=%d seconds=%.1f",
            result.status,
            mode,
            result.model,
            result.requests,
            result.tool_calls,
            result.seconds,
        )
        tail = f"{result.requests} request(s), {result.tool_calls} tool call(s), {result.seconds:.1f}s"
        if result.tokens_saved >= 200 and result.tokens_raw:
            tail += f"  ·  saved ~{result.tokens_saved:,} input tokens ({100 * result.tokens_saved // result.tokens_raw}%)"
        if result.status == "completed":
            if mode in ("coding", "plan"):
                c.rule("done")
                if result.summary:
                    c.markdown(result.summary, "main", "summary")
                if self.ledger and self.ledger.list():
                    c.task_list(self.ledger.list())
                if changed > 0 and "last_test" not in self.ctx.extra:
                    c.warn(f"{changed} file change(s) were made but no test run was recorded in this task")
                c.dim(tail + f"  ·  session {self.tracker.format_totals(usage)}")
            else:
                c.dim(tail)
        elif result.status == "cancelled":
            c.info(f"cancelled. State saved ({tail}). Use @resume to continue.")
        elif result.status == "paused":
            c.warn("every configured model failed; progress is saved and the task is paused")
            for line in result.failures[:8]:
                c.dim("  " + line)
            c.info(
                "fix the cause (credentials, quota, network) or pick another model with @changemodel, then @resume"
            )
        elif result.status == "limit":
            c.warn(f"stopped: {result.error}. Progress is saved; adjust @limits and @resume if appropriate.")
        else:
            c.error(f"task failed: {result.error}", hint="progress is saved; see @status and @resume")

    def cancel(self, reason: str = "cancel") -> bool:
        if not self.worker_running:
            return False
        self._stop_reason = reason
        self.cancel_event.set()
        self.broker.abort()
        GLOBAL_TRACKER.kill_all()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return not self.worker_running

    def resume_task(self) -> str:
        self.require_project()
        assert self.ctx is not None and self.ledger is not None
        if self.worker_running:
            raise WorkFlaskError("a task is already running")
        if self.mode != "coding":
            raise WorkFlaskError("switch to @codingagent before resuming coding work")
        handoff = load_handoff(self.ctx)
        pending = [
            t
            for t in self.ledger.list()
            if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.FAILED)
        ]
        if not pending and not handoff:
            raise WorkFlaskError("nothing to resume: no unfinished tasks and no saved handoff")
        for t in self.ledger.list(TaskStatus.FAILED):
            self.ledger.set_status(t.id, TaskStatus.PENDING)
        self.reset_exhaustion()
        text = (
            "[resume] Continue the unfinished work from the saved state below. Verify the actual file and git state with your tools before acting; "
            "do not redo completed tasks and do not regenerate existing files.\n\n"
            + self.state_text()
            + "\n\n"
            + next_step_text(self.ctx)
        )
        return self.submit(text)

    def reset_exhaustion(self) -> None:
        self.router.reset_exhaustion()

    def ask_side(self, question: str):
        self.require_project()
        assert self.side is not None
        if self.model_for("chat") is None:
            raise ConfigError("no model is configured", hint="run @setup or @changemodel")
        return self.side.ask(question)

    def switch_model(
        self, text: str, *, role: str = "coding", force: bool = False, probe_tools: bool = True
    ) -> SwitchReport:
        try:
            ref = ModelRef.parse(text, self.settings.default_provider)
        except ValueError as exc:
            raise WorkFlaskError(str(exc), hint="use provider:model-id") from exc
        if ref.provider not in self.settings.providers:
            raise ConfigError(f"provider '{ref.provider}' is not configured", hint="run @providers")
        old = self.model_for("chat" if role == "chat" else "coding")
        report = SwitchReport(str(old) if old else None, str(ref))
        was_running = self.worker_running
        if self.ctx is not None:
            self._persist_state(f"before switching to {ref}")
            report.steps.append("task state saved")
        if was_running and role == "coding":
            self.cancel("model switch")
            if not self.wait(45):
                raise WorkFlaskError(
                    "the active request did not stop within 45s; the old model configuration was kept",
                    hint="try again shortly",
                )
            report.steps.append("active request stopped safely")
        if self.executor is not None:
            notes = self.executor.reconcile_interrupted()
            report.steps.append(
                "pending tool operations inspected"
                + (f" ({'; '.join(notes)})" if notes else " (none pending)")
            )
        if self.ctx is not None:
            self._persist_state(f"handoff to {ref}")
            report.steps.append("handoff summary saved")
        provider = self.providers.get(ref.provider)
        if provider._api_key is None and provider.config.kind.value != "custom":
            self._after_failed_switch(was_running)
            raise WorkFlaskError(
                f"no API key for provider '{ref.provider}'", hint="run @providers to add one"
            )
        try:
            models, _ = self.catalog.get(provider)
        except WorkFlaskError as exc:
            models = []
            report.warnings.append(f"could not fetch the model list ({exc}); capabilities unknown")
        except Exception as exc:
            models = []
            report.warnings.append(
                f"could not fetch the model list ({type(exc).__name__}); capabilities unknown"
            )
        info = next((m for m in models if m.id == ref.model_id), None)
        if info is None and models:
            report.warnings.append(
                "model id is not in the provider's catalogue; it may still work if the provider accepts it"
            )
        if role == "coding" and info is not None and info.supports_tools is False and not force:
            alternatives = [m.id for m in models if m.supports_tools][:5]
            self._after_failed_switch(was_running)
            raise WorkFlaskError(
                f"{ref} does not support tool calling, which the coding agent needs",
                hint="compatible alternatives: "
                + (", ".join(alternatives) or "none found in the catalogue")
                + "; use --force to override",
            )
        check = provider.validate_model(ref.model_id)
        if not check.ok:
            self._after_failed_switch(was_running)
            raise WorkFlaskError(
                f"{ref} failed validation: {check.detail}", hint="the previous model configuration was kept"
            )
        report.steps.append("new model validated with a minimal request")
        if role == "coding" and probe_tools:
            probe = provider.test_tool_calling(ref.model_id)
            if not probe.ok and not probe.inconclusive and not force:
                self._after_failed_switch(was_running)
                raise WorkFlaskError(
                    f"{ref} did not emit a usable tool call: {probe.detail}",
                    hint="use --force to switch anyway",
                )
            if not probe.ok:
                report.warnings.append(f"tool-calling probe: {probe.detail}")
            else:
                report.steps.append("tool calling probed successfully")
        field_name = "chat_model" if role == "chat" else "coding_model"
        self.apply_settings(lambda s: s.model_copy(update={field_name: ref}))
        report.steps.append("configuration updated")
        if self.loop is not None and role == "coding":
            self.loop.history.append(
                self._notice(
                    f"[system notice] The model was switched from {old} to {ref}. Continue the existing work; the files on disk and this state are authoritative:\n\n{self.state_text()}"
                )
            )
            self.loop._persist(self.loop.history[-1])
            report.steps.append("task context restored")
        if was_running and role == "coding" and self.mode == "coding":
            try:
                self.resume_task()
                report.resumed = True
                report.steps.append("continuing from the next unfinished step")
            except WorkFlaskError as exc:
                report.warnings.append(f"not resumed automatically: {exc}")
        return report

    @staticmethod
    def _notice(text: str):
        from workflaskcode.providers.types import Message

        return Message("user", text)

    def _after_failed_switch(self, was_running: bool) -> None:
        if self.ledger is not None:
            self.ledger.pause_in_progress()
        if was_running:
            self.console.warn(
                "the model switch failed; the old configuration is kept and the task is recoverable with @resume"
            )

    def status_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"mode": self.mode, "model": str(self.model_for(self.mode) or "no model")}
        if self.root is not None:
            info["project"] = self.root.name
            if (
                self.git is not None
                and self.settings.git.enabled
                and GitRepo.available()
                and self.git.is_repo()
            ):
                info["branch"] = self.git.current_branch()
                info["dirty"] = len(self.git.dirty_paths())
        if self.ledger is not None:
            counts = self.ledger.counts()
            info["tasks_total"] = sum(v for k, v in counts.items() if k != "cancelled")
            info["tasks_done"] = counts["completed"]
        if self.worker_running:
            info["working"] = "…"
        if self.broker.has_pending():
            info["pending_approval"] = True
        if self.side is not None and self.side.active():
            info["side_chats"] = len(self.side.active())
        if self.policy is not None:
            info["permissions"] = self.policy.mode.value
        try:
            info["cost"] = self.tracker.totals(session=True).cost
        except Exception:
            info["cost"] = None
        return info

    def shutdown(self, reason: str = "exit") -> None:
        if self.worker_running:
            self.cancel("exit")
            self.wait(10)
        if self.side is not None:
            self.side.cancel()
        stop_dev_servers()
        GLOBAL_TRACKER.kill_all()
        if self.ctx is not None:
            try:
                self._persist_state(reason)
            except Exception as exc:
                self.console.warn(f"could not save final state: {exc}")
        if self.session is not None:
            self.sessions.set_status(self.session.id, "saved")
        self.db.close()
