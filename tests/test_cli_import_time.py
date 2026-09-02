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
2. **The wall-clock ceiling** — cumulative `-X importtime` for `bay_cli.cli`
   under 100 ms. Machine-dependent, so it is a generous ceiling (measured
   ~80 ms on the dev box, down from ~200 ms) and is skipped when
   `BAY_SKIP_PERF_ASSERTS` is set, for slow or contended CI runners.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

#: Modules that must not be pulled in by importing the CLI entry point.
FORBIDDEN_AT_IMPORT = ("requests", "ruamel.yaml")

#: Microseconds. Baseline before the lazy-import work was ~200_000 here and
#: ~132_000 on the machine the spec was written on.
IMPORT_TIME_CEILING_US = 100_000


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


def _measure_import_us() -> int:
    proc = subprocess.run(
        [_python(), "-X", "importtime", "-c", "import bay_cli.cli"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    cumulative: int | None = None
    for line in proc.stderr.splitlines():
        parts = line.split("|")
        if len(parts) == 3 and parts[2].strip() == "bay_cli.cli":
            cumulative = int(parts[1].strip())
    assert cumulative is not None, "no importtime line for bay_cli.cli"
    return cumulative


@pytest.mark.serial
class TestImportTimeCeiling:
    @pytest.mark.skipif(
        bool(os.environ.get("BAY_SKIP_PERF_ASSERTS")),
        reason="BAY_SKIP_PERF_ASSERTS set — wall-clock ceiling is machine-dependent",
    )
    @pytest.mark.skipif(
        bool(os.environ.get("PYTEST_XDIST_WORKER")),
        reason="wall-clock measurement is meaningless under xdist CPU contention; "
        "`make test-python` re-runs it in a second, serial pass",
    )
    def test_cli_import_time_under_ceiling(self) -> None:
        # Best of three: the floor is the uncontended cost, which is what the
        # ceiling is about. A single sample measures the machine's mood.
        cumulative = min(_measure_import_us() for _ in range(3))
        assert cumulative < IMPORT_TIME_CEILING_US, (
            f"import bay_cli.cli took {cumulative / 1000:.1f} ms, ceiling is "
            f"{IMPORT_TIME_CEILING_US / 1000:.0f} ms. Check for a new "
            "module-level import on the CLI path."
        )
