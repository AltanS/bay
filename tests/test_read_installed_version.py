"""Tests for paths.read_installed_version()."""

from __future__ import annotations

from pathlib import Path

import pytest

from bay_cli.errors import BayError
from bay_cli.paths import read_installed_version


# ── Helpers ──────────────────────────────────────────────────────────────


def _write_version_yml(bay_dir: Path, version: str) -> None:
    """Write a version.yml file into bay_dir."""
    (bay_dir / "version.yml").write_text(
        f'---\nbay_version: "{version}"\n'
    )


# ── read_installed_version tests ─────────────────────────────────────────


class TestReadInstalledVersion:
    """Tests for paths.read_installed_version()."""

    def test_valid_version_yml(self, tmp_path: Path) -> None:
        """Valid version.yml returns the bay_version string."""
        _write_version_yml(tmp_path, "v0.39.3")
        assert read_installed_version(tmp_path) == "v0.39.3"

    def test_missing_version_yml(self, tmp_path: Path) -> None:
        """Missing version.yml returns None."""
        assert read_installed_version(tmp_path) is None

    def test_missing_bay_version_key(self, tmp_path: Path) -> None:
        """version.yml present but without bay_version key returns None."""
        (tmp_path / "version.yml").write_text("---\nsome_other_key: foo\n")
        assert read_installed_version(tmp_path) is None

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        """Malformed YAML raises BayError."""
        (tmp_path / "version.yml").write_text("---\n: :\n  bad: [yaml\n")
        with pytest.raises(BayError, match="Failed to parse"):
            read_installed_version(tmp_path)

    def test_version_as_number(self, tmp_path: Path) -> None:
        """Numeric version values are coerced to string."""
        (tmp_path / "version.yml").write_text("---\nbay_version: 1.2\n")
        result = read_installed_version(tmp_path)
        assert isinstance(result, str)
