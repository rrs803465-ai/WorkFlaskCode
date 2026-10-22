from __future__ import annotations

import json
import re
from typing import Any

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(key: str) -> str:
    return key if _BARE.match(key) else json.dumps(key)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite floats are not representable in TOML here")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def _value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    return _scalar(value)


def dumps(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _emit_table(data, [], lines)
    return "\n".join(lines).rstrip() + "\n"


def _emit_table(table: dict[str, Any], path: list[str], lines: list[str]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in table.items() if isinstance(v, dict)}
    if path and (scalars or not tables):
        if lines:
            lines.append("")
        lines.append("[" + ".".join(_key(p) for p in path) + "]")
    for k, v in scalars.items():
        lines.append(f"{_key(k)} = {_value(v)}")
    for k, v in tables.items():
        _emit_table(v, [*path, k], lines)
