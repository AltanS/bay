"""Validate's network probe cache (M111-04 / audit P2).

`bin/bay validate` runs `git ls-remote` per build service and `skopeo inspect`
per image service, on every invocation — including the implicit validate that
every deploy runs. The cache turns a repeat run inside the TTL into zero
subprocesses.

The invariants that matter, and are asserted here:

- a cold cache still probes, and records the success;
- a warm hit inside the TTL runs **no** subprocess;
- an entry older than the TTL is a miss;
- a `warn` outcome is never written (caching a failure hides a fix);
- credentials embedded in a repo URL never reach the cache file;
- a corrupt cache file is an empty cache, never an error.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from bay_cli.commands.validate import (
    _PROBE_CACHE_MAX_AGE,
    ProbeCache,
    ValidationResult,
    _check_docker_images,
    _check_git_repos,
    _strip_url_credentials,
)

CACHE_NAME = ".validate-probe-cache"


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _read_cache(bay_dir: Path) -> dict:
    return json.loads((bay_dir / CACHE_NAME).read_text())["entries"]


@pytest.fixture
def bay_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".bay"
    d.mkdir()
    return d


# ── Git repo probes ──────────────────────────────────────────────────────


class TestGitProbeCache:
    SERVICES = {"app": {"build": {"repo": "https://github.com/acme/app.git", "branch": "main"}}}
    LS_REMOTE_OK = "abc123\trefs/heads/main\n"

    def test_cold_cache_probes_and_records(self, bay_dir: Path) -> None:
        cache = ProbeCache(bay_dir)
        result = ValidationResult()
        with patch("subprocess.run", return_value=_proc(0, self.LS_REMOTE_OK)) as run:
            _check_git_repos(self.SERVICES, {}, result, cache)
        assert run.call_count == 1
        cache.flush()
        entries = _read_cache(bay_dir)
        assert list(entries) == ["git:https://github.com/acme/app.git@main"]
        assert entries["git:https://github.com/acme/app.git@main"]["result"] == "ok"
        assert any("accessible" in m for m in result.passed)

    def test_warm_hit_runs_no_subprocess(self, bay_dir: Path) -> None:
        first = ProbeCache(bay_dir)
        with patch("subprocess.run", return_value=_proc(0, self.LS_REMOTE_OK)):
            _check_git_repos(self.SERVICES, {}, ValidationResult(), first)
        first.flush()

        second = ProbeCache(bay_dir)
        result = ValidationResult()
        with patch("subprocess.run", side_effect=AssertionError("probed on a cache hit")) as run:
            _check_git_repos(self.SERVICES, {}, result, second)
        assert run.call_count == 0
        assert any("accessible" in m for m in result.passed)
        assert not result.warnings

    def test_entry_older_than_ttl_is_a_miss(self, bay_dir: Path) -> None:
        stale = {
            "version": 1,
            "entries": {
                "git:https://github.com/acme/app.git@main": {
                    "result": "ok",
                    "detail": "repo accessible",
                    "checked_at": time.time() - (_PROBE_CACHE_MAX_AGE + 60),
                }
            },
        }
        (bay_dir / CACHE_NAME).write_text(json.dumps(stale))

        with patch("subprocess.run", return_value=_proc(0, self.LS_REMOTE_OK)) as run:
            _check_git_repos(self.SERVICES, {}, ValidationResult(), ProbeCache(bay_dir))
        assert run.call_count == 1

    def test_warn_outcome_is_never_written(self, bay_dir: Path) -> None:
        cache = ProbeCache(bay_dir)
        result = ValidationResult()
        with patch("subprocess.run", return_value=_proc(128, "", "repository not found")):
            _check_git_repos(self.SERVICES, {}, result, cache)
        cache.flush()
        assert any("unreachable" in w for w in result.warnings)
        if (bay_dir / CACHE_NAME).exists():
            assert _read_cache(bay_dir) == {}

    def test_missing_branch_is_never_cached(self, bay_dir: Path) -> None:
        cache = ProbeCache(bay_dir)
        result = ValidationResult()
        with patch("subprocess.run", return_value=_proc(0, "abc\trefs/heads/other\n")):
            _check_git_repos(self.SERVICES, {}, result, cache)
        cache.flush()
        assert any("not found" in w for w in result.warnings)
        if (bay_dir / CACHE_NAME).exists():
            assert _read_cache(bay_dir) == {}

    def test_bypass_flag_reprobes_and_refreshes(self, bay_dir: Path) -> None:
        warm = ProbeCache(bay_dir)
        with patch("subprocess.run", return_value=_proc(0, self.LS_REMOTE_OK)):
            _check_git_repos(self.SERVICES, {}, ValidationResult(), warm)
        warm.flush()

        bypass = ProbeCache(bay_dir, enabled=False)
        with patch("subprocess.run", return_value=_proc(0, self.LS_REMOTE_OK)) as run:
            _check_git_repos(self.SERVICES, {}, ValidationResult(), bypass)
        assert run.call_count == 1
        bypass.flush()
        assert _read_cache(bay_dir)  # still refreshed, not bypassed-and-dropped

    def test_branch_is_part_of_the_key(self, bay_dir: Path) -> None:
        cache = ProbeCache(bay_dir)
        with patch("subprocess.run", return_value=_proc(0, self.LS_REMOTE_OK)):
            _check_git_repos(self.SERVICES, {}, ValidationResult(), cache)
        other = {"app": {"build": {"repo": "https://github.com/acme/app.git", "branch": "dev"}}}
        with patch("subprocess.run", return_value=_proc(0, "abc\trefs/heads/dev\n")) as run:
            _check_git_repos(other, {}, ValidationResult(), cache)
        assert run.call_count == 1


# ── Credential stripping ─────────────────────────────────────────────────


class TestCredentialStripping:
    SECRET = "ghp_supersecrettoken"

    def test_strip_url_credentials_drops_userinfo(self) -> None:
        assert (
            _strip_url_credentials(f"https://x-access-token:{self.SECRET}@github.com/acme/app.git")
            == "https://github.com/acme/app.git"
        )
        assert (
            _strip_url_credentials("https://github.com/acme/app.git")
            == "https://github.com/acme/app.git"
        )
        # ssh scp-style "git@host:path" has no scheme separator — left alone
        assert _strip_url_credentials("git@github.com:acme/app.git") == "git@github.com:acme/app.git"

    def test_credential_never_reaches_the_cache_file(self, bay_dir: Path) -> None:
        services = {
            "app": {
                "build": {
                    "repo": f"https://x-access-token:{self.SECRET}@github.com/acme/app.git",
                    "branch": "main",
                }
            }
        }
        cache = ProbeCache(bay_dir)
        with patch("subprocess.run", return_value=_proc(0, "abc\trefs/heads/main\n")):
            _check_git_repos(services, {}, ValidationResult(), cache)
        cache.flush()
        raw = (bay_dir / CACHE_NAME).read_text()
        assert self.SECRET not in raw
        assert "x-access-token" not in raw
        assert "git:https://github.com/acme/app.git@main" in raw

    def test_credentialed_and_bare_urls_share_a_key(self, bay_dir: Path) -> None:
        """A token rotation must not invalidate the cache, and vice versa."""
        assert ProbeCache.git_key(
            f"https://x-access-token:{self.SECRET}@github.com/acme/app.git", "main"
        ) == ProbeCache.git_key("https://github.com/acme/app.git", "main")


# ── Image probes ─────────────────────────────────────────────────────────


class TestImageProbeCache:
    SERVICES = {"cache": {"image": "redis:7-alpine"}}

    def test_cold_then_warm(self, bay_dir: Path) -> None:
        cache = ProbeCache(bay_dir)
        with patch("bay_cli.commands.validate._command_exists", return_value=True):
            with patch("subprocess.run", return_value=_proc(0, "{}")) as run:
                _check_docker_images(self.SERVICES, {}, ValidationResult(), cache)
            assert run.call_count == 1
            cache.flush()
            assert list(_read_cache(bay_dir)) == ["image:redis:7-alpine"]

            second = ProbeCache(bay_dir)
            result = ValidationResult()
            with patch("subprocess.run", side_effect=AssertionError("probed on a hit")) as run2:
                _check_docker_images(self.SERVICES, {}, result, second)
            assert run2.call_count == 0
            assert any("exists" in m for m in result.passed)

    def test_unverified_image_is_never_cached(self, bay_dir: Path) -> None:
        cache = ProbeCache(bay_dir)
        result = ValidationResult()
        with patch("bay_cli.commands.validate._command_exists", return_value=True):
            with patch("subprocess.run", return_value=_proc(1, "", "manifest unknown")):
                _check_docker_images(self.SERVICES, {}, result, cache)
        cache.flush()
        assert any("not verified" in w for w in result.warnings)
        if (bay_dir / CACHE_NAME).exists():
            assert _read_cache(bay_dir) == {}


# ── Cache file robustness ────────────────────────────────────────────────


class TestCacheFile:
    def test_corrupt_file_is_ignored(self, bay_dir: Path) -> None:
        (bay_dir / CACHE_NAME).write_text("{not json at all")
        cache = ProbeCache(bay_dir)
        assert cache.get("git:https://example.com/x.git@") is None
        cache.put("git:https://example.com/x.git@", "ok", "repo accessible")
        cache.flush()
        assert list(_read_cache(bay_dir)) == ["git:https://example.com/x.git@"]

    def test_wrong_shape_is_ignored(self, bay_dir: Path) -> None:
        (bay_dir / CACHE_NAME).write_text(json.dumps({"entries": ["not", "a", "mapping"]}))
        assert ProbeCache(bay_dir).get("anything") is None

    def test_no_bay_dir_is_a_no_op(self) -> None:
        cache = ProbeCache(None)
        cache.put("git:x@", "ok", "repo accessible")
        assert cache.get("git:x@") == "repo accessible"
        cache.flush()  # must not raise

    def test_file_is_owner_only(self, bay_dir: Path) -> None:
        cache = ProbeCache(bay_dir)
        cache.put("image:nginx:latest", "ok", "image reference resolved")
        cache.flush()
        assert (bay_dir / CACHE_NAME).stat().st_mode & 0o077 == 0

    def test_flush_drops_expired_entries(self, bay_dir: Path) -> None:
        cache = ProbeCache(bay_dir)
        cache._entries["image:old:1"] = {
            "result": "ok",
            "detail": "",
            "checked_at": time.time() - (_PROBE_CACHE_MAX_AGE + 1),
        }
        cache.put("image:new:1", "ok", "image reference resolved")
        cache.flush()
        assert list(_read_cache(bay_dir)) == ["image:new:1"]

    def test_ttl_matches_the_rig_cache(self) -> None:
        from bay_cli.commands.ops import _RIG_CACHE_MAX_AGE

        assert _PROBE_CACHE_MAX_AGE == _RIG_CACHE_MAX_AGE == 3600
