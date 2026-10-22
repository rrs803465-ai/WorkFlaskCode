from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from workflaskcode.errors import WorkFlaskError
from workflaskcode.templates import stacks
from workflaskcode.utils.fs import atomic_write_text

SLUG = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,59}$")


@dataclass
class TemplateSpec:
    name: str
    description: str
    build: Callable[[str, dict[str, str]], dict[str, str]]
    test_command: list[str] | None = None
    run_hint: str = ""
    unverified: list[str] = field(default_factory=list)
    keywords: tuple[str, ...] = ()


@dataclass
class GenerateResult:
    written: list[str]
    conflicts: list[str]
    spec: TemplateSpec


_REGISTRY: dict[str, TemplateSpec] = {}


def register_template(spec: TemplateSpec) -> None:
    _REGISTRY[spec.name] = spec


def list_templates() -> list[TemplateSpec]:
    return sorted(_REGISTRY.values(), key=lambda s: s.name)


def get_template(name: str) -> TemplateSpec:
    spec = _REGISTRY.get(name)
    if spec is None:
        raise WorkFlaskError(f"unknown template '{name}'", hint="available: " + ", ".join(sorted(_REGISTRY)))
    return spec


def generate(
    spec: TemplateSpec,
    dest: Path,
    project_name: str,
    options: dict[str, str] | None = None,
    *,
    addons: list[str] | None = None,
) -> GenerateResult:
    if not SLUG.match(project_name):
        raise WorkFlaskError(
            "project name must start with a letter and use only letters, digits, '-' and '_'"
        )
    options = dict(options or {})
    files = dict(spec.build(project_name, options))
    for addon in addons or []:
        files.update(render_addon(addon, project_name, spec.name))
    dest = dest.resolve()
    conflicts = sorted(p for p in files if (dest / p).exists())
    if conflicts:
        return GenerateResult([], conflicts, spec)
    written: list[str] = []
    for relative, content in sorted(files.items()):
        target = (dest / relative).resolve()
        if dest not in target.parents:
            raise WorkFlaskError(f"template tried to write outside the project: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, content)
        if relative.endswith(".sh"):
            target.chmod(0o755)
        written.append(relative)
    return GenerateResult(written, [], spec)


ADDONS = {
    "docker": "Dockerfile + .dockerignore (non-root user)",
    "compose": "docker-compose.yml",
    "ci": "GitHub Actions workflow running the tests",
    "systemd": "systemd service unit template",
    "env": ".env.example with documented variables",
}


def render_addon(addon: str, project_name: str, template: str) -> dict[str, str]:
    if addon not in ADDONS:
        raise WorkFlaskError(f"unknown addon '{addon}'", hint="available: " + ", ".join(sorted(ADDONS)))
    return stacks.addon_files(addon, project_name, template)


for _spec in stacks.all_specs(TemplateSpec):
    register_template(_spec)
