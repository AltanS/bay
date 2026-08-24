"""Tests for the `Bootstrap stack directory on build server` play in deploy.yml.

Regression coverage for the sandbox provisioning gap discovered 2026-04-29.
Symptom: when a consumer declares `build_server: <other_host>` and the
build_server is NOT in the deploy play's host group (e.g. sandbox targets
`testing` but build_server is the demo infra host), the build_server's
/opt/<stack>/ directory was never created. The git_deploy role's
"Set up build server for remote builds" block then failed with EACCES when
it tried to mkdir /opt/<stack>/push-builds as app_user (the parent /opt/
requires root).

Fix: deploy.yml has a new play between Bootstrap and Deploy that runs as
root, delegates to build_server, and ensures /opt/<stack>/ exists with
{owner: app_user, group: docker, mode: 0755}.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def deploy_plays() -> list[dict]:
    deploy_yml = (Path(__file__).resolve().parent.parent / "deploy.yml").read_text()
    return yaml.safe_load(deploy_yml)


def _find_play(plays: list[dict], name_substr: str) -> dict | None:
    for play in plays:
        if name_substr in play.get("name", ""):
            return play
    return None


class TestBuildServerBootstrapPlay:
    def test_play_exists(self, deploy_plays: list[dict]) -> None:
        play = _find_play(deploy_plays, "build server")
        assert play is not None, (
            "deploy.yml must include a play named 'Bootstrap stack directory on build server'. "
            "Without it, /opt/<stack> is never created on cross-host build_servers."
        )

    def test_play_runs_as_root(self, deploy_plays: list[dict]) -> None:
        """become: true with no become_user → root. Required because mkdir /opt/<stack>
        needs root permissions (app_user can't create directories under /opt)."""
        play = _find_play(deploy_plays, "build server")
        assert play.get("become") is True
        assert "become_user" not in play, (
            "build-server bootstrap must run as root, not as app_user. "
            "/opt/ is root-owned on Linux."
        )

    def test_play_runs_once(self, deploy_plays: list[dict]) -> None:
        """run_once: true so multi-host deploys (eu+na) don't redundantly delegate
        the same task multiple times to the same build_server."""
        play = _find_play(deploy_plays, "build server")
        assert play.get("run_once") is True

    def test_play_targets_target_host(self, deploy_plays: list[dict]) -> None:
        """Hosts must be {{ target_host }} so the play has at least one host
        from which to delegate. delegate_to needs a base host."""
        play = _find_play(deploy_plays, "build server")
        assert play.get("hosts") == "{{ target_host }}"

    def test_play_skips_gather_facts(self, deploy_plays: list[dict]) -> None:
        """gather_facts: false to avoid unnecessary fact-gathering on every deploy."""
        play = _find_play(deploy_plays, "build server")
        assert play.get("gather_facts") is False

    def test_task_creates_stack_dir(self, deploy_plays: list[dict]) -> None:
        """The single task creates {{ stack_dir }} as a directory delegated to
        {{ build_server }}, owned by app_user, group docker."""
        play = _find_play(deploy_plays, "build server")
        tasks = play.get("tasks", [])
        assert len(tasks) >= 1, "build-server bootstrap play must have at least one task"

        # Find the directory creation task
        dir_task = next(
            (t for t in tasks if "ansible.builtin.file" in t),
            None,
        )
        assert dir_task is not None, (
            "build-server bootstrap must include an ansible.builtin.file task"
        )

        file_args = dir_task["ansible.builtin.file"]
        assert file_args["path"] == "{{ stack_dir }}"
        assert file_args["state"] == "directory"
        assert file_args["owner"] == "{{ app_user }}"
        assert file_args["group"] == "docker"
        assert file_args["mode"] == "0755"
        # delegate_to is templated even when `when:` skips the task, so the
        # var must be wrapped in a default to avoid "build_server is undefined"
        # errors on consumers without a build server (myapp). The actual
        # delegation only happens when `when: build_server is defined` passes.
        assert (
            dir_task.get("delegate_to")
            == "{{ build_server | default(inventory_hostname) }}"
        )

    def test_task_skipped_when_no_build_server(self, deploy_plays: list[dict]) -> None:
        """Consumers without remote-build configured (no build_server var) must not
        be affected. The task's `when:` guards on `build_server is defined`."""
        play = _find_play(deploy_plays, "build server")
        dir_task = next(
            (t for t in play["tasks"] if "ansible.builtin.file" in t),
            None,
        )
        when = dir_task.get("when", "")
        if isinstance(when, list):
            when = " ".join(str(c) for c in when)
        assert "build_server is defined" in when, (
            "build-server bootstrap task must be guarded by `when: build_server is defined` "
            "to no-op for consumers without remote-build strategy."
        )

    def test_play_runs_before_deploy_services(self, deploy_plays: list[dict]) -> None:
        """Order matters: build-server bootstrap must come BEFORE the
        Deploy services play, otherwise the git_deploy role's build-server
        block fails on mkdir /opt/<stack>/push-builds before this fix."""
        play_names = [p.get("name", "") for p in deploy_plays]
        bootstrap_idx = next(
            i for i, n in enumerate(play_names) if "build server" in n
        )
        deploy_idx = next(
            i for i, n in enumerate(play_names) if n == "Deploy services"
        )
        assert bootstrap_idx < deploy_idx, (
            "Bootstrap stack directory on build server must run before Deploy services. "
            f"Got bootstrap at index {bootstrap_idx}, Deploy services at {deploy_idx}."
        )

    def test_play_runs_after_main_bootstrap(self, deploy_plays: list[dict]) -> None:
        """The main Bootstrap (target_host) handles the case where build_server == target.
        The build-server bootstrap is additive for the cross-host case. Order is
        cosmetic but consistent: main bootstrap → build-server bootstrap → deploy."""
        play_names = [p.get("name", "") for p in deploy_plays]
        main_bootstrap_idx = next(
            i for i, n in enumerate(play_names) if n == "Bootstrap stack directory"
        )
        build_bootstrap_idx = next(
            i for i, n in enumerate(play_names) if "build server" in n
        )
        assert main_bootstrap_idx < build_bootstrap_idx
