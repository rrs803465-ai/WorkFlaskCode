from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from workflaskcode.errors import StateError
from workflaskcode.state.db import Database
from workflaskcode.tools.files import FileTools, _row_to_change
from workflaskcode.utils.fs import short_id


@dataclass
class Checkpoint:
    id: str
    label: str
    created: float
    git_head: str | None
    journal_mark: int
    tasks: list[dict]


class CheckpointManager:
    def __init__(self, db: Database, session_id: str, project_id: str, files: FileTools) -> None:
        self.db = db
        self.session_id = session_id
        self.project_id = project_id
        self.files = files

    def create(
        self, label: str, *, git_head: str | None = None, tasks: list[dict] | None = None
    ) -> Checkpoint:
        row = self.db.one(
            "SELECT COALESCE(MAX(id),0) AS m FROM file_changes WHERE session_id=?", (self.session_id,)
        )
        mark = int(row["m"])
        cid = short_id("cp")
        manifest = {"journal_mark": mark, "tasks": tasks or []}
        self.db.execute(
            "INSERT INTO checkpoints(id,session_id,project_id,label,created,git_head,manifest) VALUES(?,?,?,?,?,?,?)",
            (
                cid,
                self.session_id,
                self.project_id,
                label[:200],
                time.time(),
                git_head,
                json.dumps(manifest, default=str),
            ),
        )
        return Checkpoint(cid, label, time.time(), git_head, mark, tasks or [])

    def get(self, ref: str) -> Checkpoint | None:
        row = self.db.one("SELECT * FROM checkpoints WHERE id=? AND session_id=?", (ref, self.session_id))
        if row is None:
            return None
        manifest = json.loads(row["manifest"])
        return Checkpoint(
            row["id"],
            row["label"],
            row["created"],
            row["git_head"],
            manifest["journal_mark"],
            manifest.get("tasks", []),
        )

    def list(self) -> list[Checkpoint]:
        rows = self.db.query(
            "SELECT id FROM checkpoints WHERE session_id=? ORDER BY created DESC", (self.session_id,)
        )
        return [c for c in (self.get(r["id"]) for r in rows) if c]

    def latest(self) -> Checkpoint | None:
        items = self.list()
        return items[0] if items else None

    def changes_since(self, checkpoint: Checkpoint):
        rows = self.db.query(
            "SELECT * FROM file_changes WHERE session_id=? AND id>? ORDER BY id DESC",
            (self.session_id, checkpoint.journal_mark),
        )
        return [_row_to_change(r) for r in rows]

    def preview_rollback(self, ref: str | None = None) -> tuple[Checkpoint, list[str]]:
        cp = self.get(ref) if ref else self.latest()
        if cp is None:
            raise StateError("no such checkpoint" if ref else "no checkpoints exist yet")
        lines = []
        for change in self.changes_since(cp):
            lines.append(f"{change.op:7} {self.files.guard.relative(Path(change.path))}")
        return cp, lines

    def rollback(self, ref: str | None = None) -> tuple[Checkpoint, list[str]]:
        cp = self.get(ref) if ref else self.latest()
        if cp is None:
            raise StateError("no such checkpoint" if ref else "no checkpoints exist yet")
        results = [self.files.revert_change(change) for change in self.changes_since(cp)]
        return cp, results
