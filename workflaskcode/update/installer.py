from __future__ import annotations

import ast
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from workflaskcode.errors import WorkFlaskError
from workflaskcode.http.client import HttpClient, TransportError

MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
MAX_UNPACKED_BYTES = 150 * 1024 * 1024
MAX_MEMBERS = 6000
MIRRORED_DIRS = ("workflaskcode", "docs", "examples")
SINGLE_FILES = (
    "workflaskcode.py",
    "requirements.txt",
    "requirements-optional.txt",
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
)
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


class UpdateError(WorkFlaskError):
    pass


def install_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    if not (root / "workflaskcode.py").is_file() or not (root / "workflaskcode" / "__init__.py").is_file():
        raise UpdateError(
            "this copy was not installed from source, so @update cannot replace it",
            hint="clone the repository and run python3 workflaskcode.py from there",
        )
    return root


def download_archive(http: HttpClient, url: str, timeout: float = 90.0) -> bytes:
    try:
        response = http.request("GET", url, headers={"Accept": "application/x-gzip"}, timeout=timeout)
    except TransportError as exc:
        raise UpdateError(f"download failed: {exc}") from exc
    if not response.ok:
        raise UpdateError(f"download failed: HTTP {response.status}")
    declared = int(response.headers.get("content-length", "0") or 0)
    if declared > MAX_ARCHIVE_BYTES or len(response.body) > MAX_ARCHIVE_BYTES:
        raise UpdateError("the update archive is larger than the 60 MB safety limit; refusing it")
    return response.body


def extract_archive(data: bytes, dest: Path) -> tuple[Path, str | None]:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    total = 0
    commit = None
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise UpdateError(f"the download is not a valid archive: {exc}") from exc
    with archive:
        commit = (archive.pax_headers or {}).get("comment")
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise UpdateError("the archive has too many entries; refusing it")
        tops = {m.name.split("/", 1)[0] for m in members if m.name and m.name != "pax_global_header"}
        if len(tops) != 1:
            raise UpdateError("unexpected archive layout (expected one top-level folder)")
        for member in members:
            if member.name == "pax_global_header":
                continue
            parts = Path(member.name).parts
            if member.name.startswith("/") or ".." in parts or len(parts) < 1:
                raise UpdateError(f"unsafe path in archive: {member.name}")
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise UpdateError(f"links and device files are not allowed in an update: {member.name}")
            relative = Path(*parts[1:]) if len(parts) > 1 else None
            if relative is None:
                continue
            target = (root / relative).resolve()
            if root != target and root not in target.parents:
                raise UpdateError(f"unsafe path in archive: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            total += member.size
            if total > MAX_UNPACKED_BYTES:
                raise UpdateError("the archive unpacks to more than 150 MB; refusing it")
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = archive.extractfile(member)
            if handle is None:
                continue
            with handle, open(target, "wb") as out:
                shutil.copyfileobj(handle, out)
    return root, commit if commit and len(commit) == 40 else None


def validate_tree(new_root: Path) -> str:
    package = new_root / "workflaskcode"
    if not (new_root / "workflaskcode.py").is_file() or not (package / "__init__.py").is_file():
        raise UpdateError("the download does not look like WorkFlaskCode (launcher or package missing)")
    version = None
    for path in sorted(package.rglob("*.py")):
        if SKIP_PARTS & set(path.parts):
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            raise UpdateError(
                f"the downloaded file {path.relative_to(new_root)} does not parse: {exc}"
            ) from exc
    for line in (package / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            version = line.split("=", 1)[1].strip().strip("\"'")
    if not version:
        raise UpdateError("the downloaded package has no __version__")
    return version


def _files(base: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not base.is_dir():
        return found
    for path in base.rglob("*"):
        if path.is_file() and not (SKIP_PARTS & set(path.relative_to(base).parts)) and path.suffix != ".pyc":
            found[path.relative_to(base).as_posix()] = path
    return found


def _same(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _put(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".wfc-new")
    shutil.copy2(source, temp)
    os.replace(temp, target)


def snapshot(root: Path, backup_dir: Path) -> None:
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True)
    for name in MIRRORED_DIRS:
        for rel, path in _files(root / name).items():
            _put(path, backup_dir / name / rel)
    for name in SINGLE_FILES:
        if (root / name).is_file():
            _put(root / name, backup_dir / name)


def apply_tree(new_root: Path, root: Path) -> list[str]:
    changed: list[str] = []
    for name in MIRRORED_DIRS:
        fresh, current = _files(new_root / name), _files(root / name)
        for rel, path in fresh.items():
            target = root / name / rel
            if rel not in current or not _same(path, target):
                _put(path, target)
                changed.append(f"{name}/{rel}")
        for rel, path in current.items():
            if rel not in fresh:
                path.unlink()
                changed.append(f"{name}/{rel} (removed)")
        for folder in sorted((root / name).rglob("*"), reverse=True):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
    for name in SINGLE_FILES:
        source, target = new_root / name, root / name
        if source.is_file() and not (target.is_file() and _same(source, target)):
            _put(source, target)
            changed.append(name)
    return changed


def apply_with_rollback(new_root: Path, root: Path, backup_dir: Path) -> list[str]:
    snapshot(root, backup_dir)
    try:
        return apply_tree(new_root, root)
    except Exception as exc:
        try:
            apply_tree(backup_dir, root)
        except Exception as inner:
            raise UpdateError(
                f"the update failed ({exc}) and automatic restore also failed ({inner}); your previous files are in {backup_dir}"
            ) from exc
        raise UpdateError(f"the update failed and was rolled back: {exc}") from exc


def latest_backup(backups_dir: Path) -> Path | None:
    if not backups_dir.is_dir():
        return None
    options = sorted(p for p in backups_dir.iterdir() if p.is_dir() and p.name.startswith("update-"))
    return options[-1] if options else None


def git_pull(root: Path, timeout: float = 120.0) -> str:
    git = shutil.which("git")
    if not git:
        raise UpdateError("git is not installed, so this checkout cannot be updated in place")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    status = subprocess.run(
        [git, "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, timeout=30, env=env
    )
    if status.returncode != 0:
        raise UpdateError("could not read git status: " + status.stderr.strip()[:200])
    if status.stdout.strip():
        raise UpdateError(
            "you have uncommitted changes in this checkout",
            hint="commit or stash them, then run @update again",
        )
    pulled = subprocess.run(
        [git, "-C", str(root), "pull", "--ff-only"], capture_output=True, text=True, timeout=timeout, env=env
    )
    if pulled.returncode != 0:
        raise UpdateError("git pull failed: " + (pulled.stderr or pulled.stdout).strip()[:300])
    return pulled.stdout.strip()
