from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from workflaskcode.errors import StateError
from workflaskcode.state.db import Database
from workflaskcode.utils.fs import short_id


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

ALLOWED = {
    TaskStatus.PENDING: {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
        TaskStatus.PENDING,
    },
    TaskStatus.BLOCKED: {TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.FAILED: {TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.CANCELLED: {TaskStatus.PENDING},
}


@dataclass
class Task:
    id: str
    session_id: str
    project_id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    parent_id: str | None = None
    dependencies: list[str] = field(default_factory=list)
    created: float = 0.0
    updated: float = 0.0
    attempts: int = 0
    assigned_model: str | None = None
    relevant_files: list[str] = field(default_factory=list)
    validation: list[dict[str, Any]] = field(default_factory=list)
    failure: str | None = None
    next_action: str | None = None
    checkpoint_id: str | None = None
    verified: bool = False
    ordinal: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["status"] = self.status.value
        return out


def _task(row) -> Task:
    return Task(
        id=row["id"],
        session_id=row["session_id"],
        project_id=row["project_id"],
        description=row["description"],
        status=TaskStatus(row["status"]),
        parent_id=row["parent_id"],
        dependencies=json.loads(row["dependencies"]),
        created=row["created"],
        updated=row["updated"],
        attempts=row["attempts"],
        assigned_model=row["assigned_model"],
        relevant_files=json.loads(row["relevant_files"]),
        validation=json.loads(row["validation"]),
        failure=row["failure"],
        next_action=row["next_action"],
        checkpoint_id=row["checkpoint_id"],
        verified=bool(row["verified"]),
        ordinal=row["ordinal"],
    )


class TaskLedger:
    def __init__(self, db: Database, session_id: str, project_id: str) -> None:
        self.db = db
        self.session_id = session_id
        self.project_id = project_id

    def add(
        self,
        description: str,
        *,
        parent_id: str | None = None,
        dependencies: list[str] | None = None,
        relevant_files: list[str] | None = None,
        next_action: str | None = None,
    ) -> Task:
        description = description.strip()
        if not description:
            raise StateError("task description must not be empty")
        for dep in dependencies or []:
            if self.get(dep) is None:
                raise StateError(f"unknown dependency task '{dep}'")
        now = time.time()
        tid = short_id("t")
        with self.db.tx():
            row = self.db.one(
                "SELECT COALESCE(MAX(ordinal),0)+1 AS n FROM tasks WHERE session_id=?", (self.session_id,)
            )
            self.db.execute(
                "INSERT INTO tasks(id,session_id,project_id,parent_id,description,dependencies,status,"
                "created,updated,relevant_files,next_action,ordinal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    self.session_id,
                    self.project_id,
                    parent_id,
                    description,
                    json.dumps(dependencies or []),
                    TaskStatus.PENDING.value,
                    now,
                    now,
                    json.dumps(relevant_files or []),
                    next_action,
                    row["n"],
                ),
            )
        return self.get(tid)

    def get(self, task_id: str) -> Task | None:
        row = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return _task(row) if row else None

    def resolve(self, ref: str) -> Task | None:
        task = self.get(ref)
        if task:
            return task
        if ref.isdigit():
            row = self.db.one(
                "SELECT * FROM tasks WHERE session_id=? AND ordinal=?", (self.session_id, int(ref))
            )
            return _task(row) if row else None
        return None

    def list(self, status: TaskStatus | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks WHERE session_id=?"
        params: list[Any] = [self.session_id]
        if status:
            sql += " AND status=?"
            params.append(status.value)
        sql += " ORDER BY ordinal"
        return [_task(r) for r in self.db.query(sql, params)]

    def current(self) -> Task | None:
        row = self.db.one(
            "SELECT * FROM tasks WHERE session_id=? AND status=? ORDER BY ordinal LIMIT 1",
            (self.session_id, TaskStatus.IN_PROGRESS.value),
        )
        return _task(row) if row else None

    def ready(self) -> list[Task]:
        done = {t.id for t in self.list(TaskStatus.COMPLETED)}
        return [
            t
            for t in self.list()
            if t.status in (TaskStatus.PENDING, TaskStatus.FAILED) and all(d in done for d in t.dependencies)
        ]

    def next_ready(self) -> Task | None:
        ready = self.ready()
        return ready[0] if ready else None

    def _update(self, task_id: str, **fields: Any) -> Task:
        if not fields:
            return self.get(task_id)
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
        return self.get(task_id)

    def set_status(self, task_id: str, status: TaskStatus, *, failure: str | None = None) -> Task:
        task = self.get(task_id)
        if task is None:
            raise StateError(f"unknown task '{task_id}'")
        if status is task.status:
            return task
        if status not in ALLOWED[task.status]:
            raise StateError(f"cannot move a task from {task.status.value} to {status.value}")
        if status is TaskStatus.IN_PROGRESS:
            done = {t.id for t in self.list(TaskStatus.COMPLETED)}
            missing = [d for d in task.dependencies if d not in done]
            if missing:
                raise StateError(f"task depends on unfinished tasks: {', '.join(missing)}")
            return self._update(task_id, status=status.value, attempts=task.attempts + 1, failure=None)
        if status is TaskStatus.COMPLETED and not task.verified:
            raise StateError(
                "a task can only be completed after a passing validation result is recorded",
                hint="run the tests/validation, or mark it verified through @tasks done --force",
            )
        return self._update(task_id, status=status.value, failure=failure if failure else task.failure)

    def record_validation(
        self, task_id: str, name: str, passed: bool, detail: str = "", *, counts_as_verification: bool = True
    ) -> Task:
        task = self.get(task_id)
        if task is None:
            raise StateError(f"unknown task '{task_id}'")
        entry = {"name": name, "passed": passed, "detail": detail[:2000], "ts": time.time()}
        validation = [*task.validation, entry][-30:]
        verified = task.verified
        if counts_as_verification:
            verified = passed and all(v["passed"] for v in validation[-1:])
        return self._update(task_id, validation=json.dumps(validation), verified=int(verified))

    def force_verified(self, task_id: str, reason: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise StateError(f"unknown task '{task_id}'")
        entry = {"name": "manual", "passed": True, "detail": f"user override: {reason}", "ts": time.time()}
        return self._update(task_id, validation=json.dumps([*task.validation, entry][-30:]), verified=1)

    def complete(self, task_id: str) -> Task:
        return self.set_status(task_id, TaskStatus.COMPLETED)

    def fail(self, task_id: str, reason: str) -> Task:
        return self.set_status(task_id, TaskStatus.FAILED, failure=reason)

    def set_next_action(self, task_id: str, text: str | None) -> Task:
        return self._update(task_id, next_action=text)

    def set_model(self, task_id: str, model: str) -> Task:
        return self._update(task_id, assigned_model=model)

    def set_files(self, task_id: str, files: list[str]) -> Task:
        return self._update(task_id, relevant_files=json.dumps(sorted(set(files))))

    def set_checkpoint(self, task_id: str, checkpoint_id: str) -> Task:
        return self._update(task_id, checkpoint_id=checkpoint_id)

    def cancel_open(self) -> int:
        n = 0
        for t in self.list():
            if t.status in (TaskStatus.IN_PROGRESS, TaskStatus.PENDING, TaskStatus.BLOCKED):
                self.set_status(t.id, TaskStatus.CANCELLED)
                n += 1
        return n

    def pause_in_progress(self) -> int:
        n = 0
        for t in self.list(TaskStatus.IN_PROGRESS):
            self._update(t.id, status=TaskStatus.PENDING.value, next_action=t.next_action or "resume")
            n += 1
        return n

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in TaskStatus}
        for t in self.list():
            out[t.status.value] += 1
        return out

    def render(self) -> str:
        tasks = self.list()
        if not tasks:
            return "no tasks"
        marks = {
            TaskStatus.PENDING: "[ ]",
            TaskStatus.IN_PROGRESS: "[~]",
            TaskStatus.COMPLETED: "[x]",
            TaskStatus.FAILED: "[!]",
            TaskStatus.BLOCKED: "[#]",
            TaskStatus.CANCELLED: "[-]",
        }
        lines = []
        for t in tasks:
            extra = ""
            if t.status is TaskStatus.COMPLETED and not t.verified:
                extra = " (unverified)"
            if t.failure and t.status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
                extra += f" - {t.failure[:80]}"
            lines.append(f"{marks[t.status]} {t.ordinal}. {t.description}{extra}")
        return "\n".join(lines)

    def snapshot(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self.list()]
