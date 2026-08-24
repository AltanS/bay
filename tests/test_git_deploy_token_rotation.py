"""Tests for git remote set-url before fetch/pull after token rotation.

Covers:
  - Structural ordering: set-url present and before fetch/pull task
  - Task attributes: no_log, changed_when, when conditions
  - bay_token_url filter output format
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# filter_plugins is outside src/, add it to sys.path (follows test_filter_plugins.py pattern)
sys.path.insert(0, str(Path(__file__).parent.parent / "filter_plugins"))

from bay_filters import bay_token_url  # noqa: E402

_ROLES = Path(__file__).resolve().parent.parent / "roles" / "git_deploy" / "tasks"
_REMOTE_BUILD = _ROLES / "remote_build.yml"
_CLONE_REPOS = _ROLES / "clone_repos.yml"


def _load_tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())


def _task_names(tasks: list[dict]) -> list[str]:
    return [t.get("name", "") for t in tasks]


def _find_task(tasks: list[dict], substr: str) -> dict | None:
    for t in tasks:
        if substr in t.get("name", ""):
            return t
    return None


# ── remote_build.yml ──────────────────────────────────────────────────────


class TestRemoteBuildSetUrl:
    @pytest.fixture(scope="class")
    def tasks(self) -> list[dict]:
        return _load_tasks(_REMOTE_BUILD)

    def test_set_url_task_present_before_fetch(self, tasks: list[dict]) -> None:
        """set-url task exists and appears before the Fetch latest changes task."""
        names = _task_names(tasks)
        set_url_indices = [i for i, n in enumerate(names) if "Refresh remote.origin.url" in n]
        fetch_indices = [i for i, n in enumerate(names) if "Fetch latest changes" in n and "SSH" not in n]
        assert set_url_indices, "No 'Refresh remote.origin.url' task found in remote_build.yml"
        assert fetch_indices, "No 'Fetch latest changes' (token) task found in remote_build.yml"
        assert set_url_indices[0] < fetch_indices[0], (
            f"set-url task (index {set_url_indices[0]}) must precede fetch task (index {fetch_indices[0]})"
        )

    def test_set_url_has_no_log(self, tasks: list[dict]) -> None:
        """set-url task in remote_build.yml has no_log: true."""
        task = _find_task(tasks, "Refresh remote.origin.url")
        assert task is not None, "set-url task not found"
        assert task.get("no_log") is True, f"no_log not True on set-url task: {task}"

    def test_set_url_has_changed_when_false(self, tasks: list[dict]) -> None:
        """set-url task in remote_build.yml has changed_when: false."""
        task = _find_task(tasks, "Refresh remote.origin.url")
        assert task is not None, "set-url task not found"
        assert task.get("changed_when") is False, (
            f"changed_when not False on set-url task: {task}"
        )

    def test_set_url_condition_matches_fetch_condition(self, tasks: list[dict]) -> None:
        """set-url task when conditions include stat.exists check and token-defined check."""
        task = _find_task(tasks, "Refresh remote.origin.url")
        assert task is not None, "set-url task not found"
        when = task.get("when", [])
        if isinstance(when, str):
            when = [when]
        when_str = " ".join(str(w) for w in when)
        assert "_remote_repo_stat.stat.exists" in when_str, (
            f"_remote_repo_stat.stat.exists not in when: {when}"
        )
        assert "_build.token is defined" in when_str, (
            f"'_build.token is defined' not in when: {when}"
        )


# ── clone_repos.yml ───────────────────────────────────────────────────────


class TestCloneReposSetUrl:
    @pytest.fixture(scope="class")
    def tasks(self) -> list[dict]:
        return _load_tasks(_CLONE_REPOS)

    def test_clone_repos_set_url_present_before_pull(self, tasks: list[dict]) -> None:
        """set-url task exists and appears before the Pull latest changes (token) task."""
        names = _task_names(tasks)
        set_url_indices = [i for i, n in enumerate(names) if "Refresh remote.origin.url" in n]
        pull_indices = [i for i, n in enumerate(names) if "Pull latest changes (token)" in n]
        assert set_url_indices, "No 'Refresh remote.origin.url' task found in clone_repos.yml"
        assert pull_indices, "No 'Pull latest changes (token)' task found in clone_repos.yml"
        assert set_url_indices[0] < pull_indices[0], (
            f"set-url task (index {set_url_indices[0]}) must precede pull task (index {pull_indices[0]})"
        )

    def test_clone_repos_set_url_no_log(self, tasks: list[dict]) -> None:
        """set-url task in clone_repos.yml has no_log: true."""
        task = _find_task(tasks, "Refresh remote.origin.url")
        assert task is not None, "set-url task not found in clone_repos.yml"
        assert task.get("no_log") is True, f"no_log not True on set-url task: {task}"

    def test_clone_repos_set_url_changed_when_false(self, tasks: list[dict]) -> None:
        """set-url task in clone_repos.yml has changed_when: false."""
        task = _find_task(tasks, "Refresh remote.origin.url")
        assert task is not None, "set-url task not found in clone_repos.yml"
        assert task.get("changed_when") is False, (
            f"changed_when not False on set-url task: {task}"
        )


# ── bay_token_url filter ─────────────────────────────────────────────────


class TestBayTokenUrlFilterOutputFormat:
    def test_bay_token_url_filter_output_format(self) -> None:
        """bay_token_url produces a usable HTTPS URL, not SSH-form or empty string."""
        result = bay_token_url("git@github.com:Org/repo.git", "ghp_testtoken")
        assert result == "https://x-access-token:ghp_testtoken@github.com/Org/repo.git", (
            f"Unexpected token URL: {result!r}"
        )
