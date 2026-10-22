from __future__ import annotations

import logging
import re
import threading

REDACTED = "[REDACTED]"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-or-v1-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
_BEARER = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/\-=]{8,})")
_KEYED = re.compile(
    r"""(?ix)
    (["']?(?:api[_-]?key|x-api-key|authorization|secret|token|password|passwd)["']?
     \s*[:=]\s*["']?)
    ([^\s"',}]{6,})
    """
)

_lock = threading.Lock()
_known_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        with _lock:
            _known_secrets.add(value)


def clear_registered_secrets() -> None:
    with _lock:
        _known_secrets.clear()


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _KEYED.sub(lambda m: m.group(1) + REDACTED, text)
    return text


def redact_obj(obj: object) -> object:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and re.search(r"(?i)key|secret|token|password|authorization", k):
                out[k] = REDACTED if v else v
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def mask_secret(value: str | None) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}...{value[-4:]}" if value.startswith("sk-") else f"...{value[-4:]}"


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = redact(msg)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    target = logger or logging.getLogger()
    flt = RedactingFilter()
    for handler in target.handlers:
        handler.addFilter(flt)
    target.addFilter(flt)
