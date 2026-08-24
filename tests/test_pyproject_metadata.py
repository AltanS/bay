"""Guard the packaging metadata that a public repo is judged on.

`version.yml` is the framework's real version — consumers pin to it via
`.bay-version` and Ansible does minimum-version checks against it.
`pyproject.toml` carries its own version, and `uv.lock` records that version
a third time. All three must agree:

- pyproject drifting from version.yml is the "still says 0.1.0" smell.
- uv.lock drifting from pyproject breaks CI's `uv sync --locked` outright.

`scripts/release.sh` bumps all three together; these tests are what stop it
from regressing (and what catch a hand-edited version).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"
_VERSION_YML = _ROOT / "version.yml"
_UV_LOCK = _ROOT / "uv.lock"
_LICENSE = _ROOT / "LICENSE"


@pytest.fixture(scope="module")
def project() -> dict:
    return tomllib.loads(_PYPROJECT.read_text())["project"]


def _framework_version() -> str:
    return str(yaml.safe_load(_VERSION_YML.read_text())["bay_version"]).lstrip("v")


# ── Version consistency ─────────────────────────────────────────────────

def test_pyproject_version_matches_version_yml(project):
    assert project["version"] == _framework_version(), (
        "pyproject.toml version drifted from version.yml — "
        "scripts/release.sh bumps both; do not hand-edit either"
    )


def test_uv_lock_version_matches_pyproject(project):
    """A stale lock fails CI's `uv sync --locked` before any test runs."""
    match = re.search(
        r'name = "bay"\nversion = "([^"]+)"', _UV_LOCK.read_text()
    )
    assert match, "could not find the bay package entry in uv.lock"
    assert match.group(1) == project["version"], (
        "uv.lock is stale — run `uv lock` after changing the version"
    )


def test_version_matches_version_yml(project):
    """pyproject and version.yml must agree — the CLI and Ansible read different ones.

    This replaced a check that rejected the literal "0.1.0" as the setuptools
    placeholder. That heuristic stopped being usable when the public version
    line deliberately restarted AT 0.1.0. Comparing the two real sources of
    truth is a stronger check anyway: a forgotten bump in either file is
    caught, whatever the number happens to be.
    """
    import yaml

    version_yml = yaml.safe_load((_ROOT / "version.yml").read_text())
    assert version_yml["bay_version"] == project["version"], (
        "version.yml and pyproject.toml disagree — `make release` bumps both, "
        "so this means one was edited by hand"
    )


# ── Public-repo metadata ────────────────────────────────────────────────

def test_license_is_declared_and_matches_the_license_file(project):
    """The declared SPDX id and the LICENSE file must not drift apart.

    Bay was BSL 1.1 until 2026-08-24 and is now MIT. The source-available
    terms were dropped as unenforceable in practice, and MIT also removes the
    one real legal question in the tree: `ansible-core` is GPL-3.0 and is
    imported as a library, not merely shelled out to. GPL is one-way
    compatible with MIT and is not compatible with BSL, which is not an OSI
    licence.
    """
    assert project["license"] == "MIT"
    assert "LICENSE" in project["license-files"]
    assert _LICENSE.exists()
    assert "MIT License" in _LICENSE.read_text()


def test_license_has_no_unfilled_placeholders(project):
    """A template left unedited grants nothing. Catch that, whatever the licence."""
    text = _LICENSE.read_text()
    # The holder is read out of the file, not hard-coded here: repeating the
    # org name in a test is exactly the kind of stray identifier scripts/
    # leak-scan.sh exists to catch, and it has no business failing on its own
    # guard test.
    copyright_line = next(
        (ln for ln in text.splitlines() if ln.startswith("Copyright (c)")), ""
    )
    assert copyright_line, "LICENSE has no copyright line"
    holder = copyright_line.removeprefix("Copyright (c)").strip()
    assert holder and not holder.isdigit(), "LICENSE names no copyright holder"
    for marker in ("[yyyy]", "[name of copyright owner]", "[fullname]", "TODO"):
        assert marker not in text, f"LICENSE still contains the placeholder {marker!r}"


def test_no_stale_business_source_terms_remain(project):
    """Guard the switch away from BSL — a leftover Change Date contradicts MIT."""
    text = _LICENSE.read_text()
    for term in ("Business Source License", "Change Date", "Additional Use Grant"):
        assert term not in text, f"LICENSE still carries the BSL term {term!r}"


def test_readme_and_urls_are_declared(project):
    assert project["readme"] == "README.md"
    assert (_ROOT / "README.md").exists()
    urls = project["urls"]
    assert urls["Homepage"].startswith("https://")
    for name in ("Homepage", "Documentation", "Source", "Issues"):
        assert name in urls


def test_description_and_classifiers_are_present(project):
    assert len(project["description"]) > 30
    classifiers = project["classifiers"]
    assert any(c.startswith("Programming Language :: Python") for c in classifiers)
    # PEP 639: with a `license` expression, License:: classifiers are invalid.
    assert not any(c.startswith("License ::") for c in classifiers)
