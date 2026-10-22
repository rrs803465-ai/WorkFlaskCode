from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

SEVERITIES = ("critical", "high", "medium", "low", "info")
CATEGORIES = (
    "security",
    "code-quality",
    "dependencies",
    "performance",
    "architecture",
    "test-coverage",
    "configuration",
    "maintainability",
    "error-handling",
    "accessibility",
    "design",
)
SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITIES)}


@dataclass
class Finding:
    category: str
    severity: str
    rule: str
    file: str | None
    line: int | None
    evidence: str
    explanation: str
    fix: str
    confidence: str
    verified: bool
    tool: str
    id: str = ""

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category {self.category}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity}")
        if not self.id:
            raw = f"{self.category}|{self.rule}|{self.file}|{self.line}|{self.evidence[:120]}"
            self.id = "WFC-" + hashlib.sha1(raw.encode()).hexdigest()[:8]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def location(self) -> str:
        if not self.file:
            return "(project)"
        return f"{self.file}:{self.line}" if self.line else self.file
