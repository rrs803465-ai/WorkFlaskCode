from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
        if mode is not None:
            restrict_permissions(Path(tmp_name), mode)
        elif path.exists():
            with contextlib.suppress(OSError):
                os.chmod(tmp_name, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def restrict_permissions(path: Path, mode: int = 0o600) -> bool:
    if IS_WINDOWS:
        return False
    try:
        os.chmod(path, mode)
        return True
    except OSError:
        return False


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def read_text_safely(path: Path, limit_bytes: int = 2_000_000) -> str | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit_bytes + 1)
    except OSError:
        return None
    if len(data) > limit_bytes or looks_binary(data):
        return None
    return data.decode("utf-8", errors="replace")


def short_id(prefix: str = "") -> str:
    import secrets

    return f"{prefix}{secrets.token_hex(4)}"
