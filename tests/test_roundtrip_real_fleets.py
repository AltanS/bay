"""THE round-trip gate (M115 S05) over the real fleets.

For each fleet: import it into tmp_path, compile it, then render BOTH today's
services.yml and the compiled one through build_specs.yml and env.j2 (see
:mod:`bay_cli.roundtrip`) and assert every container spec, env file, raw key
and per-box set is identical. A difference fails with a unified diff per
container.

The fleets and their exception list live in the private workspace file
``../roundtrip-fleets.toml`` (next to this repo), because the fleet and
container names may not appear in this public repo: scripts/leak-scan.sh
refuses them. Without that file, or without a fleet directory, the test skips,
for example in CI. tests/test_import.py runs the same gate on an anonymised
fixture, so the code path has CI coverage.

Each exception is ``(fleet, container, key, reason)``. An exception that no
longer matches a real difference fails the test, so the list can only shrink.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from bay_cli import roundtrip

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT.parent / "roundtrip-fleets.toml"


def _config() -> dict[str, Any]:
    if not CONFIG.is_file():
        return {}
    return tomllib.loads(CONFIG.read_text())


_CFG = _config()
EXCEPTIONS = tuple(
    roundtrip.Exception_(e["fleet"], e["container"], e["key"], e["reason"])
    for e in _CFG.get("exception", [])
)
FLEETS = [
    pytest.param(
        f["name"],
        CONFIG.parent / f["path"],
        marks=pytest.mark.skipif(
            not (CONFIG.parent / f["path"] / "group_vars" / "all").is_dir(),
            reason="fleet checkout not present",
        ),
        id=f"fleet{i}",
    )
    for i, f in enumerate(_CFG.get("fleet", []))
] or [
    pytest.param(None, None, marks=pytest.mark.skip(reason=f"{CONFIG.name} not present"), id="none")
]


def _state(root: Path) -> dict[str, tuple[int, int]]:
    """Size and mtime of the files the importer may read, by stat only."""
    out: dict[str, tuple[int, int]] = {}
    for sub in ("group_vars", "hosts", "files"):
        for p in sorted((root / sub).rglob("*")) if (root / sub).is_dir() else []:
            if p.is_file():
                out[str(p.relative_to(root))] = (p.stat().st_size, p.stat().st_mtime_ns)
    return out


@pytest.mark.parametrize(("name", "path"), FLEETS)
def test_roundtrip_build_specs_identical(name: str, path: Path, tmp_path: Path) -> None:
    before = _state(path)
    gate = roundtrip.run_gate(path, name=name, workdir=tmp_path, exceptions=EXCEPTIONS)
    assert _state(path) == before, "the import wrote into the fleet it reads"

    assert gate.unsupported == [], gate.unsupported
    assert gate.containers > 0
    assert not gate.diffs, (
        "\n".join(gate.summary_lines()) + "\n\n" + "\n".join(d.diff for d in gate.diffs)
    )

    used = {(container, key) for _box, container, key in gate.excepted}
    stale = [e for e in EXCEPTIONS if e.fleet == name and (e.container, e.key) not in used]
    assert stale == [], f"exceptions that no longer match a difference (delete them): {stale}"
