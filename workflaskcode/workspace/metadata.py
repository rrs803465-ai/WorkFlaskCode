from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workflaskcode.utils.fs import atomic_write_text

KNOWN_FILES = ("state.json", "tasks.json", "design.json", "handoff.md")
BEGIN = "<!-- workflaskcode:begin -->"
END = "<!-- workflaskcode:end -->"


@dataclass
class MetadataInspection:
    directory: Path
    existed: bool
    known: list[str]
    unknown: list[str]
    corrupt: list[str]


class ProjectMetadata:
    def __init__(self, project_root: Path, directory: Path) -> None:
        self.project_root = project_root
        self.directory = directory

    @classmethod
    def for_project(cls, root: Path, location: str, home_project_dir: Path) -> ProjectMetadata:
        directory = (root / ".workflaskcode") if location == "project" else home_project_dir
        return cls(root, directory)

    def inspect(self) -> MetadataInspection:
        d = self.directory
        if not d.is_dir():
            return MetadataInspection(d, False, [], [], [])
        names = sorted(p.name for p in d.iterdir())
        known = [n for n in names if n in KNOWN_FILES]
        unknown = [n for n in names if n not in KNOWN_FILES]
        corrupt = []
        for n in known:
            if n.endswith(".json"):
                try:
                    json.loads((d / n).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    corrupt.append(n)
        return MetadataInspection(d, True, known, unknown, corrupt)

    def ensure(self) -> MetadataInspection:
        report = self.inspect()
        self.directory.mkdir(parents=True, exist_ok=True)
        for name in report.corrupt:
            src = self.directory / name
            src.replace(src.with_name(f"{name}.corrupt-{int(time.time())}"))
        if self.directory.name == ".workflaskcode":
            gi = self.directory / ".gitignore"
            if not gi.exists():
                atomic_write_text(gi, "state.json\nhandoff.md\nsessions/\nbackups/\n*.db*\n")
        return report

    def read_json(self, name: str, default: Any = None) -> Any:
        try:
            return json.loads((self.directory / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def write_json(self, name: str, data: Any) -> None:
        atomic_write_text(
            self.directory / name, json.dumps(data, indent=2, sort_keys=True, default=str) + "\n"
        )

    def read_text(self, name: str) -> str | None:
        try:
            return (self.directory / name).read_text(encoding="utf-8")
        except OSError:
            return None

    def write_text(self, name: str, text: str) -> None:
        atomic_write_text(self.directory / name, text)

    @property
    def project_md(self) -> Path:
        return self.project_root / "PROJECT.md"

    def read_project_md(self) -> str | None:
        try:
            return self.project_md.read_text(encoding="utf-8")
        except OSError:
            return None

    def update_project_md(self, managed_body: str) -> str:
        block = f"{BEGIN}\n{managed_body.strip()}\n{END}"
        existing = self.read_project_md()
        if existing is None:
            text = f"# {self.project_root.name}\n\n{block}\n"
            action = "created"
        elif BEGIN in existing and END in existing:
            head, _, rest = existing.partition(BEGIN)
            _, _, tail = rest.partition(END)
            text = head + block + tail
            action = "updated managed block"
        else:
            text = existing.rstrip("\n") + "\n\n" + block + "\n"
            action = "appended managed block"
        atomic_write_text(self.project_md, text)
        return action
