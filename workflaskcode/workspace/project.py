from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from workflaskcode.utils.fs import read_text_safely

_NPM_STUB = "no test specified"


@dataclass
class ProjectInfo:
    root: Path
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    test_commands: list[list[str]] = field(default_factory=list)
    format_commands: list[list[str]] = field(default_factory=list)
    type_commands: list[list[str]] = field(default_factory=list)
    dev_commands: list[list[str]] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    is_git: bool = False
    is_empty: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def is_existing_project(self) -> bool:
        return bool(self.markers) or self.is_git

    def summary(self) -> str:
        parts = []
        if self.languages:
            parts.append("languages: " + ", ".join(self.languages))
        if self.frameworks:
            parts.append("frameworks: " + ", ".join(self.frameworks))
        if self.test_commands:
            parts.append("tests: " + " | ".join(" ".join(c) for c in self.test_commands))
        if self.is_git:
            parts.append("git repository")
        return "; ".join(parts) or ("empty directory" if self.is_empty else "no recognised project files")


def _has(root: Path, *names: str) -> bool:
    return any((root / n).exists() for n in names)


def _python_frameworks(root: Path) -> list[str]:
    blob = ""
    for name in ("requirements.txt", "pyproject.toml", "setup.cfg", "Pipfile"):
        text = read_text_safely(root / name, 200_000)
        if text:
            blob += text.lower() + "\n"
    found = []
    for fw in ("fastapi", "flask", "django", "pytest", "sqlalchemy"):
        if fw in blob:
            found.append(fw)
    return found


def detect_project(root: Path) -> ProjectInfo:
    root = root.resolve()
    info = ProjectInfo(root=root)
    try:
        entries = [p for p in root.iterdir() if p.name != ".DS_Store"]
    except OSError:
        entries = []
    info.is_empty = not entries
    info.is_git = (root / ".git").exists()

    if _has(root, "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"):
        info.languages.append("python")
        info.markers.append("python")
        info.frameworks += _python_frameworks(root)
        pyproject = root / "pyproject.toml"
        pytest_configured = False
        ruff_configured = False
        if pyproject.is_file():
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                tool = data.get("tool", {})
                pytest_configured = "pytest" in tool
                ruff_configured = "ruff" in tool
            except (OSError, tomllib.TOMLDecodeError):
                info.notes.append("pyproject.toml could not be parsed")
        if (
            pytest_configured
            or (root / "tests").is_dir()
            or (root / "pytest.ini").exists()
            or "pytest" in info.frameworks
        ):
            info.test_commands.append(["python", "-m", "pytest"])
        if ruff_configured or (root / "ruff.toml").exists():
            info.format_commands.append(["ruff", "format"])
        if (root / "mypy.ini").exists() or (
            pyproject.is_file() and "[tool.mypy]" in (read_text_safely(pyproject) or "")
        ):
            info.type_commands.append(["mypy", "."])
        if _has(root, "uv.lock"):
            info.package_managers.append("uv")
        elif _has(root, "poetry.lock"):
            info.package_managers.append("poetry")
        else:
            info.package_managers.append("pip")

    pkg = root / "package.json"
    if pkg.is_file():
        info.markers.append("node")
        info.languages.append("typescript" if _has(root, "tsconfig.json") else "javascript")
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
            info.notes.append("package.json could not be parsed")
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        for fw in ("react", "next", "vue", "svelte", "express", "vite", "vitest", "jest"):
            if fw in deps:
                info.frameworks.append(fw)
        scripts = data.get("scripts") or {}
        manager = "pnpm" if _has(root, "pnpm-lock.yaml") else "yarn" if _has(root, "yarn.lock") else "npm"
        info.package_managers.append(manager)
        test = str(scripts.get("test", ""))
        if test and _NPM_STUB not in test:
            info.test_commands.append([manager, "test"])
        if "lint" in scripts:
            info.format_commands.append([manager, "run", "lint"])
        if "typecheck" in scripts:
            info.type_commands.append([manager, "run", "typecheck"])
        elif _has(root, "tsconfig.json"):
            info.type_commands.append(["npx", "tsc", "--noEmit"])
        for key in ("dev", "start"):
            if key in scripts:
                info.dev_commands.append([manager, "run", key])
                break

    if _has(root, "go.mod"):
        info.markers.append("go")
        info.languages.append("go")
        info.test_commands.append(["go", "test", "./..."])
        info.format_commands.append(["gofmt", "-l", "."])
        info.type_commands.append(["go", "vet", "./..."])
    if _has(root, "Cargo.toml"):
        info.markers.append("rust")
        info.languages.append("rust")
        info.test_commands.append(["cargo", "test"])
        info.format_commands.append(["cargo", "fmt", "--check"])
        info.type_commands.append(["cargo", "check"])
    if _has(root, "composer.json"):
        info.markers.append("php")
        info.languages.append("php")
    if _has(root, "pom.xml", "build.gradle", "build.gradle.kts"):
        info.markers.append("java")
        info.languages.append("java")
    if _has(root, "Dockerfile", "docker-compose.yml", "compose.yaml"):
        info.markers.append("docker")
    if (root / "index.html").is_file() and not info.markers:
        info.markers.append("static-web")
        info.languages.append("html")

    info.languages = list(dict.fromkeys(info.languages))
    info.frameworks = list(dict.fromkeys(info.frameworks))
    info.package_managers = list(dict.fromkeys(info.package_managers))
    return info
