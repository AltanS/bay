"""The version-drift guard must name the files a consumer actually has.

Both `BayError` messages named the pre-1.0 dot-directory and pin-file names,
so an operator whose pin was stale went looking for files that no Bay layout
contains.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bay_cli import guards, paths
from bay_cli.errors import BayError

GUARDS_SOURCE = Path(guards.__file__).read_text()

OLD_PREFIX = "." + "argo"  # legacy-argo: asserting the old name is absent


def test_no_pre_1_0_path_names_survive_in_the_source() -> None:
    assert OLD_PREFIX not in GUARDS_SOURCE


def test_no_stale_legacy_tags_left_behind() -> None:
    """A `legacy-argo` tag suppresses the rename sweep on its line forever."""
    assert "legacy-argo" not in GUARDS_SOURCE


def test_missing_installed_version_names_the_bay_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "is_dev_linked", lambda root: False)
    monkeypatch.setattr(paths, "read_pinned_version", lambda root: "v1.2.3")
    monkeypatch.setattr(paths, "read_installed_version", lambda bay_dir: None)

    with pytest.raises(BayError) as excinfo:
        guards.check_bay_version(tmp_path / ".bay", tmp_path)

    message = str(excinfo.value)
    assert ".bay/version.yml" in message
    assert OLD_PREFIX not in message


def test_mismatch_names_the_bay_pin_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "is_dev_linked", lambda root: False)
    monkeypatch.setattr(paths, "read_pinned_version", lambda root: "v1.2.3")
    monkeypatch.setattr(paths, "read_installed_version", lambda bay_dir: "v1.0.0")

    with pytest.raises(BayError) as excinfo:
        guards.check_bay_version(tmp_path / ".bay", tmp_path)

    message = str(excinfo.value)
    assert ".bay-version: v1.2.3" in message
    assert OLD_PREFIX not in message
