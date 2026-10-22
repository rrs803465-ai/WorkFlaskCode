from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from workflaskcode.utils.fs import restrict_permissions

HOME_ENV = "WORKFLASKCODE_HOME"

SUBDIRS = (
    "providers",
    "projects",
    "sessions",
    "memory",
    "logs",
    "cache",
    "templates",
    "reports",
    "backups",
    "state",
    "plugins",
    "skills",
)


def default_home_path(env: dict[str, str] | None = None, platform: str | None = None) -> Path:
    env = os.environ if env is None else env
    platform = platform or sys.platform
    override = env.get(HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()
    user_home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    if platform == "darwin":
        return user_home / "Library" / "Application Support" / "WorkFlaskCode"
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA")
        root = Path(base) if base else user_home / "AppData" / "Local"
        return root / "WorkFlaskCode"
    return user_home / ".workflaskcode"


@dataclass(frozen=True)
class AgentHome:
    root: Path

    @classmethod
    def at(cls, path: Path | str | None = None) -> AgentHome:
        return cls(Path(path).expanduser().resolve() if path else default_home_path().resolve())

    def ensure(self) -> AgentHome:
        self.root.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRS:
            (self.root / name).mkdir(exist_ok=True)
        restrict_permissions(self.root, 0o700)
        restrict_permissions(self.providers_dir, 0o700)
        return self

    @property
    def exists(self) -> bool:
        return self.root.is_dir()

    @property
    def config_file(self) -> Path:
        return self.root / "config.toml"

    @property
    def providers_dir(self) -> Path:
        return self.root / "providers"

    @property
    def projects_dir(self) -> Path:
        return self.root / "projects"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def memory_dir(self) -> Path:
        return self.root / "memory"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def templates_dir(self) -> Path:
        return self.root / "templates"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def plugins_dir(self) -> Path:
        return self.root / "plugins"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "agent.db"

    def project_dir(self, project_id: str) -> Path:
        return self.projects_dir / project_id
