from __future__ import annotations

import json
import platform
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from workflaskcode.tools.process import run_process
from workflaskcode.utils.fs import atomic_write_text
from workflaskcode.utils.redact import redact


@dataclass
class TestSummary:
    framework: str = "unknown"
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    exit_code: int | None = None
    failures: list[str] = field(default_factory=list)
    parsed: bool = False

    @property
    def headline(self) -> str:
        if not self.parsed:
            return f"exit code {self.exit_code} (test output format not recognised)"
        parts = [f"{self.passed} passed", f"{self.failed} failed"]
        if self.errors:
            parts.append(f"{self.errors} errors")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return f"{self.framework}: " + ", ".join(parts)

    def render(self) -> str:
        lines = [self.headline]
        for failure in self.failures[:20]:
            lines.append(f"  FAILED {failure}")
        if len(self.failures) > 20:
            lines.append(f"  ... and {len(self.failures) - 20} more")
        return "\n".join(lines)


_PYTEST_COUNT = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")
_PYTEST_FAIL = re.compile(r"^(?:FAILED|ERROR)\s+(\S.*?)(?:\s+-\s+.*)?$", re.M)
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
_UNITTEST_FAILED = re.compile(r"^FAILED \((.*)\)", re.M)
_UNITTEST_FAIL_LINE = re.compile(r"^(?:FAIL|ERROR): (\S.*)$", re.M)
_JEST_TESTS = re.compile(r"^Tests:\s+(.*)$", re.M)
_GO_FAIL = re.compile(r"^--- FAIL: (\S+)", re.M)
_CARGO = re.compile(r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored", re.M)


def parse_test_summary(output: str, exit_code: int | None = None) -> TestSummary:
    out = TestSummary(exit_code=exit_code)
    pytest_line = None
    for line in reversed(output.splitlines()):
        if re.search(r"=+ .*(passed|failed|error|skipped|no tests ran).* =+", line) or re.match(
            r"^\d+ (passed|failed)", line.strip()
        ):
            pytest_line = line
            break
    if pytest_line:
        out.framework = "pytest"
        out.parsed = True
        for count, kind in _PYTEST_COUNT.findall(pytest_line):
            n = int(count)
            if kind == "passed":
                out.passed += n
            elif kind == "failed":
                out.failed += n
            elif kind.startswith("error"):
                out.errors += n
            elif kind == "skipped":
                out.skipped += n
        out.failures = [m.group(1).strip() for m in _PYTEST_FAIL.finditer(output)]
        if "no tests ran" in pytest_line:
            out.passed = 0
        return out
    ran = _UNITTEST_RAN.search(output)
    if ran:
        out.framework = "unittest"
        out.parsed = True
        total = int(ran.group(1))
        failed = errors = skipped = 0
        for part in (
            _UNITTEST_FAILED.search(output).group(1).split(",") if _UNITTEST_FAILED.search(output) else []
        ):
            key, _, val = part.strip().partition("=")
            if key == "failures":
                failed = int(val)
            elif key == "errors":
                errors = int(val)
            elif key == "skipped":
                skipped = int(val)
        out.failed, out.errors, out.skipped = failed, errors, skipped
        out.passed = max(total - failed - errors - skipped, 0)
        out.failures = [m.group(1) for m in _UNITTEST_FAIL_LINE.finditer(output)]
        return out
    jest = _JEST_TESTS.search(output)
    if jest:
        out.framework = "jest/vitest"
        out.parsed = True
        for count, kind in re.findall(r"(\d+)\s+(passed|failed|skipped|todo)", jest.group(1)):
            n = int(count)
            if kind == "passed":
                out.passed = n
            elif kind == "failed":
                out.failed = n
            else:
                out.skipped += n
        out.failures = re.findall(r"^\s*(?:FAIL|✕|×)\s+(\S.*)$", output, re.M)[:50]
        return out
    cargo = _CARGO.findall(output)
    if cargo:
        out.framework = "cargo"
        out.parsed = True
        out.passed = sum(int(p) for p, _, _ in cargo)
        out.failed = sum(int(f) for _, f, _ in cargo)
        out.skipped = sum(int(i) for _, _, i in cargo)
        out.failures = re.findall(r"^test (\S+) \.\.\. FAILED", output, re.M)
        return out
    if re.search(r"^(ok|FAIL|---)\s", output, re.M) and ("PASS" in output or "FAIL" in output):
        out.framework = "go"
        out.parsed = True
        out.failures = _GO_FAIL.findall(output)
        out.failed = len(out.failures)
        out.passed = len(re.findall(r"^--- PASS: ", output, re.M)) or len(re.findall(r"^ok\s", output, re.M))
        return out
    return out


@dataclass
class BenchmarkResult:
    command: str
    runs: int
    durations: list[float]
    exit_codes: list[int | None]
    environment: dict[str, Any]
    timestamp: float
    label: str = ""

    @property
    def mean(self) -> float:
        return statistics.fmean(self.durations) if self.durations else 0.0

    @property
    def median(self) -> float:
        return statistics.median(self.durations) if self.durations else 0.0

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.durations) if len(self.durations) > 1 else 0.0

    @property
    def best(self) -> float:
        return min(self.durations) if self.durations else 0.0

    @property
    def worst(self) -> float:
        return max(self.durations) if self.durations else 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(mean=self.mean, median=self.median, stdev=self.stdev, best=self.best, worst=self.worst)
        return data


def environment_info() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": __import__("os").cpu_count(),
    }


def benchmark_command(
    argv: list[str],
    cwd: Path,
    *,
    runs: int = 5,
    warmup: int = 1,
    timeout: float = 300.0,
    cancel=None,
    label: str = "",
) -> BenchmarkResult:
    durations: list[float] = []
    codes: list[int | None] = []
    for _ in range(warmup):
        run_process(argv=argv, cwd=cwd, timeout=timeout, cancel=cancel)
    for _ in range(runs):
        started = time.perf_counter()
        result = run_process(argv=argv, cwd=cwd, timeout=timeout, cancel=cancel)
        durations.append(time.perf_counter() - started)
        codes.append(result.exit_code)
        if result.cancelled or result.error:
            break
    return BenchmarkResult(
        " ".join(argv), len(durations), durations, codes, environment_info(), time.time(), label
    )


def save_benchmark(result: BenchmarkResult, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(result.timestamp))
    path = reports_dir / f"benchmark-{stamp}.json"
    atomic_write_text(path, redact(json.dumps(result.to_dict(), indent=2)))
    return path


def load_benchmarks(reports_dir: Path, command: str | None = None) -> list[dict[str, Any]]:
    items = []
    for path in sorted(reports_dir.glob("benchmark-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if command is None or data.get("command") == command:
            data["_file"] = path.name
            items.append(data)
    return items


def compare_benchmarks(old: dict[str, Any], new: dict[str, Any]) -> str:
    if old.get("command") != new.get("command"):
        return "benchmarks use different commands; the comparison is not meaningful"
    notes = []
    if old.get("environment") != new.get("environment"):
        notes.append("environment differs between the two runs (results may not be comparable)")
    om, nm = old["median"], new["median"]
    delta = (nm - om) / om * 100 if om else 0.0
    noise = max(old.get("stdev", 0), new.get("stdev", 0))
    verdict = "no significant change" if abs(nm - om) <= noise else ("slower" if nm > om else "faster")
    notes.append(f"median {om:.3f}s -> {nm:.3f}s ({delta:+.1f}%), {verdict}")
    return "\n".join(notes)
