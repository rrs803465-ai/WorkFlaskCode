from __future__ import annotations

from enum import StrEnum


class WorkFlaskError(Exception):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message if not self.hint else f"{self.message} ({self.hint})"


class ConfigError(WorkFlaskError):
    pass


class CredentialError(WorkFlaskError):
    pass


class CredentialStoreUnavailable(CredentialError):
    pass


class PathSecurityError(WorkFlaskError):
    pass


class PermissionDenied(WorkFlaskError):
    pass


class StateError(WorkFlaskError):
    pass


class ToolError(WorkFlaskError):
    pass


class Cancelled(WorkFlaskError):
    pass


class ErrorKind(StrEnum):
    NETWORK = "network"
    TIMEOUT = "timeout"
    OVERLOADED = "overloaded"
    RATE_LIMIT_MODEL = "rate_limit_model"
    QUOTA_ACCOUNT = "quota_account"
    INVALID_CREDENTIALS = "invalid_credentials"
    BILLING = "billing"
    UNSUPPORTED_TOOLS = "unsupported_tools"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_OVERFLOW = "context_overflow"
    SAFETY_REFUSAL = "safety_refusal"
    MODEL_UNAVAILABLE = "model_unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    UNKNOWN = "unknown"


TRANSIENT_KINDS = frozenset(
    {ErrorKind.NETWORK, ErrorKind.TIMEOUT, ErrorKind.OVERLOADED, ErrorKind.RATE_LIMIT_MODEL}
)
MODEL_SPECIFIC_KINDS = frozenset(
    {
        ErrorKind.RATE_LIMIT_MODEL,
        ErrorKind.OVERLOADED,
        ErrorKind.UNSUPPORTED_TOOLS,
        ErrorKind.MODEL_UNAVAILABLE,
        ErrorKind.CONTEXT_OVERFLOW,
        ErrorKind.SAFETY_REFUSAL,
        ErrorKind.MALFORMED_RESPONSE,
    }
)
ACCOUNT_WIDE_KINDS = frozenset({ErrorKind.QUOTA_ACCOUNT, ErrorKind.INVALID_CREDENTIALS, ErrorKind.BILLING})


class ProviderError(WorkFlaskError):
    def __init__(
        self,
        message: str,
        *,
        kind: ErrorKind = ErrorKind.UNKNOWN,
        status: int | None = None,
        retry_after: float | None = None,
        provider: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.provider = provider

    @property
    def transient(self) -> bool:
        return self.kind in TRANSIENT_KINDS

    @property
    def model_specific(self) -> bool:
        return self.kind in MODEL_SPECIFIC_KINDS

    @property
    def account_wide(self) -> bool:
        return self.kind in ACCOUNT_WIDE_KINDS


class ModelListingUnsupported(ProviderError):
    pass


class AllModelsExhausted(WorkFlaskError):
    def __init__(self, message: str, failures: list[str] | None = None) -> None:
        super().__init__(message)
        self.failures = failures or []
