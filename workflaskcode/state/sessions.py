from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workflaskcode.errors import StateError
from workflaskcode.state.db import Database
from workflaskcode.utils.fs import short_id
from workflaskcode.utils.redact import redact


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    path: Path
    created: float
    last_opened: float
    metadata_location: str


@dataclass(frozen=True)
class SessionRecord:
    id: str
    project_id: str
    created: float
    updated: float
    mode: str
    title: str
    status: str


def _project(row) -> ProjectRecord:
    return ProjectRecord(
        row["id"],
        row["name"],
        Path(row["path"]),
        row["created"],
        row["last_opened"],
        row["metadata_location"],
    )


def _session(row) -> SessionRecord:
    return SessionRecord(
        row["id"], row["project_id"], row["created"], row["updated"], row["mode"], row["title"], row["status"]
    )


class ProjectRegistry:
    def __init__(self, db: Database) -> None:
        self.db = db

    def register(self, path: Path, name: str | None = None, metadata_location: str = "home") -> ProjectRecord:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise StateError(f"not a directory: {resolved}")
        existing = self.by_path(resolved)
        now = time.time()
        if existing:
            self.db.execute("UPDATE projects SET last_opened=? WHERE id=?", (now, existing.id))
            return self.get(existing.id)
        pid = re.sub(r"[^a-z0-9]+", "-", (name or resolved.name).lower()).strip("-")[:24] or "project"
        pid = f"{pid}-{short_id()}"
        self.db.execute(
            "INSERT INTO projects(id,name,path,created,last_opened,metadata_location) VALUES(?,?,?,?,?,?)",
            (pid, name or resolved.name, str(resolved), now, now, metadata_location),
        )
        return self.get(pid)

    def get(self, project_id: str) -> ProjectRecord | None:
        row = self.db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        return _project(row) if row else None

    def by_path(self, path: Path) -> ProjectRecord | None:
        row = self.db.one("SELECT * FROM projects WHERE path=?", (str(path.expanduser().resolve()),))
        return _project(row) if row else None

    def find(self, ref: str) -> ProjectRecord | None:
        row = self.db.one("SELECT * FROM projects WHERE id=?", (ref,))
        if row:
            return _project(row)
        rows = self.db.query("SELECT * FROM projects WHERE name=?", (ref,))
        if len(rows) == 1:
            return _project(rows[0])
        if len(rows) > 1:
            return None
        return self.by_path(Path(ref)) if ref else None

    def list(self) -> list[ProjectRecord]:
        return [_project(r) for r in self.db.query("SELECT * FROM projects ORDER BY last_opened DESC")]

    def remove(self, project_id: str) -> None:
        with self.db.tx():
            for table in ("messages",):
                self.db.execute(
                    f"DELETE FROM {table} WHERE session_id IN (SELECT id FROM sessions WHERE project_id=?)",
                    (project_id,),
                )
            self.db.execute("DELETE FROM sessions WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM projects WHERE id=?", (project_id,))

    def set_metadata_location(self, project_id: str, location: str) -> None:
        self.db.execute("UPDATE projects SET metadata_location=? WHERE id=?", (location, project_id))


class SessionStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, project_id: str, mode: str, title: str = "") -> SessionRecord:
        sid = short_id("s")
        now = time.time()
        self.db.execute(
            "INSERT INTO sessions(id,project_id,created,updated,mode,title,status) VALUES(?,?,?,?,?,?,'active')",
            (sid, project_id, now, now, mode, title),
        )
        return self.get(sid)

    def get(self, session_id: str) -> SessionRecord | None:
        row = self.db.one("SELECT * FROM sessions WHERE id=?", (session_id,))
        return _session(row) if row else None

    def latest_for_project(self, project_id: str) -> SessionRecord | None:
        row = self.db.one(
            "SELECT * FROM sessions WHERE project_id=? ORDER BY updated DESC, rowid DESC LIMIT 1",
            (project_id,),
        )
        return _session(row) if row else None

    def list(self, project_id: str | None = None, limit: int = 50) -> list[SessionRecord]:
        if project_id:
            rows = self.db.query(
                "SELECT * FROM sessions WHERE project_id=? ORDER BY updated DESC LIMIT ?",
                (project_id, limit),
            )
        else:
            rows = self.db.query("SELECT * FROM sessions ORDER BY updated DESC LIMIT ?", (limit,))
        return [_session(r) for r in rows]

    def set_mode(self, session_id: str, mode: str) -> None:
        self.db.execute("UPDATE sessions SET mode=?, updated=? WHERE id=?", (mode, time.time(), session_id))

    def set_status(self, session_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE sessions SET status=?, updated=? WHERE id=?", (status, time.time(), session_id)
        )

    def set_title(self, session_id: str, title: str) -> None:
        self.db.execute("UPDATE sessions SET title=? WHERE id=?", (title[:120], session_id))

    def add_message(
        self,
        session_id: str,
        channel: str,
        role: str,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO messages(session_id,channel,role,content,payload,ts) VALUES(?,?,?,?,?,?)",
            (
                session_id,
                channel,
                role,
                redact(content),
                json.dumps(payload, default=str) if payload is not None else None,
                now,
            ),
        )
        self.db.execute("UPDATE sessions SET updated=? WHERE id=?", (now, session_id))
        return int(cur.lastrowid or 0)

    def messages(
        self, session_id: str, channel: str = "main", limit: int | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE session_id=? AND channel=? ORDER BY id"
        rows = self.db.query(sql, (session_id, channel))
        if limit:
            rows = rows[-limit:]
        out = []
        for r in rows:
            item = {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "ts": r["ts"],
                "channel": r["channel"],
            }
            item["payload"] = json.loads(r["payload"]) if r["payload"] else None
            out.append(item)
        return out

    def search(self, query: str, project_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        escaped = query.replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        sql = (
            "SELECT m.id, m.session_id, m.channel, m.role, m.content, m.ts, s.title "
            "FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE m.content LIKE ? ESCAPE '\\' "
        )
        params: list[Any] = [like]
        if project_id:
            sql += "AND s.project_id=? "
            params.append(project_id)
        sql += "ORDER BY m.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.query(sql, params)]

    def delete_all(self) -> None:
        with self.db.tx():
            self.db.execute("DELETE FROM messages")
            self.db.execute("DELETE FROM sessions")
