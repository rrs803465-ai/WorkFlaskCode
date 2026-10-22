from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from workflaskcode.errors import ErrorKind, ProviderError

_QUOTA = (
    "insufficient_quota",
    "exceeded your current quota",
    "quota exceeded",
    "free-models-per-day",
    "daily limit",
    "per day",
    "monthly limit",
    "usage limit",
)
_BILLING = (
    "credit balance",
    "insufficient credits",
    "insufficient funds",
    "payment required",
    "billing",
    "add more credits",
    "requires more credits",
)
_CONTEXT = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "prompt is too long",
    "too many tokens",
    "request too large",
    "input is too long",
    "context window",
)
_TOOLS = (
    "does not support tool",
    "doesn't support tool",
    "tool use is not supported",
    "tools are not supported",
    "tool calling is not supported",
    "no endpoints found that support tool",
    "function calling is not supported",
    "does not support function",
    "unsupported parameter: tools",
    "tool_choice",
)
_MODEL = (
    "model not found",
    "does not exist",
    "unknown model",
    "no such model",
    "model_not_found",
    "is not a valid model",
    "no endpoints found",
    "model is not available",
    "not available",
)
_SAFETY = ("content_policy", "content policy", "safety", "refus", "flagged", "moderation")


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


def extract_message(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("error") or ""
            meta = err.get("metadata")
            if isinstance(meta, dict) and meta.get("raw"):
                msg = f"{msg} | upstream: {str(meta['raw'])[:300]}"
            return str(msg)
        if isinstance(err, str):
            return err
        for key in ("message", "detail", "error_description"):
            if isinstance(body.get(key), str):
                return body[key]
    if isinstance(body, str):
        return body[:300]
    return ""


def _has(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def classify_http_error(
    status: int,
    headers: dict[str, str] | None,
    body: bytes | str | None,
    *,
    provider: str | None = None,
) -> ProviderError:
    raw = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else (body or "")
    parsed: Any = None
    try:
        parsed = json.loads(raw) if raw.strip().startswith(("{", "[")) else None
    except ValueError:
        parsed = None
    message = extract_message(parsed) or re.sub(r"\s+", " ", raw)[:300] or f"HTTP {status}"
    haystack = (message + " " + raw[:2000]).lower()
    err_type = ""
    err_code = ""
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            err_type = str(err.get("type") or "").lower()
            err_code = str(err.get("code") or "").lower()
        elif isinstance(parsed.get("type"), str):
            err_type = parsed["type"].lower()
    haystack += f" {err_type} {err_code}"
    retry_after = parse_retry_after((headers or {}).get("retry-after"))

    def make(kind: ErrorKind, hint: str | None = None) -> ProviderError:
        return ProviderError(
            f"{message} [HTTP {status}]",
            kind=kind,
            status=status,
            retry_after=retry_after,
            provider=provider,
            hint=hint,
        )

    if status == 401 or err_type == "authentication_error":
        return make(ErrorKind.INVALID_CREDENTIALS, "check the API key; rotate it with @providers")
    if status == 402 or _has(haystack, _BILLING) or err_type == "billing_error":
        return make(ErrorKind.BILLING, "add credits or choose a free model")
    if status == 403 or err_type == "permission_error":
        return make(ErrorKind.INVALID_CREDENTIALS, "key lacks permission for this model/endpoint")
    if status in (408, 504) or err_type == "timeout_error":
        return make(ErrorKind.TIMEOUT)
    if status == 413 or _has(haystack, _CONTEXT):
        return make(ErrorKind.CONTEXT_OVERFLOW, "run @compact or pick a larger-context model")
    if status == 429 or err_type == "rate_limit_error":
        if _has(haystack, _QUOTA):
            return make(ErrorKind.QUOTA_ACCOUNT, "the account quota is exhausted; wait or add credit")
        return make(ErrorKind.RATE_LIMIT_MODEL)
    if status == 404:
        if "model" in haystack:
            return make(ErrorKind.MODEL_UNAVAILABLE)
        return make(ErrorKind.INVALID_REQUEST, "404 - check the provider base_url")
    if status in (500, 502, 503, 529) or err_type in ("overloaded_error", "api_error"):
        return make(ErrorKind.OVERLOADED)
    if status in (400, 422):
        if _has(haystack, _TOOLS):
            return make(ErrorKind.UNSUPPORTED_TOOLS)
        if _has(haystack, _MODEL):
            return make(ErrorKind.MODEL_UNAVAILABLE)
        if _has(haystack, _SAFETY):
            return make(ErrorKind.SAFETY_REFUSAL)
        return make(ErrorKind.INVALID_REQUEST)
    if status >= 500:
        return make(ErrorKind.OVERLOADED)
    return make(ErrorKind.UNKNOWN)


def classify_body_error(body: Any, *, provider: str | None = None) -> ProviderError | None:
    if not isinstance(body, dict) or "error" not in body:
        return None
    err = body["error"]
    code = None
    if isinstance(err, dict):
        raw_code = err.get("code")
        if isinstance(raw_code, int):
            code = raw_code
        elif isinstance(raw_code, str) and raw_code.isdigit():
            code = int(raw_code)
    return classify_http_error(code or 500, {}, json.dumps(body), provider=provider)


def transport_error(exc: Exception, provider: str | None = None) -> ProviderError:
    timeout = bool(getattr(exc, "timeout", False))
    return ProviderError(
        f"{'timed out' if timeout else 'network error'}: {exc}",
        kind=ErrorKind.TIMEOUT if timeout else ErrorKind.NETWORK,
        provider=provider,
        hint="check connectivity/proxy settings and the provider base URL",
    )
