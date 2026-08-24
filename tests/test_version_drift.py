"""Tests for M48 version drift detection (S1 + S2 + S3).

Covers:
- paths.read_installed_version()
- validate._validate_version_drift()
- guards.check_bay_version()
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bay_cli.commands.validate import ValidationResult, _validate_version_drift
from bay_cli.errors import BayError
from bay_cli.guards import check_bay_version
from bay_cli.paths import read_installed_version


# ── Helpers ──────────────────────────────────────────────────────────────


class _JsonMode:
    """Context manager to suppress Rich console output during tests."""

    def __enter__(self):
        from bay_cli.console.output import set_json_mode

        set_json_mode(True)
        return self

    def __exit__(self, *args):
        from bay_cli.console.output import set_json_mode

        set_json_mode(False)


def _write_version_yml(bay_dir: Path, version: str) -> None:
    """Write a version.yml file into bay_dir."""
    (bay_dir / "version.yml").write_text(
        f'---\nbay_version: "{version}"\n'
    )


def _write_pinned_version(root: Path, version: str) -> None:
    """Write .bay-version file."""
    (root / ".bay-version").write_text(version)


def _set_dev_link(root: Path) -> None:
    """Create the .bay-dev sentinel."""
    (root / ".bay-dev").touch()


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


# ── _validate_version_drift tests ────────────────────────────────────────


class TestValidateVersionDrift:
    """Tests for validate._validate_version_drift()."""

    def test_versions_match(self, tmp_path: Path) -> None:
        """Matching versions produce exactly one ok(), no failures."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.39.3")
        _write_version_yml(bay_dir, "v0.39.3")

        with _JsonMode():
            result = ValidationResult()
            _validate_version_drift(tmp_path, bay_dir, result)

        assert len(result.passed) == 1
        assert "v0.39.3" in result.passed[0]
        assert len(result.failed) == 0

    def test_versions_match_v_prefix_mismatch(self, tmp_path: Path) -> None:
        """Pinned 'v0.41.4' matches installed '0.41.4' (v-prefix stripped)."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.41.4")
        _write_version_yml(bay_dir, "0.41.4")

        with _JsonMode():
            result = ValidationResult()
            _validate_version_drift(tmp_path, bay_dir, result)

        assert len(result.passed) == 1
        assert len(result.failed) == 0

    def test_versions_mismatch(self, tmp_path: Path) -> None:
        """Mismatched versions produce exactly one fail() with both versions."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.39.3")
        _write_version_yml(bay_dir, "v0.38.0")

        with _JsonMode():
            result = ValidationResult()
            _validate_version_drift(tmp_path, bay_dir, result)

        assert len(result.failed) == 1
        assert "v0.39.3" in result.failed[0]
        assert "v0.38.0" in result.failed[0]
        assert "bin/bay install" in result.failed[0]
        assert len(result.passed) == 0

    def test_missing_bay_version_file(self, tmp_path: Path) -> None:
        """Missing .bay-version produces warn(), no failure."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_version_yml(bay_dir, "v0.39.3")
        # No .bay-version file

        with _JsonMode():
            result = ValidationResult()
            _validate_version_drift(tmp_path, bay_dir, result)

        assert len(result.warnings) == 1
        assert ".argo-version" in result.warnings[0]  # legacy-argo: message text not yet renamed in validate.py
        assert len(result.failed) == 0

    def test_missing_version_yml(self, tmp_path: Path) -> None:
        """Missing version.yml (but .bay-version present) produces fail()."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.39.3")
        # No version.yml

        with _JsonMode():
            result = ValidationResult()
            _validate_version_drift(tmp_path, bay_dir, result)

        assert len(result.failed) == 1
        assert "version.yml" in result.failed[0]

    def test_dev_link_mode(self, tmp_path: Path) -> None:
        """Dev-link active produces warn(), no failure, even with mismatch."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.39.3")
        _write_version_yml(bay_dir, "v0.38.0")  # mismatch
        _set_dev_link(tmp_path)

        with _JsonMode():
            result = ValidationResult()
            _validate_version_drift(tmp_path, bay_dir, result)

        assert len(result.warnings) == 1
        assert "dev-link" in result.warnings[0]
        assert len(result.failed) == 0


# ── run_validation integration ───────────────────────────────────────────


class TestRunValidationVersionSection:
    """Test that run_validation() output includes a framework version entry."""

    def test_includes_framework_version_check(self, tmp_path: Path) -> None:
        """run_validation with bay_dir includes a Framework version result."""
        from bay_cli.commands.validate import run_validation

        # Minimal consumer structure
        gv = tmp_path / "group_vars" / "all"
        gv.mkdir(parents=True)
        (gv / "services.yml").write_text(
            "---\nservices:\n  app:\n    image: nginx:latest\n"
            "    access: public\n    domains:\n      - app.example.com\n"
            "    ports:\n      internal: 8080\naccessories: {}\n"
        )
        (gv / "main.yml").write_text("---\ndomain_base: example.com\n")
        hosts_dir = tmp_path / "hosts"
        hosts_dir.mkdir()
        (hosts_dir / "production").write_text("[production]\n10.0.0.1\n")

        # Set up framework version
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.40.0")
        _write_version_yml(bay_dir, "v0.40.0")

        with _JsonMode():
            result = run_validation(
                tmp_path, "production", bay_dir=bay_dir, show_banner=False
            )

        assert result.total_issues == 0
        all_msgs = result.passed + result.warnings
        assert any("Framework version" in msg for msg in all_msgs)


# ── guards.check_bay_version tests ─────────────────────────────────────


class TestGuardCheckBayVersion:
    """Tests for guards.check_bay_version()."""

    def test_matching_versions(self, tmp_path: Path) -> None:
        """Matching versions: no exception raised."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.39.3")
        _write_version_yml(bay_dir, "v0.39.3")

        # Should not raise
        check_bay_version(bay_dir, root=tmp_path)

    def test_matching_versions_v_prefix_mismatch(self, tmp_path: Path) -> None:
        """Pinned 'v0.41.4' matches installed '0.41.4' in guard check."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.41.4")
        _write_version_yml(bay_dir, "0.41.4")

        # Should not raise
        check_bay_version(bay_dir, root=tmp_path)

    def test_mismatched_versions(self, tmp_path: Path) -> None:
        """Mismatched versions raise BayError with both versions."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.39.3")
        _write_version_yml(bay_dir, "v0.38.0")

        with pytest.raises(BayError, match="mismatch"):
            check_bay_version(bay_dir, root=tmp_path)

    def test_missing_version_yml(self, tmp_path: Path) -> None:
        """Missing version.yml raises BayError."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.39.3")
        # No version.yml

        with pytest.raises(BayError, match="missing or unreadable"):
            check_bay_version(bay_dir, root=tmp_path)

    def test_dev_link_mode(self, tmp_path: Path) -> None:
        """Dev-link mode: no exception raised even with mismatch."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_pinned_version(tmp_path, "v0.39.3")
        _write_version_yml(bay_dir, "v0.38.0")  # mismatch
        _set_dev_link(tmp_path)

        # Should not raise
        check_bay_version(bay_dir, root=tmp_path)

    def test_missing_pinned_version(self, tmp_path: Path) -> None:
        """Missing .bay-version: no exception (backward compat)."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_version_yml(bay_dir, "v0.39.3")
        # No .bay-version file

        # Should not raise
        check_bay_version(bay_dir, root=tmp_path)
