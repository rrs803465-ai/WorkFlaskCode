from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "builtin_skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    source: str

    def read(self, limit: int = 20_000) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")[:limit]


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta, text[end + 4 :].lstrip("\n")


class SkillIndex:
    def __init__(self, global_dir: Path, project_dir: Path | None = None) -> None:
        self.global_dir = global_dir
        self.project_dir = project_dir

    def install_builtin(self) -> list[str]:
        installed: list[str] = []
        if not BUILTIN_DIR.is_dir():
            return installed
        self.global_dir.mkdir(parents=True, exist_ok=True)
        for src in sorted(BUILTIN_DIR.iterdir()):
            dest = self.global_dir / src.name
            if src.is_dir() and not dest.exists():
                shutil.copytree(src, dest)
                installed.append(src.name)
        return installed

    def _scan(self, base: Path, source: str) -> list[Skill]:
        skills: list[Skill] = []
        if not base.is_dir():
            return skills
        for entry in sorted(base.iterdir()):
            md = entry / "SKILL.md"
            if not entry.is_dir() or not NAME_RE.match(entry.name) or not md.is_file():
                continue
            try:
                head = md.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            meta, body = parse_frontmatter(head)
            description = meta.get("description") or next(
                (ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), ""
            )
            skills.append(Skill(meta.get("name") or entry.name, description[:200], md, source))
        return skills

    def list(self) -> list[Skill]:
        seen: dict[str, Skill] = {}
        for skill in self._scan(self.global_dir, "global"):
            seen[skill.name] = skill
        if self.project_dir:
            for skill in self._scan(self.project_dir, "project"):
                seen[skill.name] = skill
        return sorted(seen.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.list() if s.name == name), None)

    def index_text(self) -> str:
        return "\n".join(f"- {s.name}: {s.description}" for s in self.list())
