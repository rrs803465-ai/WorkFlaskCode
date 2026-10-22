from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from workflaskcode.errors import StateError

_V1 = """
CREATE TABLE projects(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
    created REAL NOT NULL, last_opened REAL NOT NULL, metadata_location TEXT NOT NULL DEFAULT 'home'
);
CREATE TABLE sessions(
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    created REAL NOT NULL, updated REAL NOT NULL, mode TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX idx_sessions_project ON sessions(project_id, updated);
CREATE TABLE messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
    channel TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    payload TEXT, ts REAL NOT NULL
);
CREATE INDEX idx_messages_session ON messages(session_id, channel, id);
CREATE TABLE tasks(
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, project_id TEXT NOT NULL,
    parent_id TEXT, description TEXT NOT NULL, dependencies TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, assigned_model TEXT,
    relevant_files TEXT NOT NULL DEFAULT '[]', validation TEXT NOT NULL DEFAULT '[]',
    failure TEXT, next_action TEXT, checkpoint_id TEXT,
    verified INTEGER NOT NULL DEFAULT 0, ordinal INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_tasks_session ON tasks(session_id, ordinal);
CREATE TABLE checkpoints(
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, project_id TEXT NOT NULL,
    label TEXT NOT NULL, created REAL NOT NULL, git_head TEXT, manifest TEXT NOT NULL
);
CREATE TABLE tool_executions(
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT, tool TEXT NOT NULL,
    args_hash TEXT NOT NULL, args TEXT NOT NULL, status TEXT NOT NULL,
    expected TEXT, result TEXT, started REAL NOT NULL, finished REAL
);
CREATE INDEX idx_exec_dedupe ON tool_executions(session_id, task_id, tool, args_hash);
CREATE TABLE file_changes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, task_id TEXT,
    path TEXT NOT NULL, op TEXT NOT NULL, before_hash TEXT, after_hash TEXT,
    backup_path TEXT, ts REAL NOT NULL, checkpoint_id TEXT
);
CREATE INDEX idx_changes_session ON file_changes(session_id, id);
CREATE TABLE usage(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session_id TEXT, task_id TEXT,
    provider TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL, cost_estimated INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT, channel TEXT NOT NULL DEFAULT 'main'
);
CREATE INDEX idx_usage_ts ON usage(ts);
CREATE TABLE audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session_id TEXT,
    kind TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE kv(
    scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated REAL NOT NULL,
    PRIMARY KEY(scope, key)
);
"""

MIGRATIONS: list[tuple[int, str]] = [(1, _V1)]


class Database:
    def __init__(self, path: Path | str, migrations: list[tuple[int, str]] | None = None) -> None:
        self.path = str(path)
        self._migrations = MIGRATIONS if migrations is None else migrations
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._tx_depth = 0
        with contextlib.suppress(sqlite3.DatabaseError):
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @property
    def version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        latest = max((v for v, _ in self._migrations), default=0)
        current = self.version
        if current > latest:
            raise StateError(
                f"state database is schema v{current}; this release supports up to v{latest}",
                hint="upgrade WorkFlaskCode or move state/agent.db aside",
            )
        for target, script in sorted(self._migrations):
            if target <= current:
                continue
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    for statement in _split_script(script):
                        self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version={int(target)}")
                    self._conn.execute("COMMIT")
                except sqlite3.Error as exc:
                    with contextlib.suppress(sqlite3.Error):
                        self._conn.execute("ROLLBACK")
                    raise StateError(f"migration to v{target} failed: {exc}") from exc

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            outer = self._tx_depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield self._conn
            except BaseException:
                self._tx_depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._conn.close()

    def kv_set(self, scope: str, key: str, value: Any) -> None:
        import time

        self.execute(
            "INSERT INTO kv(scope,key,value,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (scope, key, json.dumps(value), time.time()),
        )

    def kv_get(self, scope: str, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE scope=? AND key=?", (scope, key))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def kv_delete(self, scope: str, key: str) -> None:
        self.execute("DELETE FROM kv WHERE scope=? AND key=?", (scope, key))

    def audit(self, kind: str, detail: dict[str, Any] | str, session_id: str | None = None) -> None:
        import time

        from workflaskcode.utils.redact import redact

        text = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        self.execute(
            "INSERT INTO audit_log(ts,session_id,kind,detail) VALUES(?,?,?,?)",
            (time.time(), session_id, kind, redact(text)),
        )


def _split_script(script: str) -> list[str]:
    statements = []
    for part in script.split(";"):
        stripped = part.strip()
        if stripped:
            statements.append(stripped)
    return statements
