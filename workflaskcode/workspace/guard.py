from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from pathlib import Path

from workflaskcode.errors import PathSecurityError

SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "id_ecdsa",
    "credentials",
    "credentials.json",
    "*.cred.json",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "secrets.*",
    "*.kdbx",
)
SENSITIVE_DIRS = (".ssh", ".aws", ".gnupg", ".kube")


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.realpath(p))


def _inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    if any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_PATTERNS):
        return not name.endswith((".example", ".sample", ".template"))
    return any(part.lower() in SENSITIVE_DIRS for part in path.parts)


class WorkspaceGuard:
    def __init__(
        self,
        root: Path,
        extra_roots: Iterable[Path] = (),
        protected: Iterable[Path] = (),
    ) -> None:
        self.root = Path(os.path.realpath(root))
        self.extra_roots = [Path(os.path.realpath(p)) for p in extra_roots]
        self.protected = [Path(os.path.realpath(p)) for p in protected]

    @property
    def allowed_roots(self) -> list[Path]:
        return [self.root, *self.extra_roots]

    def authorize_extra_root(self, path: Path) -> Path:
        resolved = Path(os.path.realpath(path))
        if not resolved.is_dir():
            raise PathSecurityError(f"not a directory: {resolved}")
        if resolved not in self.extra_roots and resolved != self.root:
            self.extra_roots.append(resolved)
        return resolved

    def resolve(self, path: str | Path, *, write: bool = False) -> Path:
        raw = str(path)
        if not raw.strip():
            raise PathSecurityError("empty path")
        if "\x00" in raw:
            raise PathSecurityError("path contains a NUL byte")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        real_path = Path(os.path.realpath(candidate))
        real = os.path.normcase(str(real_path))
        for protected in self.protected:
            if _inside(real, os.path.normcase(str(protected))):
                raise PathSecurityError(
                    "that location belongs to the agent's private data and is not accessible to tools"
                )
        if not any(_inside(real, os.path.normcase(str(root))) for root in self.allowed_roots):
            raise PathSecurityError(
                f"path escapes the workspace: {raw}",
                hint="use @workspace to see authorised roots; add one with @workspace add <dir>",
            )
        if write and ".git" in real_path.parts:
            raise PathSecurityError("refusing to modify .git internals; use the git tools instead")
        return real_path

    def relative(self, path: Path) -> str:
        for root in self.allowed_roots:
            try:
                return str(path.relative_to(root)) or "."
            except ValueError:
                continue
        return str(path)

    def contains(self, path: str | Path) -> bool:
        try:
            self.resolve(path)
            return True
        except PathSecurityError:
            return False
