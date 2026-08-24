"""The changelog must document the version being shipped.

Consumers pin a framework version and move with `bin/bay update`. A release
that lands without a changelog entry is invisible to them — they get new
behaviour with no way to find out what changed short of reading git log.

`make release` bumps `version.yml`, so the entry has to be written *before*
releasing:

    1. add a `## [X.Y.Z] — <date>` section to CHANGELOG.md
    2. commit
    3. make release VERSION=X.Y.Z
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_VERSION_FILE = _REPO_ROOT / "version.yml"

# "## [0.111.0] — 2026-07-28" and range headings like "## [0.107.1] – [0.107.9]"
_HEADING = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def _declared_version() -> str:
    return str(yaml.safe_load(_VERSION_FILE.read_text())["bay_version"])


def _documented_versions() -> list[str]:
    return _HEADING.findall(_CHANGELOG.read_text())


def test_changelog_exists():
    assert _CHANGELOG.is_file(), (
        "CHANGELOG.md is missing — it is how consumers find out what a "
        "framework bump changed."
    )


def test_current_version_is_documented():
    version = _declared_version()
    documented = _documented_versions()
    assert version in documented, (
        f"version.yml declares {version} but CHANGELOG.md documents "
        f"{documented[:5]}. Add a `## [{version}] — <date>` section before "
        f"releasing — `make release` bumps version.yml, so the entry must "
        f"already be committed."
    )


def test_newest_entry_is_not_behind_the_current_version():
    """The top entry must be this release, or the one being prepared.

    Deliberately not `== version.yml`: the entry is written and committed
    *before* `make release` bumps version.yml, so between those two steps the
    changelog legitimately leads by one version. `scripts/release.sh` enforces
    the exact match at the moment it matters — it refuses to tag a version
    with no entry.
    """
    def key(v: str) -> tuple[int, ...]:
        return tuple(int(part) for part in v.split("."))

    documented = _documented_versions()
    assert documented, "CHANGELOG.md has no version headings"
    assert key(documented[0]) >= key(_declared_version()), (
        f"the newest CHANGELOG.md entry is {documented[0]}, which is older "
        f"than the {_declared_version()} in version.yml. Entries are "
        f"newest-first, so a release went out undocumented."
    )


def test_versions_are_ordered_newest_first():
    def key(v: str) -> tuple[int, ...]:
        return tuple(int(part) for part in v.split("."))

    documented = _documented_versions()
    # Range headings ("[0.107.1] – [0.107.9]") contribute their low bound, so
    # compare only that the sequence is non-increasing.
    for older, newer in zip(documented[1:], documented):
        assert key(newer) >= key(older), (
            f"CHANGELOG.md is out of order: {newer} appears above {older}"
        )
