from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workflaskcode.audit.checks import ALL_NATIVE, run_native
from workflaskcode.audit.models import CATEGORIES, SEVERITIES, SEVERITY_RANK, Finding

EXTERNAL_TIMEOUT = 120
BANDIT_SEVERITY = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
SEMGREP_SEVERITY = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
RUFF_SECURITY_PREFIXES = ("S",)


@dataclass
class AuditReport:
    root: str
    started: float
    duration: float
    findings: list[Finding]
    tools_run: list[str]
    tools_skipped: dict[str, str]
    categories: list[str]
    files_scanned: int = 0
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in SEVERITIES}
        for f in self.findings:
            out[f.severity] += 1
        return out

    def by_category(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {}
        for f in self.findings:
            out.setdefault(f.category, []).append(f)
        return out

    def filtered(self, min_severity: str = "info", category: str | None = None) -> list[Finding]:
        limit = SEVERITY_RANK[min_severity]
        return [
            f
            for f in self.findings
            if SEVERITY_RANK[f.severity] <= limit and (category is None or f.category == category)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "started": self.started,
            "duration_seconds": round(self.duration, 3),
            "tools_run": self.tools_run,
            "tools_skipped": self.tools_skipped,
            "counts": self.counts(),
            "notes": self.notes,
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        counts = self.counts()
        lines = [
            "# Audit report",
            "",
            f"- Root: `{self.root}`",
            f"- Duration: {self.duration:.2f}s",
            f"- Tools run: {', '.join(self.tools_run) or 'none'}",
            "- Findings: " + ", ".join(f"{counts[s]} {s}" for s in SEVERITIES),
            "",
        ]
        if self.tools_skipped:
            lines.append("Skipped:")
            lines += [f"- {k}: {v}" for k, v in sorted(self.tools_skipped.items())]
            lines.append("")
        for sev in SEVERITIES:
            group = [f for f in self.findings if f.severity == sev]
            if not group:
                continue
            lines.append(f"## {sev.capitalize()} ({len(group)})")
            lines.append("")
            for f in group:
                state = "verified" if f.verified else "unverified"
                lines.append(f"### {f.id} {f.rule} - {f.location}")
                lines.append(
                    f"- Category: {f.category} | Confidence: {f.confidence} | {state} | Source: {f.tool}"
                )
                lines.append(f"- Evidence: `{f.evidence}`")
                lines.append(f"- Why it matters: {f.explanation}")
                lines.append(f"- Fix: {f.fix}")
                lines.append("")
        if not self.findings:
            lines.append("No findings from the checks that ran. This is not proof of absence of problems.")
        return "\n".join(lines)


def _run_external(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=EXTERNAL_TIMEOUT, check=False
    )
    return proc.returncode, proc.stdout, proc.stderr


class AuditEngine:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def available_tools(self) -> dict[str, bool]:
        return {
            name: shutil.which(name) is not None for name in ("ruff", "bandit", "semgrep", "pip-audit", "npm")
        }

    def _ruff(self) -> list[Finding]:
        code, out, err = _run_external(
            ["ruff", "check", "--output-format", "json", "--select", "S,B,E9,F,BLE,ASYNC", "--no-cache", "."],
            self.root,
        )
        if code not in (0, 1):
            raise RuntimeError(err.strip()[:200] or f"exit {code}")
        findings: list[Finding] = []
        for item in json.loads(out or "[]"):
            rule = item.get("code") or "ruff"
            is_sec = rule.startswith(RUFF_SECURITY_PREFIXES)
            filename = item.get("filename", "")
            try:
                filename = Path(filename).resolve().relative_to(self.root).as_posix()
            except ValueError:
                pass
            row = (item.get("location") or {}).get("row")
            findings.append(
                Finding(
                    "security" if is_sec else "code-quality",
                    "medium" if is_sec else ("high" if rule.startswith(("E9", "F8")) else "low"),
                    f"ruff/{rule}",
                    filename,
                    row,
                    (item.get("message") or "")[:200],
                    item.get("message") or "",
                    "See https://docs.astral.sh/ruff/rules/ for " + rule,
                    "high",
                    True,
                    "ruff",
                )
            )
        return findings

    def _bandit(self) -> list[Finding]:
        code, out, err = _run_external(
            ["bandit", "-r", ".", "-f", "json", "-q", "-x", "./node_modules,./.venv,./venv,./tests"],
            self.root,
        )
        if code not in (0, 1) or not out.strip():
            raise RuntimeError(err.strip()[:200] or f"exit {code}")
        findings: list[Finding] = []
        for item in json.loads(out).get("results", []):
            filename = item.get("filename", "")
            try:
                filename = Path(filename).resolve().relative_to(self.root).as_posix()
            except ValueError:
                pass
            findings.append(
                Finding(
                    "security",
                    BANDIT_SEVERITY.get(item.get("issue_severity", "LOW"), "low"),
                    f"bandit/{item.get('test_id')}",
                    filename,
                    item.get("line_number"),
                    (item.get("code") or "").strip().splitlines()[0][:200] if item.get("code") else "",
                    item.get("issue_text", ""),
                    "Review the flagged call; see " + (item.get("more_info") or "bandit docs"),
                    (item.get("issue_confidence") or "low").lower(),
                    True,
                    "bandit",
                )
            )
        return findings

    def _semgrep(self) -> list[Finding]:
        code, out, err = _run_external(
            ["semgrep", "--config", "auto", "--json", "--quiet", "--metrics", "off", "."], self.root
        )
        if code not in (0, 1) or not out.strip():
            raise RuntimeError(err.strip()[:200] or f"exit {code}")
        findings: list[Finding] = []
        for item in json.loads(out).get("results", []):
            extra = item.get("extra", {})
            findings.append(
                Finding(
                    "security",
                    SEMGREP_SEVERITY.get(extra.get("severity", "INFO"), "low"),
                    f"semgrep/{item.get('check_id', '').rsplit('.', 1)[-1]}",
                    item.get("path"),
                    (item.get("start") or {}).get("line"),
                    (extra.get("lines") or "").strip()[:200],
                    extra.get("message", ""),
                    "See the rule documentation referenced by the check id.",
                    "medium",
                    True,
                    "semgrep",
                )
            )
        return findings

    def _pip_audit(self) -> list[Finding]:
        req = self.root / "requirements.txt"
        if not req.exists():
            raise RuntimeError("no requirements.txt")
        code, out, err = _run_external(
            ["pip-audit", "-r", "requirements.txt", "-f", "json", "--progress-spinner", "off"], self.root
        )
        if not out.strip():
            raise RuntimeError(err.strip()[:200] or f"exit {code}")
        findings: list[Finding] = []
        for dep in json.loads(out).get("dependencies", []):
            for vuln in dep.get("vulns", []):
                fixes = ", ".join(vuln.get("fix_versions") or []) or "no fixed version published"
                findings.append(
                    Finding(
                        "dependencies",
                        "high",
                        f"pip-audit/{vuln.get('id')}",
                        "requirements.txt",
                        None,
                        f"{dep.get('name')}=={dep.get('version')}",
                        (vuln.get("description") or "Known vulnerability")[:300],
                        f"Upgrade to: {fixes}",
                        "high",
                        True,
                        "pip-audit",
                    )
                )
        return findings

    def run(
        self, categories: list[str] | None = None, *, use_external: bool = True, has_git: bool = False
    ) -> AuditReport:
        started = time.time()
        wanted = set(categories) if categories else set(CATEGORIES)
        unknown = wanted - set(CATEGORIES)
        if unknown:
            raise ValueError(f"unknown categories: {', '.join(sorted(unknown))}")
        native_names = [name for name, _ in ALL_NATIVE]
        findings: list[Finding] = []
        tools_run: list[str] = []
        skipped: dict[str, str] = {}
        for name, finding in run_native(self.root, native_names, has_git):
            if finding.category in wanted:
                findings.append(finding)
            if f"native:{name}" not in tools_run:
                tools_run.append(f"native:{name}")
        if use_external:
            external = {
                "ruff": self._ruff,
                "bandit": self._bandit,
                "semgrep": self._semgrep,
                "pip-audit": self._pip_audit,
            }
            for tool, fn in external.items():
                if shutil.which(tool) is None:
                    skipped[tool] = "not installed"
                    continue
                try:
                    results = fn()
                except subprocess.TimeoutExpired:
                    skipped[tool] = f"timed out after {EXTERNAL_TIMEOUT}s"
                    continue
                except (RuntimeError, ValueError, OSError) as exc:
                    skipped[tool] = f"failed: {exc}"
                    continue
                tools_run.append(tool)
                findings.extend(f for f in results if f.category in wanted)
        seen: set[tuple[str, str | None, int | None]] = set()
        unique: list[Finding] = []
        for f in sorted(
            findings, key=lambda x: (SEVERITY_RANK[x.severity], x.file or "", x.line or 0, x.rule)
        ):
            key = (f.rule, f.file, f.line)
            if key in seen:
                continue
            seen.add(key)
            unique.append(f)
        notes = ["Native checks are heuristic and marked unverified where they cannot prove exploitability."]
        if not use_external:
            notes.append("External analyzers were disabled for this run.")
        return AuditReport(
            root=str(self.root),
            started=started,
            duration=time.time() - started,
            findings=unique,
            tools_run=tools_run,
            tools_skipped=skipped,
            categories=sorted(wanted),
            notes=notes,
        )
