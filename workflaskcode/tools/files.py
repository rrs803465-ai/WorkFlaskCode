from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from workflaskcode.errors import PermissionDenied, ToolError
from workflaskcode.security.permissions import ApprovalRequest, ApprovalResult, Approver, Risk
from workflaskcode.state.db import Database
from workflaskcode.utils.fs import (
    atomic_write_bytes,
    looks_binary,
    read_text_safely,
    sha256_bytes,
    sha256_file,
    short_id,
)
from workflaskcode.workspace.guard import WorkspaceGuard, is_sensitive_path

IGNORED_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tox",
    ".next",
    ".cache",
    "target",
    ".idea",
    ".vscode",
    ".workflaskcode",
    "site-packages",
    ".eggs",
}
MAX_READ_BYTES = 400_000
MAX_SEARCH_FILE_BYTES = 1_500_000


@dataclass
class ChangeRecord:
    path: str
    op: str
    before_hash: str | None
    after_hash: str | None
    backup_path: str | None
    ts: float
    id: int | None = None
    checkpoint_id: str | None = None


@dataclass
class EditSummary:
    path: str
    op: str
    added: int = 0
    removed: int = 0
    note: str = ""

    def __str__(self) -> str:
        tail = f" (+{self.added}/-{self.removed})" if self.op in ("modify", "create") else ""
        return f"{self.op} {self.path}{tail}" + (f" - {self.note}" if self.note else "")


def unified_diff(old: str, new: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _count(old: str, new: str) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


class FileTools:
    def __init__(
        self,
        guard: WorkspaceGuard,
        db: Database,
        backup_dir: Path,
        *,
        session_id: str,
        approver: Approver,
        can_write: Callable[[], bool] = lambda: True,
        write_needs_approval: Callable[[], bool] = lambda: False,
        dirty_paths: Callable[[], set[str]] | None = None,
        task_id_getter: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.guard = guard
        self.db = db
        self.backup_dir = backup_dir
        self.session_id = session_id
        self.approver = approver
        self._can_write = can_write
        self._write_needs_approval = write_needs_approval
        self._write_remembered = False
        self._dirty_paths = dirty_paths
        self._task_id = task_id_getter
        self._seen: dict[str, str] = {}

    def list_dir(self, path: str = ".", *, depth: int = 1, limit: int = 300) -> str:
        base = self.guard.resolve(path)
        if not base.is_dir():
            raise ToolError(f"not a directory: {path}")
        lines: list[str] = []
        base_depth = len(base.parts)
        for current, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
            level = len(Path(current).parts) - base_depth
            if level >= depth:
                dirs[:] = []
            rel_dir = Path(current).relative_to(base)
            for d in dirs:
                lines.append(f"{(rel_dir / d).as_posix()}/".removeprefix("./"))
            for f in sorted(files):
                lines.append((rel_dir / f).as_posix().removeprefix("./"))
            if len(lines) >= limit:
                lines = lines[:limit] + [f"... (truncated at {limit} entries)"]
                break
        return "\n".join(lines) or "(empty directory)"

    def read_file(self, path: str, *, start: int = 1, end: int | None = None) -> str:
        real = self.guard.resolve(path)
        if not real.is_file():
            raise ToolError(f"not a file: {path}")
        if is_sensitive_path(real):
            request = ApprovalRequest(
                "sensitive-read",
                f"Read possibly sensitive file {self.guard.relative(real)}",
                "The contents would be sent to the model provider.",
                risk=Risk.MODIFY,
            )
            if not self.approver(request).approved:
                raise PermissionDenied(f"reading {self.guard.relative(real)} requires approval")
        raw = real.read_bytes()
        if looks_binary(raw):
            raise ToolError(f"{path} looks like a binary file ({len(raw)} bytes)")
        self._seen[str(real)] = sha256_bytes(raw)
        text = raw[: MAX_READ_BYTES * 4].decode("utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        end = total if end is None else min(end, total)
        start = max(1, start)
        window = lines[start - 1 : end]
        numbered = [f"{i:>5}\t{line}" for i, line in enumerate(window, start)]
        body = "\n".join(numbered)
        if len(body) > MAX_READ_BYTES:
            body = body[:MAX_READ_BYTES] + "\n[truncated: use start/end to read further]"
        return f"{self.guard.relative(real)} (lines {start}-{end} of {total})\n{body}"

    def search(
        self,
        pattern: str,
        path: str = ".",
        *,
        glob: str | None = None,
        regex: bool = False,
        ignore_case: bool = False,
        limit: int = 100,
    ) -> str:
        base = self.guard.resolve(path)
        flags = re.IGNORECASE if ignore_case else 0
        try:
            rx = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc
        hits: list[str] = []
        for file in self._walk_files(base):
            if (
                glob
                and not fnmatch.fnmatch(file.name, glob)
                and not fnmatch.fnmatch(self.guard.relative(file), glob)
            ):
                continue
            if is_sensitive_path(file):
                continue
            try:
                if file.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
            except OSError:
                continue
            text = read_text_safely(file, MAX_SEARCH_FILE_BYTES)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{self.guard.relative(file)}:{lineno}: {line.strip()[:200]}")
                    if len(hits) >= limit:
                        return "\n".join(hits) + f"\n... (stopped at {limit} matches)"
        return "\n".join(hits) or "no matches"

    def find_files(self, name_glob: str, path: str = ".", limit: int = 200) -> str:
        base = self.guard.resolve(path)
        out = []
        for file in self._walk_files(base):
            if fnmatch.fnmatch(file.name, name_glob) or fnmatch.fnmatch(self.guard.relative(file), name_glob):
                out.append(self.guard.relative(file))
                if len(out) >= limit:
                    break
        return "\n".join(out) or "no files matched"

    def find_symbol(self, name: str, path: str = ".", limit: int = 50) -> str:
        rx = re.compile(
            rf"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|func|fn|struct|interface|type|enum|trait)\s+{re.escape(name)}\b"
            rf"|^\s*(?:export\s+)?(?:const|let|var)\s+{re.escape(name)}\s*[=:]"
        )
        base = self.guard.resolve(path)
        hits: list[str] = []
        for file in self._walk_files(base):
            text = read_text_safely(file, MAX_SEARCH_FILE_BYTES)
            if text is None:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{self.guard.relative(file)}:{i}: {line.strip()[:160]}")
                    if len(hits) >= limit:
                        return "\n".join(hits)
        return "\n".join(hits) or f"no definition of '{name}' found (heuristic search)"

    def _walk_files(self, base: Path) -> Iterator[Path]:
        if base.is_file():
            yield base
            return
        for current, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
            for f in sorted(files):
                yield Path(current) / f

    def compare(self, path_a: str, path_b: str) -> str:
        a, b = self.guard.resolve(path_a), self.guard.resolve(path_b)
        ta, tb = read_text_safely(a), read_text_safely(b)
        if ta is None or tb is None:
            raise ToolError("both files must be readable text files")
        diff = "".join(
            difflib.unified_diff(
                ta.splitlines(keepends=True),
                tb.splitlines(keepends=True),
                fromfile=self.guard.relative(a),
                tofile=self.guard.relative(b),
            )
        )
        return diff or "files are identical"

    def _require_write(self) -> None:
        if not self._can_write():
            raise PermissionDenied("file writes are disabled in the current mode/permission policy")

    def _approve_write(self, op: str, real: Path, old: str | None, new: str | None) -> None:
        if self._write_remembered or not self._write_needs_approval():
            return
        rel = self.guard.relative(real)
        preview = "".join(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                (new or "").splitlines(keepends=True),
                f"a/{rel}",
                f"b/{rel}",
            )
        )
        lines = preview.splitlines()
        shown = "\n".join(lines[:24]) + (
            f"\n... {len(lines) - 24} more diff lines" if len(lines) > 24 else ""
        )
        request = ApprovalRequest(
            "file-write",
            f"{op} {rel}",
            shown,
            effects=[f"{op}s {rel} (a backup is kept for @undo)"],
            risk=Risk.MODIFY,
        )
        result = self.approver(request)
        if not result.approved:
            raise PermissionDenied(f"{op} of {rel} was not approved")
        if result.remember:
            self._write_remembered = True

    def _backup(self, real: Path) -> str | None:
        if not real.is_file():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"{int(time.time() * 1000)}-{short_id()}-{real.name}"
        shutil.copy2(real, target)
        return str(target)

    def _record(
        self, real: Path, op: str, before: str | None, after: str | None, backup: str | None
    ) -> ChangeRecord:
        ts = time.time()
        cur = self.db.execute(
            "INSERT INTO file_changes(session_id,task_id,path,op,before_hash,after_hash,backup_path,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (self.session_id, self._task_id(), str(real), op, before, after, backup, ts),
        )
        return ChangeRecord(str(real), op, before, after, backup, ts, id=int(cur.lastrowid or 0))

    def _check_not_clobbering(self, real: Path, expected_hash: str | None) -> None:
        current = sha256_file(real)
        if current is None:
            return
        known = expected_hash or self._seen.get(str(real))
        if known is not None and known != current:
            raise ToolError(
                f"{self.guard.relative(real)} changed on disk since it was last read "
                "(someone else edited it). Re-read the file and redo the edit."
            )
        if known is None and self._dirty_paths is not None:
            try:
                dirty = real in {Path(p) for p in self._dirty_paths()}
            except Exception:
                dirty = False
            if dirty:
                req = ApprovalRequest(
                    "overwrite-dirty",
                    f"{self.guard.relative(real)} has uncommitted changes that the agent has not seen",
                    "Editing it could mix agent edits with your unsaved work.",
                    risk=Risk.MODIFY,
                )
                if not self.approver(req).approved:
                    raise PermissionDenied(
                        f"{self.guard.relative(real)} has uncommitted user changes; read it first or approve the edit"
                    )

    def _write(
        self, real: Path, data: bytes, op: str, before: str | None, backup: str | None
    ) -> ChangeRecord:
        atomic_write_bytes(real, data)
        after = sha256_bytes(data)
        self._seen[str(real)] = after
        return self._record(real, op, before, after, backup)

    def create_file(self, path: str, content: str, *, overwrite: bool = False) -> EditSummary:
        self._require_write()
        real = self.guard.resolve(path, write=True)
        if real.is_dir():
            raise ToolError(f"{path} is a directory")
        existed = real.exists()
        if existed and not overwrite:
            raise ToolError(f"{self.guard.relative(real)} already exists; use edit_file or set overwrite")
        before = sha256_file(real) if existed else None
        if existed:
            self._check_not_clobbering(real, None)
        old_text = read_text_safely(real) or "" if existed else ""
        self._approve_write("modify" if existed else "create", real, old_text, content)
        backup = self._backup(real) if existed else None
        real.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        self._write(real, data, "modify" if existed else "create", before, backup)
        added, removed = _count(old_text, content)
        return EditSummary(self.guard.relative(real), "modify" if existed else "create", added, removed)

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
        expected_hash: str | None = None,
    ) -> EditSummary:
        self._require_write()
        real = self.guard.resolve(path, write=True)
        if not real.is_file():
            raise ToolError(f"{path} does not exist; use create_file")
        raw = real.read_bytes()
        if looks_binary(raw):
            raise ToolError("refusing to edit a binary file")
        if not old_text:
            raise ToolError("old_text must not be empty")
        text = raw.decode("utf-8", errors="strict") if _is_utf8(raw) else None
        if text is None:
            raise ToolError("file is not valid UTF-8; refusing to edit")
        self._check_not_clobbering(real, expected_hash)
        needle = old_text
        count = text.count(needle)
        if count == 0 and "\r\n" in text and "\n" in old_text and "\r\n" not in old_text:
            needle = old_text.replace("\n", "\r\n")
            new_text = new_text.replace("\n", "\r\n")
            count = text.count(needle)
        if count == 0:
            near = _closest_line(text, old_text)
            raise ToolError(
                "old_text was not found in the file" + (f"; closest line: {near!r}" if near else "")
            )
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_text matches {count} places; add surrounding context to make it unique or pass replace_all"
            )
        updated = text.replace(needle, new_text) if replace_all else text.replace(needle, new_text, 1)
        if updated == text:
            return EditSummary(self.guard.relative(real), "modify", 0, 0, "no change")
        before = sha256_bytes(raw)
        self._approve_write("modify", real, text, updated)
        backup = self._backup(real)
        self._write(real, updated.encode("utf-8"), "modify", before, backup)
        added, removed = _count(text, updated)
        return EditSummary(self.guard.relative(real), "modify", added, removed)

    def apply_patch(self, patch: str) -> list[EditSummary]:
        self._require_write()
        files = parse_unified_diff(patch)
        if not files:
            raise ToolError("no file changes found in the patch")
        staged: list[tuple[Path, str | None, str | None, str]] = []
        for fp in files:
            if fp.new_path == "/dev/null":
                real = self.guard.resolve(fp.old_path, write=True)
                old = real.read_text(encoding="utf-8") if real.is_file() else None
                if old is None:
                    raise ToolError(f"cannot delete {fp.old_path}: file does not exist")
                staged.append((real, old, None, "delete"))
                continue
            real = self.guard.resolve(fp.new_path, write=True)
            if fp.old_path == "/dev/null":
                if real.exists():
                    raise ToolError(f"patch creates {fp.new_path} but it already exists")
                old = None
            else:
                if not real.is_file():
                    raise ToolError(f"patch targets {fp.new_path} which does not exist")
                text = read_text_safely(real)
                if text is None:
                    raise ToolError(f"{fp.new_path} is not an editable text file")
                self._check_not_clobbering(real, None)
                old = text
            new = apply_hunks(old or "", fp.hunks, fp.new_path)
            staged.append((real, old, new, "create" if old is None else "modify"))
        summaries: list[EditSummary] = []
        for real, old, new, op in staged:
            if op == "delete":
                summaries.append(self.delete_file(self.guard.relative(real)))
                continue
            assert new is not None
            self._approve_write(op, real, old, new)
            before = sha256_file(real) if old is not None else None
            backup = self._backup(real) if old is not None else None
            real.parent.mkdir(parents=True, exist_ok=True)
            self._write(real, new.encode("utf-8"), op, before, backup)
            a, r = _count(old or "", new)
            summaries.append(EditSummary(self.guard.relative(real), op, a, r))
        return summaries

    def move_file(self, src: str, dst: str) -> EditSummary:
        self._require_write()
        s = self.guard.resolve(src, write=True)
        d = self.guard.resolve(dst, write=True)
        if not s.exists():
            raise ToolError(f"{src} does not exist")
        if d.exists():
            raise ToolError(f"{dst} already exists; refusing to overwrite")
        if s.is_dir():
            raise ToolError("moving directories is not supported by this tool; move files individually")
        self._approve_write("move", s, None, None)
        before = sha256_file(s)
        backup = self._backup(s)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        self._seen.pop(str(s), None)
        self._seen[str(d)] = before or ""
        self._record(s, "delete", before, None, backup)
        self._record(d, "create", None, before, None)
        return EditSummary(f"{self.guard.relative(s)} -> {self.guard.relative(d)}", "move")

    def delete_file(self, path: str, *, _approved: bool = False) -> EditSummary:
        self._require_write()
        real = self.guard.resolve(path, write=True)
        if not real.is_file():
            raise ToolError(f"{path} is not a file (directories are not deleted by this tool)")
        if not _approved:
            req = ApprovalRequest(
                "delete",
                f"Delete {self.guard.relative(real)}",
                "A backup is kept in the agent's backup store, so @undo can restore it.",
                effects=["deletes a file"],
                risk=Risk.DESTRUCTIVE,
                destructive=True,
            )
            result: ApprovalResult = self.approver(req)
            if not result.approved:
                raise PermissionDenied(f"deleting {self.guard.relative(real)} was not approved")
        before = sha256_file(real)
        backup = self._backup(real)
        real.unlink()
        self._seen.pop(str(real), None)
        self._record(real, "delete", before, None, backup)
        return EditSummary(self.guard.relative(real), "delete")

    def journal(self, limit: int = 50) -> list[ChangeRecord]:
        rows = self.db.query(
            "SELECT * FROM file_changes WHERE session_id=? ORDER BY id DESC LIMIT ?", (self.session_id, limit)
        )
        return [_row_to_change(r) for r in rows]

    def undo_last(self, count: int = 1) -> list[str]:
        self._require_write()
        rows = self.db.query(
            "SELECT * FROM file_changes WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (self.session_id, count),
        )
        return [self.revert_change(_row_to_change(row)) for row in rows]

    def revert_change(self, change: ChangeRecord) -> str:
        real = Path(change.path)
        rel = self.guard.relative(real)
        current = sha256_file(real) if real.exists() else None
        if current != change.after_hash:
            return (
                f"SKIPPED {rel}: it no longer matches the agent's last write "
                "(modified since); use @diff and resolve manually"
            )
        if change.op == "create":
            real.unlink(missing_ok=True)
            self._seen.pop(str(real), None)
            outcome = f"removed {rel} (it was created by the agent)"
        elif change.backup_path and Path(change.backup_path).is_file():
            atomic_write_bytes(real, Path(change.backup_path).read_bytes())
            self._seen[str(real)] = change.before_hash or ""
            outcome = f"restored {rel}"
        else:
            return f"SKIPPED {rel}: no backup is available"
        self.db.execute("DELETE FROM file_changes WHERE id=?", (change.id,))
        return outcome


def _row_to_change(r) -> ChangeRecord:
    return ChangeRecord(
        r["path"],
        r["op"],
        r["before_hash"],
        r["after_hash"],
        r["backup_path"],
        r["ts"],
        r["id"],
        r["checkpoint_id"],
    )


def _is_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _closest_line(text: str, needle: str) -> str | None:
    first = next((ln.strip() for ln in needle.splitlines() if ln.strip()), "")
    if not first:
        return None
    match = difflib.get_close_matches(first, [ln.strip() for ln in text.splitlines()], n=1, cutoff=0.6)
    return match[0][:160] if match else None


@dataclass
class Hunk:
    old_start: int
    old_len: int
    lines: list[str]


@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_prefix(path: str) -> str:
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_unified_diff(patch: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    lines = patch.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = FilePatch(_strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:]))
            files.append(current)
            hunk = None
            i += 2
            continue
        m = _HUNK.match(line)
        if m and current is not None:
            hunk = Hunk(int(m.group(1)), int(m.group(2) or 1), [])
            current.hunks.append(hunk)
        elif hunk is not None:
            if line.startswith((" ", "+", "-")):
                hunk.lines.append(line)
            elif line == "":
                hunk.lines.append(" ")
            elif line.startswith("\\"):
                pass
            else:
                hunk = None
        i += 1
    return [f for f in files if f.hunks]


def apply_hunks(old: str, hunks: list[Hunk], label: str) -> str:
    src = old.splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    for hunk in hunks:
        old_block = [ln[1:] for ln in hunk.lines if ln[:1] in (" ", "-")]
        new_block = [ln[1:] for ln in hunk.lines if ln[:1] in (" ", "+")]
        start = _locate(src, old_block, max(hunk.old_start - 1, 0), pos)
        if start is None:
            raise ToolError(
                f"hunk for {label} (starting at old line {hunk.old_start}) does not match the file; "
                "re-read the file and regenerate the patch"
            )
        out.extend(src[pos:start])
        ending = "\r\n" if src and src[0].endswith("\r\n") else "\n"
        out.extend(_with_endings(new_block, src[start : start + len(old_block)], ending))
        pos = start + len(old_block)
    out.extend(src[pos:])
    return "".join(out)


def _norm(line: str) -> str:
    return line.rstrip("\r\n")


def _locate(src: list[str], block: list[str], hint: int, floor: int) -> int | None:
    if not block:
        return min(max(hint, floor), len(src))
    target = [_norm(b) for b in block]
    candidates = sorted(range(floor, len(src) - len(block) + 1), key=lambda i: abs(i - hint))
    for i in candidates:
        if [_norm(s) for s in src[i : i + len(block)]] == target:
            return i
    target_ws = [t.strip() for t in target]
    for i in candidates:
        if [_norm(s).strip() for s in src[i : i + len(block)]] == target_ws:
            return i
    return None


def _with_endings(new_block: list[str], replaced: list[str], default: str) -> list[str]:
    out = []
    for idx, line in enumerate(new_block):
        text = _norm(line)
        if idx < len(replaced) and replaced[idx].endswith("\r\n"):
            ending = "\r\n"
        elif idx < len(replaced) and replaced[idx].endswith("\n"):
            ending = "\n"
        else:
            ending = (
                default
                if (line.endswith(("\n", "\r\n")) or idx < len(new_block) - 1)
                else ("\n" if line.endswith("\n") else "")
            )
        out.append(text + ending)
    return out
