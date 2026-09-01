"""Documentation claims that the code can contradict.

Each of these was shipped wrong: an access mode that no schema accepts, a
command described as doing another command's job, a link to a document that
this repo deliberately does not contain, and a release instruction that drifts
`version.yml` from the git tags.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FEATURES = _REPO_ROOT / "docs" / "features.md"
_README = _REPO_ROOT / "README.md"
_SCHEMA = _REPO_ROOT / "src" / "bay_cli" / "schemas" / "services.schema.json"


def _access_modes() -> list[str]:
    schema = json.loads(_SCHEMA.read_text())
    return schema["$defs"]["service"]["properties"]["access"]["enum"]


def test_features_documents_exactly_the_schema_access_modes() -> None:
    """`admin` was documented for years; the schema has never accepted it."""
    modes = _access_modes()
    assert set(modes) == {"public", "vpn"}, (
        "the schema changed — update docs/features.md to match"
    )
    text = _FEATURES.read_text()
    assert "`admin`" not in text, "docs/features.md documents a non-existent access mode"
    for mode in modes:
        assert f"`{mode}`" in text


def test_features_does_not_conflate_validate_with_doctor() -> None:
    """`validate` reads config files; `doctor` probes the environment."""
    text = _FEATURES.read_text()
    validate_line = next(
        line for line in text.splitlines() if line.startswith("- **`bay validate`**")
    )
    for probe in ("SSH connectivity", "DNS resolution", "vault password"):
        assert probe not in validate_line, (
            f"docs/features.md credits `bay validate` with doctor's probe: {probe}"
        )
    doctor_line = next(
        (line for line in text.splitlines() if line.startswith("- **`bay doctor`**")),
        "",
    )
    assert "DNS resolution" in doctor_line and "SSH" in doctor_line, (
        "docs/features.md must name `bay doctor` for the environment probes"
    )


def test_readme_links_only_to_docs_that_exist() -> None:
    """production-access.md lives in the private workspace repo, by design.

    It holds real server IPs and tailnet topology, so it can never ship here —
    the link had to be dropped, not satisfied.
    """
    text = _README.read_text()
    assert "production-access" not in text
    assert not (_REPO_ROOT / "docs" / "production-access.md").exists()


def test_readme_release_workflow_uses_make_release() -> None:
    """A hand tag leaves version.yml behind, breaking minimum-version checks."""
    text = _README.read_text()
    assert "git tag v" not in text, "README instructs a hand-written release tag"
    assert "make release VERSION=" in text
    assert "CONTRIBUTING.md" in text


def test_contributing_still_documents_the_release_command() -> None:
    text = (_REPO_ROOT / "CONTRIBUTING.md").read_text()
    assert "make release VERSION=" in text
    assert "CHANGELOG.md" in text
