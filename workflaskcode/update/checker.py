from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from workflaskcode import __version__
from workflaskcode.config.models import UpdateSettings
from workflaskcode.errors import WorkFlaskError
from workflaskcode.http.client import HttpClient, TransportError

CHECK_TIMEOUT = 4.0
STATE_FILE = "update-state.json"
VERSION_LINE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")
SHA = re.compile(r"^[0-9a-f]{40}$")
OPT_OUT_ENV = "WORKFLASKCODE_NO_UPDATE_CHECK"


class UpdateCheckError(WorkFlaskError):
    pass


@dataclass(frozen=True)
class Endpoints:
    api: str = "https://api.github.com"
    raw: str = "https://raw.githubusercontent.com"
    archive: str = "https://codeload.github.com"


@dataclass
class UpdateInfo:
    repo: str
    branch: str
    local: str
    remote: str
    sha: str | None
    newer: bool
    reason: str

    def headline(self) -> str:
        if self.reason == "new commits":
            return f"new update available: {self.local} has new commits upstream. Type @update to install it."
        return f"new update available: {self.local} -> {self.remote}. Type @update to install it."


def parse_version(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", text.split("+")[0].split("-")[0])
    return tuple(int(p) for p in parts[:4]) or (0,)


def read_state(home_root: Path) -> dict:
    try:
        return json.loads((home_root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(home_root: Path, **fields: object) -> None:
    state = read_state(home_root)
    state.update(fields)
    home_root.mkdir(parents=True, exist_ok=True)
    tmp = home_root / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, home_root / STATE_FILE)


def _get(http: HttpClient, url: str, headers: dict[str, str] | None = None, timeout: float = CHECK_TIMEOUT):
    try:
        return http.request("GET", url, headers=headers, timeout=timeout)
    except TransportError as exc:
        raise UpdateCheckError(f"could not reach {url.split('/')[2]}: {exc}") from exc


def resolve_branch(http: HttpClient, cfg: UpdateSettings, endpoints: Endpoints, timeout: float) -> str:
    if cfg.branch:
        return cfg.branch
    response = _get(
        http, f"{endpoints.api}/repos/{cfg.repo}", {"Accept": "application/vnd.github+json"}, timeout
    )
    if response.ok:
        try:
            branch = response.json().get("default_branch")
        except ValueError:
            branch = None
        if isinstance(branch, str) and re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", branch):
            return branch
    return "main"


def fetch_remote_version(
    http: HttpClient, cfg: UpdateSettings, endpoints: Endpoints, branch: str, timeout: float
) -> tuple[str, str]:
    candidates = [branch] if cfg.branch else [branch, *[b for b in ("main", "master") if b != branch]]
    last = "no response"
    for name in candidates:
        response = _get(http, f"{endpoints.raw}/{cfg.repo}/{name}/workflaskcode/__init__.py", None, timeout)
        if response.ok:
            match = VERSION_LINE.search(response.text())
            if match:
                return match.group(1), name
            last = "the remote __init__.py has no __version__"
        else:
            last = f"HTTP {response.status} for branch {name}"
    raise UpdateCheckError(f"could not read the upstream version ({last}); check updates.repo in the config")


def fetch_remote_sha(
    http: HttpClient, cfg: UpdateSettings, endpoints: Endpoints, branch: str, timeout: float
) -> str | None:
    try:
        response = _get(
            http,
            f"{endpoints.api}/repos/{cfg.repo}/commits/{branch}",
            {"Accept": "application/vnd.github.sha"},
            timeout,
        )
    except UpdateCheckError:
        return None
    sha = response.text().strip() if response.ok else ""
    return sha if SHA.match(sha) else None


def check_for_update(
    http: HttpClient,
    cfg: UpdateSettings,
    home_root: Path,
    endpoints: Endpoints = Endpoints(),
    timeout: float = CHECK_TIMEOUT,
    local_version: str = __version__,
) -> UpdateInfo:
    branch = resolve_branch(http, cfg, endpoints, timeout)
    remote, branch = fetch_remote_version(http, cfg, endpoints, branch, timeout)
    sha = fetch_remote_sha(http, cfg, endpoints, branch, timeout)
    state = read_state(home_root)
    local_v, remote_v = parse_version(local_version), parse_version(remote)
    if remote_v > local_v:
        newer, reason = True, "newer version"
    elif remote_v == local_v and sha and state.get("sha") and state["sha"] != sha:
        newer, reason = True, "new commits"
    else:
        newer, reason = False, "up to date"
    return UpdateInfo(cfg.repo, branch, local_version, remote, sha, newer, reason)


class UpdateNotifier:
    def __init__(
        self, http: HttpClient, cfg: UpdateSettings, home_root: Path, endpoints: Endpoints = Endpoints()
    ) -> None:
        self.http = http
        self.cfg = cfg
        self.home_root = home_root
        self.endpoints = endpoints
        self.info: UpdateInfo | None = None
        self.error: str | None = None
        self._announced = False
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg.check_on_start and not os.environ.get(OPT_OUT_ENV)

    def start(self) -> None:
        if not self.enabled:
            self._done.set()
            return
        self._thread = threading.Thread(target=self._run, name="update-check", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.info = check_for_update(self.http, self.cfg, self.home_root, self.endpoints)
        except WorkFlaskError as exc:
            self.error = str(exc)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._done.set()

    def wait(self, timeout: float) -> UpdateInfo | None:
        self._done.wait(timeout)
        return self.take()

    def take(self) -> UpdateInfo | None:
        if self._announced or not self._done.is_set() or not self.info or not self.info.newer:
            return None
        self._announced = True
        return self.info


def fetch_changelog(
    http: HttpClient,
    cfg: UpdateSettings,
    endpoints: Endpoints,
    branch: str,
    newer_than: str,
    timeout: float = CHECK_TIMEOUT,
) -> list[str]:
    try:
        response = _get(http, f"{endpoints.raw}/{cfg.repo}/{branch}/CHANGELOG.md", None, timeout)
    except UpdateCheckError:
        return []
    if not response.ok:
        return []
    floor = parse_version(newer_than)
    notes: list[str] = []
    include = False
    for line in response.text().splitlines():
        if line.startswith("## "):
            include = parse_version(line[3:]) > floor
            if not include and notes:
                break
        if include and line.strip():
            notes.append(line.rstrip()[:160])
        if len(notes) >= 24:
            break
    return notes
