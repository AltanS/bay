"""CLI start-up cost (M111-04 / audit P10).

`bay_cli.cli` imports every command module eagerly, so anything imported at
module scope anywhere under those modules is paid for by *every* invocation,
including `bay --help` and the CLI's own `--version`. The two heavyweights are
`requests` (~60 ms) and `ruamel.yaml` (~12 ms); both are now imported inside
their call sites.

Two assertions, deliberately different in kind:

1. **The module check** — `requests` and `ruamel.yaml` are absent from
   `sys.modules` after importing the CLI. Machine-independent, exact, and it is
   the thing that actually regresses (someone adds a module-level import). It
   is never skipped.
2. **The cost ratio** — cumulative `-X importtime` for `bay_cli.cli`,
   divided by a reference import (`json, pathlib, typer`) measured in the
   same run on the same machine. A fixed millisecond ceiling could not hold:
   the same box measured 72-86 ms idle and 176-204 ms under load, so the
   ceiling failed for reasons that had nothing to do with Bay. A ratio
   cancels the machine and its mood out, because both numbers move together.
   It is still skipped when `BAY_SKIP_PERF_ASSERTS` is set, and it is still
   serial-only.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

#: Modules that must not be pulled in by importing the CLI entry point.
FORBIDDEN_AT_IMPORT = ("requests", "ruamel.yaml")

#: The reference import. Cheap, stdlib plus the one dependency the CLI cannot
#: avoid, so it measures the machine and the interpreter rather than Bay.
REFERENCE_IMPORT = "import json, pathlib, typer"

#: `import bay_cli.cli` may cost at most this many times the reference.
#: Measured 2.8x on the dev box. Re-adding a single module-level
#: `import requests` (the exact regression this file exists to catch) was
#: measured at 4.0x, so the limit sits between the two, verified red.
IMPORT_COST_RATIO_MAX = 3.5


def _python() -> str:
    return sys.executable


class TestNoHeavyImports:
    """The check that must never be skipped."""

    def test_cli_import_does_not_pull_heavy_modules(self) -> None:
        code = (
            "import bay_cli.cli, sys; "
            "print(','.join(m for m in sys.modules if m in "
            f"{FORBIDDEN_AT_IMPORT!r}))"
        )
        proc = subprocess.run(
            [_python(), "-c", code], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, proc.stderr
        leaked = [m for m in proc.stdout.strip().split(",") if m]
        assert not leaked, (
            f"importing bay_cli.cli pulled in {leaked}. Move the import inside "
            "the function that uses it — see tests/test_cli_import_time.py."
        )

    @pytest.mark.parametrize("module", FORBIDDEN_AT_IMPORT)
    def test_module_is_still_importable_on_demand(self, module: str) -> None:
        """Lazy must mean deferred, not removed — the deps are still there."""
        proc = subprocess.run(
            [_python(), "-c", f"import {module}"], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, proc.stderr


def _importtime_lines(code: str) -> list[tuple[int, str]]:
    """Run *code* under `-X importtime` and return (cumulative_us, name) rows."""
    proc = subprocess.run(
        [_python(), "-X", "importtime", "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    rows: list[tuple[int, str]] = []
    for line in proc.stderr.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        try:
            cumulative = int(parts[1].strip())
        except ValueError:
            continue  # the header row
        rows.append((cumulative, parts[2]))
    return rows


def _measure_import_us() -> int:
    """Cumulative cost of `import bay_cli.cli`, in microseconds."""
    cumulative: int | None = None
    for value, name in _importtime_lines("import bay_cli.cli"):
        if name.strip() == "bay_cli.cli":
            cumulative = value
    assert cumulative is not None, "no importtime line for bay_cli.cli"
    return cumulative


def _measure_reference_us() -> int:
    """Cumulative cost of the reference import, in microseconds.

    Sums only the top-level rows. importtime indents nested imports, and a
    nested row is already counted inside its parent's cumulative figure.
    """
    total = 0
    for value, name in _importtime_lines(REFERENCE_IMPORT):
        if not name.startswith("  "):
            total += value
    assert total > 0, "no importtime rows for the reference import"
    return total


@pytest.mark.serial
class TestImportCostRatio:
    @pytest.mark.skipif(
        bool(os.environ.get("BAY_SKIP_PERF_ASSERTS")),
        reason="BAY_SKIP_PERF_ASSERTS set — timing is machine-dependent",
    )
    @pytest.mark.skipif(
        bool(os.environ.get("PYTEST_XDIST_WORKER")),
        reason="timing is meaningless under xdist CPU contention; "
        "`make test-python` re-runs it in a second, serial pass",
    )
    def test_cli_import_cost_ratio(self) -> None:
        # Best of three for each, measured in this same run on this same
        # machine. The floor is the uncontended cost, which is the honest
        # number. A single sample measures the machine's mood instead.
        cumulative = min(_measure_import_us() for _ in range(3))
        reference = min(_measure_reference_us() for _ in range(3))
        ratio = cumulative / reference
        assert ratio < IMPORT_COST_RATIO_MAX, (
            f"import bay_cli.cli cost {ratio:.1f}x the reference import "
            f"({cumulative / 1000:.1f} ms against {reference / 1000:.1f} ms), "
            f"limit is {IMPORT_COST_RATIO_MAX}x. Check for a new module-level "
            "import on the CLI path."
        )
