from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from workflaskcode.config.models import ProviderConfig, ProviderKind
from workflaskcode.config.paths import AgentHome
from workflaskcode.errors import CredentialError, CredentialStoreUnavailable
from workflaskcode.utils.fs import atomic_write_text, restrict_permissions
from workflaskcode.utils.redact import mask_secret, register_secret

SERVICE = "workflaskcode"

STANDARD_ENV = {
    ProviderKind.OPENROUTER: "OPENROUTER_API_KEY",
    ProviderKind.OPENAI: "OPENAI_API_KEY",
    ProviderKind.ANTHROPIC: "ANTHROPIC_API_KEY",
}


@dataclass(frozen=True)
class Credential:
    value: str
    source: str

    @property
    def masked(self) -> str:
        return mask_secret(self.value)


def _load_keyring():
    try:
        import keyring

        return keyring
    except Exception:
        return None


def _env_names(name: str, provider: ProviderConfig | None) -> list[str]:
    names = [f"WORKFLASKCODE_KEY_{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}"]
    if provider:
        if provider.env_var:
            names.append(provider.env_var)
        std = STANDARD_ENV.get(provider.kind)
        if std:
            names.append(std)
    return names


class CredentialStore:
    def __init__(self, home: AgentHome, keyring_backend: object | None = None, env=None) -> None:
        self.home = home
        self._keyring = keyring_backend if keyring_backend is not None else _load_keyring()
        self._env = os.environ if env is None else env

    def get(self, name: str, provider: ProviderConfig | None = None) -> Credential | None:
        for env_name in _env_names(name, provider):
            value = self._env.get(env_name)
            if value:
                register_secret(value)
                return Credential(value, f"env:{env_name}")
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(SERVICE, name)
            except Exception:
                value = None
            if value:
                register_secret(value)
                return Credential(value, "keyring")
        path = self._file_path(name)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                value = data.get("api_key")
            except (OSError, ValueError):
                value = None
            if value:
                register_secret(value)
                return Credential(value, "file")
        return None

    def require(self, name: str, provider: ProviderConfig | None = None) -> Credential:
        cred = self.get(name, provider)
        if cred is None:
            raise CredentialError(
                f"no API key for provider '{name}'",
                hint=f"run 'workflaskcode setup', or set {_env_names(name, provider)[-1]}",
            )
        return cred

    @property
    def keyring_available(self) -> bool:
        return self._keyring is not None

    def set(self, name: str, value: str, *, allow_file_fallback: bool = False) -> str:
        value = value.strip()
        if not value:
            raise CredentialError("API key must not be empty")
        register_secret(value)
        if self._keyring is not None:
            try:
                self._keyring.set_password(SERVICE, name, value)
                self._remove_file(name)
                return "keyring"
            except Exception as exc:
                if not allow_file_fallback:
                    raise CredentialStoreUnavailable(
                        f"OS credential store failed: {type(exc).__name__}",
                        hint="re-run and consent to the 0600 file fallback, or use an env var",
                    ) from exc
        elif not allow_file_fallback:
            raise CredentialStoreUnavailable(
                "no OS credential store is available (install the 'keyring' extra)",
                hint="pass consent for the file fallback or export the key as an env var",
            )
        path = self._file_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"api_key": value}), mode=0o600)
        restrict_permissions(path, 0o600)
        return "file"

    def remove(self, name: str) -> bool:
        removed = False
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE, name)
                removed = True
            except Exception:
                pass
        removed = self._remove_file(name) or removed
        return removed

    def rotate(self, name: str, new_value: str, *, allow_file_fallback: bool = False) -> str:
        return self.set(name, new_value, allow_file_fallback=allow_file_fallback)

    def _file_path(self, name: str) -> Path:
        safe = re.sub(r"[^a-z0-9_-]", "_", name.lower())
        return self.home.providers_dir / f"{safe}.cred.json"

    def _remove_file(self, name: str) -> bool:
        path = self._file_path(name)
        if path.is_file():
            path.unlink()
            return True
        return False

    def describe(self, name: str, provider: ProviderConfig | None = None) -> str:
        cred = self.get(name, provider)
        if not cred:
            return "not configured"
        return f"{cred.masked} (from {cred.source})"
