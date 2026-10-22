from __future__ import annotations

import copy
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from workflaskcode.config import toml_writer
from workflaskcode.config.models import SCHEMA_VERSION, ModelRef, Settings
from workflaskcode.config.paths import AgentHome
from workflaskcode.errors import ConfigError
from workflaskcode.utils.fs import atomic_write_text

Migration = Callable[[dict[str, Any]], dict[str, Any]]

MIGRATIONS: dict[int, Migration] = {}

PROJECT_OVERRIDABLE = {
    "mode",
    "coding_model",
    "chat_model",
    "fallback_models",
    "context_budget_tokens",
    "permissions",
    "limits",
    "browser",
    "git",
    "privacy",
    "metadata_location",
}


def migrate(
    raw: dict[str, Any],
    migrations: dict[int, Migration] | None = None,
    target: int | None = None,
) -> dict[str, Any]:
    migrations = MIGRATIONS if migrations is None else migrations
    target = SCHEMA_VERSION if target is None else target
    raw = copy.deepcopy(raw)
    try:
        version = int(raw.get("schema_version", 1))
    except (TypeError, ValueError) as exc:
        raise ConfigError("schema_version must be an integer") from exc
    if version > target:
        raise ConfigError(
            f"config.toml uses schema {version}, newer than this release supports ({target})",
            hint="upgrade WorkFlaskCode or restore an older config from backups/",
        )
    while version < target:
        step = migrations.get(version)
        if step is None:
            raise ConfigError(f"no migration from config schema {version}")
        raw = step(raw)
        version += 1
        raw["schema_version"] = version
    return raw


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def settings_from_dict(raw: dict[str, Any]) -> Settings:
    raw = copy.deepcopy(raw)
    for key in ("coding_model", "chat_model"):
        if isinstance(raw.get(key), str):
            raw[key] = ModelRef.parse(raw[key]).model_dump()
    if isinstance(raw.get("fallback_models"), list):
        raw["fallback_models"] = [
            ModelRef.parse(m).model_dump() if isinstance(m, str) else m for m in raw["fallback_models"]
        ]
    try:
        return Settings.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        raise ConfigError(f"invalid configuration: {_summarise(exc)}") from exc


def _summarise(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors()[:6]:
            loc = ".".join(str(p) for p in err["loc"]) or "(root)"
            parts.append(f"{loc}: {err['msg']}")
        return "; ".join(parts)
    return str(exc)


class ConfigStore:
    def __init__(self, home: AgentHome) -> None:
        self.home = home

    @property
    def path(self) -> Path:
        return self.home.config_file

    def exists(self) -> bool:
        return self.path.is_file()

    def load_raw(self) -> dict[str, Any]:
        if not self.exists():
            return {}
        try:
            with self.path.open("rb") as handle:
                return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            backup = self._quarantine()
            raise ConfigError(
                f"config.toml is not valid TOML: {exc}",
                hint=f"the broken file was kept as {backup.name}; run 'workflaskcode setup'",
            ) from exc

    def _quarantine(self) -> Path:
        backup = self.path.with_name(f"{self.path.name}.bad-{int(time.time())}")
        try:
            self.path.replace(backup)
        except OSError:
            return self.path
        return backup

    def load(self) -> Settings:
        raw = migrate(self.load_raw())
        return settings_from_dict(raw)

    def load_or_default(self) -> Settings:
        return self.load() if self.exists() else Settings()

    def save(self, settings: Settings) -> None:
        settings_from_dict(settings.to_toml_dict())
        if self.exists():
            self._backup_existing()
        header = (
            "# WorkFlaskCode configuration. Contains NO credentials.\n"
            "# Edit with 'workflaskcode setup' or '@config', or by hand.\n"
        )
        atomic_write_text(self.path, header + toml_writer.dumps(settings.to_toml_dict()))

    def _backup_existing(self) -> None:
        self.home.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.home.backups_dir / f"config-{stamp}.toml"
        try:
            target.write_bytes(self.path.read_bytes())
        except OSError:
            return
        backups = sorted(self.home.backups_dir.glob("config-*.toml"))
        for old in backups[:-10]:
            old.unlink(missing_ok=True)

    def effective(
        self,
        project_config: Path | None = None,
        session_overrides: dict[str, Any] | None = None,
    ) -> Settings:
        base = migrate(self.load_raw()) if self.exists() else {}
        if base:
            base = _stringify_models(base)
        merged = dict(base)
        if project_config and project_config.is_file():
            try:
                with project_config.open("rb") as handle:
                    proj = tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{project_config} is not valid TOML: {exc}") from exc
            forbidden = set(proj) - PROJECT_OVERRIDABLE - {"schema_version"}
            if forbidden:
                raise ConfigError(
                    f"project config may not set: {', '.join(sorted(forbidden))}",
                    hint="providers and credentials are global-only",
                )
            merged = deep_merge(merged, {k: v for k, v in proj.items() if k != "schema_version"})
        if session_overrides:
            merged = deep_merge(merged, session_overrides)
        return settings_from_dict(merged)


def _stringify_models(raw: dict[str, Any]) -> dict[str, Any]:
    return raw


def set_dotted(settings: Settings, dotted: str, value: str) -> Settings:
    raw = settings.to_toml_dict()
    cursor: Any = raw
    parts = dotted.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
        if not isinstance(cursor, dict):
            raise ConfigError(f"'{dotted}' does not address a settings table")
    leaf = parts[-1]
    if leaf in ("providers",) or parts[0] == "providers":
        raise ConfigError("provider settings are edited through '@providers' / 'setup'")
    old = cursor.get(leaf)
    cursor[leaf] = _coerce(value, old)
    return settings_from_dict(raw)


def _coerce(value: str, old: Any) -> Any:
    if isinstance(old, bool):
        if value.lower() in ("1", "true", "yes", "on"):
            return True
        if value.lower() in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"expected a boolean, got '{value}'")
    if isinstance(old, int):
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"expected an integer, got '{value}'") from exc
    if isinstance(old, float):
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"expected a number, got '{value}'") from exc
    if isinstance(old, list):
        return [v.strip() for v in value.split(",") if v.strip()]
    if old is None:
        low = value.lower()
        if low in ("true", "false"):
            return low == "true"
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    return value
