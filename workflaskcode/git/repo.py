from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from workflaskcode.errors import WorkFlaskError
from workflaskcode.security.permissions import filtered_env


class GitError(WorkFlaskError):
    pass


@dataclass(frozen=True)
class StatusEntry:
    index: str
    worktree: str
    path: str
    original: str | None = None

    @property
    def untracked(self) -> bool:
        return self.index == "?" and self.worktree == "?"

    @property
    def staged(self) -> bool:
        return self.index not in (" ", "?")

    @property
    def unstaged(self) -> bool:
        return self.worktree not in (" ", "?")

    @property
    def label(self) -> str:
        if self.untracked:
            return "untracked"
        parts = []
        if self.staged:
            parts.append("staged")
        if self.unstaged:
            parts.append("modified")
        return "+".join(parts) or "changed"


class GitRepo:
    def __init__(self, root: Path, timeout: float = 60.0) -> None:
        self.root = root
        self.timeout = timeout

    @staticmethod
    def available() -> bool:
        return shutil.which("git") is not None

    @staticmethod
    def _env() -> dict[str, str]:
        env = filtered_env(
            extra_keep={
                "GIT_AUTHOR_NAME",
                "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL",
                "XDG_CONFIG_HOME",
            }
        )
        for key, value in os.environ.items():
            if key.startswith("GIT_CONFIG") or key in ("GIT_SSH_COMMAND", "GIT_EXEC_PATH"):
                env[key] = value
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = env.get("LC_ALL", "C.UTF-8")
        return env

    def run(self, *args: str, check: bool = True, input_text: str | None = None) -> str:
        if not self.available():
            raise GitError("git is not installed", hint="install git and re-run")
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=self._env(),
                input=input_text,
                stdin=None if input_text is not None else subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out") from exc
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
        return proc.stdout

    def is_repo(self) -> bool:
        if not self.available():
            return False
        try:
            return self.run("rev-parse", "--is-inside-work-tree", check=False).strip() == "true"
        except GitError:
            return False

    def toplevel(self) -> Path | None:
        out = self.run("rev-parse", "--show-toplevel", check=False).strip()
        return Path(out) if out else None

    def init(self) -> None:
        self.run("init", "-b", "main")

    def head(self) -> str | None:
        out = self.run("rev-parse", "--verify", "-q", "HEAD", check=False).strip()
        return out or None

    def current_branch(self) -> str | None:
        out = self.run("branch", "--show-current", check=False).strip()
        return out or None

    def status(self) -> list[StatusEntry]:
        raw = self.run("status", "--porcelain=v1", "-z", "--untracked-files=all")
        entries: list[StatusEntry] = []
        parts = raw.split("\0")
        i = 0
        while i < len(parts):
            item = parts[i]
            i += 1
            if len(item) < 4:
                continue
            x, y, path = item[0], item[1], item[3:]
            original = None
            if x in "RC":
                original = parts[i] if i < len(parts) else None
                i += 1
            entries.append(StatusEntry(x, y, path, original))
        return entries

    def dirty_paths(self) -> set[str]:
        top = self.toplevel() or self.root
        return {str((top / e.path).resolve()) for e in self.status() if not e.untracked}

    def is_clean(self) -> bool:
        return not self.status()

    def diff(
        self,
        *,
        staged: bool = False,
        paths: list[str] | None = None,
        stat: bool = False,
        rev: str | None = None,
    ) -> str:
        args = ["diff", "--no-color", "--no-ext-diff"]
        if staged:
            args.append("--cached")
        if stat:
            args.append("--stat")
        if rev:
            args.append(rev)
        if paths:
            args += ["--", *paths]
        return self.run(*args)

    def full_diff_text(self) -> str:
        tracked = (
            self.run("diff", "--no-color", "--no-ext-diff", "HEAD", check=False)
            if self.head()
            else self.diff(staged=True) + self.diff()
        )
        untracked = []
        for e in self.status():
            if e.untracked:
                untracked.append(f"new file (untracked): {e.path}")
        return tracked + ("\n" + "\n".join(untracked) + "\n" if untracked else "")

    def log(self, count: int = 20, fmt: str = "%h %s", rev_range: str | None = None) -> list[str]:
        if not self.head():
            return []
        args = ["log", f"-{count}", f"--pretty=format:{fmt}"]
        if rev_range:
            args.append(rev_range)
        out = self.run(*args, check=False)
        return [ln for ln in out.splitlines() if ln]

    def branches(self) -> list[tuple[str, bool]]:
        out = self.run("branch", "--format=%(HEAD)|%(refname:short)", check=False)
        res = []
        for line in out.splitlines():
            flag, _, name = line.partition("|")
            if name:
                res.append((name, flag == "*"))
        return res

    _BRANCH_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,100}$")

    def create_branch(self, name: str, *, switch: bool = True) -> None:
        if not self._BRANCH_OK.match(name) or ".." in name or name.endswith((".lock", "/")):
            raise GitError(f"invalid branch name: {name!r}")
        if not self.head():
            raise GitError("the repository has no commits yet; commit once before branching")
        if switch:
            self.run("switch", "-c", name)
        else:
            self.run("branch", name)

    def switch_branch(self, name: str) -> None:
        if not self._BRANCH_OK.match(name):
            raise GitError(f"invalid branch name: {name!r}")
        if not self.is_clean():
            dirty = ", ".join(e.path for e in self.status()[:5])
            raise GitError(
                f"working tree has uncommitted changes ({dirty}); commit or stash before switching"
            )
        self.run("switch", name)

    def add(self, paths: list[str] | None = None) -> None:
        self.run("add", *(paths or ["-A"]))

    def commit(self, message: str, *, paths: list[str] | None = None) -> str:
        if not message.strip():
            raise GitError("commit message must not be empty")
        if paths:
            self.run("add", "--", *paths)
        if not self.run("diff", "--cached", "--name-only").strip():
            raise GitError("nothing staged to commit")
        self.run("commit", "-m", message)
        return self.head() or ""

    def stash_push(self, message: str) -> bool:
        out = self.run("stash", "push", "--include-untracked", "-m", message, check=False)
        return "No local changes" not in out

    def worktrees(self) -> list[tuple[str, str | None]]:
        out = self.run("worktree", "list", "--porcelain", check=False)
        res: list[tuple[str, str | None]] = []
        path = None
        branch = None
        for line in out.splitlines() + [""]:
            if line.startswith("worktree "):
                path = line[9:]
            elif line.startswith("branch "):
                branch = line[7:].removeprefix("refs/heads/")
            elif not line and path:
                res.append((path, branch))
                path, branch = None, None
        return res

    def add_worktree(self, path: Path, branch: str) -> None:
        if not self._BRANCH_OK.match(branch):
            raise GitError(f"invalid branch name: {branch!r}")
        self.run("worktree", "add", "-b", branch, str(path))

    def remove_worktree(self, path: Path) -> None:
        self.run("worktree", "remove", str(path))

    def changed_files_since(self, rev: str) -> list[str]:
        return [ln for ln in self.run("diff", "--name-only", rev, check=False).splitlines() if ln]

    def blame_authors(self, path: str) -> dict[str, int]:
        out = self.run("blame", "--line-porcelain", "--", path, check=False)
        counts: dict[str, int] = {}
        for line in out.splitlines():
            if line.startswith("author "):
                counts[line[7:]] = counts.get(line[7:], 0) + 1
        return counts

    def tags(self) -> list[str]:
        return [t for t in self.run("tag", "--sort=-creatordate", check=False).splitlines() if t]

    def remote_url(self) -> str | None:
        out = self.run("remote", "get-url", "origin", check=False).strip()
        return out or None

    def mark_ref(self, name: str) -> str | None:
        head = self.head()
        if head:
            self.run("update-ref", f"refs/workflaskcode/{name}", head, check=False)
        return head

    @staticmethod
    def conventional_message(entries: list[StatusEntry], diff_stat: str) -> str:
        if not entries:
            return "chore: no changes"
        paths = [e.path for e in entries]
        names = [Path(p).name for p in paths]
        test_only = all("test" in p.lower() for p in paths)
        docs_only = all(p.lower().endswith((".md", ".rst", ".txt")) for p in paths)
        only_new = all(e.untracked or e.index == "A" for e in entries)
        kind = "test" if test_only else "docs" if docs_only else "feat" if only_new else "chore"
        if kind == "chore" and any(e.unstaged or e.staged for e in entries) and not only_new:
            kind = "fix" if any(re.search(r"fix|bug|error", p, re.I) for p in paths) else "refactor"
        scope = ""
        tops = {Path(p).parts[0] for p in paths if len(Path(p).parts) > 1}
        if len(tops) == 1:
            scope = f"({tops.pop()})"
        subject = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        verb = {
            "feat": "add",
            "docs": "update",
            "test": "update",
            "fix": "fix",
            "refactor": "update",
            "chore": "update",
        }[kind]
        body = "\n\n" + diff_stat.strip() if diff_stat.strip() else ""
        return f"{kind}{scope}: {verb} {subject}{body}"
