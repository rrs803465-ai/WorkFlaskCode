from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_MODEL_ID_BAD = re.compile(r"[\s\x00-\x1f\x7f]")
_SECRET_HEADER = re.compile(r"(?i)authorization|api[-_]?key|secret|token|password")


class ProviderKind(StrEnum):
    OPENROUTER = "openrouter"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    CUSTOM = "custom"


class PermissionMode(StrEnum):
    READ_ONLY = "read_only"
    APPROVE_ALL = "approve_all"
    APPROVE_RISKY = "approve_risky"
    RESTRICTED_AUTONOMOUS = "restricted_autonomous"


class InteractionMode(StrEnum):
    CHAT = "chat"
    CODING = "coding"


def validate_model_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("model id must not be empty")
    if len(value) > 200:
        raise ValueError("model id is longer than 200 characters")
    if _MODEL_ID_BAD.search(value):
        raise ValueError("model id must not contain whitespace or control characters")
    return value


class ModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model_id: str

    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError("provider name must be a lowercase slug (a-z, 0-9, '-', '_')")
        return v

    @field_validator("model_id")
    @classmethod
    def _model(cls, v: str) -> str:
        return validate_model_id(v)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model_id}"

    @classmethod
    def parse(cls, text: str, default_provider: str | None = None) -> ModelRef:
        text = text.strip()
        if ":" in text:
            head, tail = text.split(":", 1)
            if _SLUG.match(head):
                return cls(provider=head, model_id=tail)
        if default_provider:
            return cls(provider=default_provider, model_id=text)
        raise ValueError(f"'{text}' is not in 'provider:model-id' form")


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: ProviderKind
    base_url: str | None = None
    env_var: str | None = Field(
        default=None, description="Name of an environment variable holding the API key."
    )
    allow_paid: bool = False
    extra_headers: dict[str, str] = Field(default_factory=dict)
    request_options: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    supports_model_listing: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError("provider name must be a lowercase slug")
        return v

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not embed credentials")
        return v.rstrip("/")

    @field_validator("extra_headers")
    @classmethod
    def _headers(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if _SECRET_HEADER.search(key):
                raise ValueError(
                    f"header '{key}' looks like a credential; credentials must not be stored "
                    "in config.toml (use the credential store or an env var)"
                )
        return v

    @field_validator("env_var")
    @classmethod
    def _env(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", v):
            raise ValueError("env_var must be a valid environment variable name")
        return v

    @model_validator(mode="after")
    def _custom_needs_url(self) -> ProviderConfig:
        if self.kind is ProviderKind.CUSTOM and not self.base_url:
            raise ValueError("custom providers require base_url")
        return self


class PermissionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PermissionMode = PermissionMode.APPROVE_RISKY
    allow: list[str] = Field(default_factory=list, description="Extra allowed command globs.")
    deny: list[str] = Field(default_factory=list, description="Extra denied command globs.")
    network: Literal["ask", "deny", "allow"] = "ask"
    allow_shell_strings: bool = True
    extra_roots: list[str] = Field(default_factory=list)
    command_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_output_bytes: int = Field(default=60_000, ge=1_000, le=5_000_000)


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=4, ge=1, le=20)
    base_delay_seconds: float = Field(default=1.0, ge=0, le=60)
    max_delay_seconds: float = Field(default=30.0, ge=0, le=600)
    jitter: float = Field(default=0.25, ge=0, le=1)


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tool_calls_per_task: int = Field(default=80, ge=1, le=2000)
    max_requests_per_task: int = Field(default=60, ge=1, le=2000)
    max_task_seconds: float = Field(default=1800.0, gt=0)
    max_cost_per_task: float | None = Field(default=None, ge=0)
    max_parallel_requests: int = Field(default=2, ge=1, le=32)
    max_side_chats: int = Field(default=3, ge=1, le=16)
    max_test_repair_attempts: int = Field(default=3, ge=0, le=20)
    model_budgets: dict[str, float] = Field(default_factory=dict)


class BrowserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=5173, ge=1, le=65535)
    startup_timeout_seconds: float = Field(default=30.0, gt=0)


class GitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    auto_checkpoint: bool = True


class UISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: Literal["auto", "dark", "light", "plain"] = "auto"
    color: bool = True
    plain: bool = False
    verbose: bool = False
    stream: bool = True


class PrivacySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    side_chat_may_read_files: bool = True
    send_project_context: bool = True


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    default_provider: str | None = None
    coding_model: ModelRef | None = None
    chat_model: ModelRef | None = None
    fallback_models: list[ModelRef] = Field(default_factory=list)
    default_project_root: str | None = None
    mode: InteractionMode = InteractionMode.CODING
    metadata_location: Literal["project", "home"] = "home"
    context_budget_tokens: int = Field(default=60_000, ge=2_000, le=2_000_000)
    logging_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    telemetry: Literal["off"] = "off"
    plugins_enabled: list[str] = Field(default_factory=list)
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    limits: Limits = Field(default_factory=Limits)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    git: GitSettings = Field(default_factory=GitSettings)
    ui: UISettings = Field(default_factory=UISettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        for key, prov in self.providers.items():
            if key != prov.name:
                raise ValueError(f"provider table key '{key}' does not match name '{prov.name}'")
        for label, ref in (("coding_model", self.coding_model), ("chat_model", self.chat_model)):
            if ref and ref.provider not in self.providers:
                raise ValueError(f"{label} references unknown provider '{ref.provider}'")
        for ref in self.fallback_models:
            if ref.provider not in self.providers:
                raise ValueError(f"fallback model references unknown provider '{ref.provider}'")
        if self.default_provider and self.default_provider not in self.providers:
            raise ValueError(f"default_provider '{self.default_provider}' is not configured")
        return self

    def effective_chat_model(self) -> ModelRef | None:
        return self.chat_model or self.coding_model

    def configured(self) -> bool:
        return self.coding_model is not None

    def to_toml_dict(self) -> dict[str, Any]:
        raw = self.model_dump(mode="json")
        raw["coding_model"] = str(self.coding_model) if self.coding_model else None
        raw["chat_model"] = str(self.chat_model) if self.chat_model else None
        raw["fallback_models"] = [str(m) for m in self.fallback_models]
        return _drop_none(raw)


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value if v is not None]
    return value
