"""The log_archive per-container fan is one rendered script (M111 P5).

Sixteen looped Ansible tasks created the per-service artefacts, one round
trip per container per artefact kind. They are now a single template whose
container list is resolved at render time. This file pins the behaviour that
made the old tasks safe:

  * every artefact exists exactly once per service,
  * the normal/sensitive permission split survives the collapse,
  * the sensitive-mode 0700 root:root invariant is still asserted, and
  * the script reports what it changed instead of claiming `changed: true`.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _REPO_ROOT / "roles" / "log_archive" / "templates" / "setup-log-dirs.sh.j2"
_TASKS = _REPO_ROOT / "roles" / "log_archive" / "tasks" / "main.yml"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402

_ARTEFACTS = (
    "",
    "/.malformed",
    "/live.log",
    "/.cursor",
    "/.prune-log",
    "/today.log",
    "/.retention",
)

_ROOT = "/opt/stack/logs/services"


def _containers():
    return [
        {
            "key": "api",
            "value": {"log_retention": {"mode": "normal", "days": 30, "compress": True}},
        },
        {
            "key": "billing",
            "value": {
                "log_retention": {
                    "mode": "sensitive",
                    "days": 7,
                    "max_total_size": "500M",
                    "compress": False,
                }
            },
        },
    ]


def _render(containers=None) -> str:
    env = make_ansible_env(_TEMPLATE.parent)
    return env.get_template(_TEMPLATE.name).render(
        ansible_managed="test",
        app_user="appuser",
        app_user_group="appuser",
        log_archive_group="argo-logreaders",  # legacy-argo: unix group on hosts
        log_archive_log_root=_ROOT,
        log_archive_default_mode="normal",
        log_archive_default_compress=True,
        _log_archive_containers=_containers() if containers is None else containers,
    )


_MARKER = "# ── per-service artefacts (generated"


def _calls(rendered: str) -> list[list[str]]:
    """Every ensure_*/assert_* invocation, shlex-split into its real words.

    Split over the whole generated section rather than line by line: the
    `.retention` body is a single quoted argument that spans four lines.
    """
    section = rendered.split(_MARKER, 1)[1]
    section = section.rsplit("exit 0", 1)[0]
    out: list[list[str]] = []
    for word in shlex.split(section, comments=True):
        if word.startswith(("ensure_", "assert_")):
            out.append([word])
        elif out:
            out[-1].append(word)
    return out


def test_every_artefact_appears_exactly_once_per_service():
    calls = _calls(_render())
    targets = [c[1] for c in calls if c[0].startswith("ensure_")]
    for svc in ("api", "billing"):
        for suffix in _ARTEFACTS:
            path = f"{_ROOT}/{svc}{suffix}"
            hits = targets.count(path)
            assert hits == 1, f"{path} rendered {hits} times, expected 1"
    # Nothing else snuck in.
    assert len(targets) == 2 * len(_ARTEFACTS)


def test_normal_and_sensitive_permission_split_is_preserved():
    calls = {(c[0], c[1]): c[2:] for c in _calls(_render())}
    assert calls[("ensure_dir", f"{_ROOT}/api")] == ["appuser", "argo-logreaders", "750"]  # legacy-argo: unix group on hosts
    assert calls[("ensure_file", f"{_ROOT}/api/live.log")] == [
        "appuser",
        "argo-logreaders",  # legacy-argo: unix group on hosts
        "640",
    ]
    assert calls[("ensure_dir", f"{_ROOT}/billing")] == ["root", "root", "700"]
    assert calls[("ensure_file", f"{_ROOT}/billing/live.log")] == ["root", "root", "600"]
    assert calls[("ensure_dir", f"{_ROOT}/billing/.malformed")] == ["root", "root", "700"]


def test_sensitive_invariant_is_asserted_only_for_sensitive_services():
    rendered = _render()
    sensitive = [c[1] for c in _calls(rendered) if c[0] == "assert_sensitive"]
    assert sensitive == [f"{_ROOT}/billing"]
    # The check itself, not just the call.
    assert '"$m" != "700" || "$o" != "root" || "$g" != "root"' in rendered
    assert "exit 1" in rendered


def test_script_is_strict_and_reports_changes():
    rendered = _render()
    assert rendered.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in rendered
    assert "printf 'CHANGED: %s\\n'" in rendered


def test_retention_values_are_shell_quoted():
    """`.retention` is `source`d by archive-logs.sh — an unquoted value ran."""
    rendered = _render(
        [
            {
                "key": "api",
                "value": {"log_retention": {"mode": "normal", "days": "$(id)"}},
            }
        ]
    )
    assert "$(id)" in rendered  # it is present...
    assert "days=$(id)" not in rendered  # ...but never as a bare assignment
    body = [c for c in _calls(rendered) if c[0] == "ensure_content"][0][5]
    assert "days='$(id)'" in body


def test_empty_container_list_renders_a_valid_no_op_script():
    rendered = _render([])
    assert "ensure_dir" not in rendered.split("exit 0")[-1]
    assert rendered.rstrip().endswith("exit 0")


@pytest.mark.skipif(
    subprocess.run(["which", "bash"], capture_output=True).returncode != 0,
    reason="bash not available",
)
def test_rendered_script_is_syntactically_valid_bash(tmp_path):
    script = tmp_path / "setup-log-dirs.sh"
    script.write_text(_render())
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_no_task_in_the_role_loops_over_the_container_list():
    """The whole point of P5: the fan is gone."""
    tasks = yaml.safe_load(_TASKS.read_text())

    def walk(items):
        for t in items:
            yield t
            for key in ("block", "rescue", "always"):
                if key in t:
                    yield from walk(t[key])

    looping = [
        t["name"]
        for t in walk(tasks)
        if "_log_archive_containers" in str(t.get("loop", ""))
    ]
    assert looping == [], f"still fanning over the container list: {looping}"


def test_the_setup_task_derives_changed_from_script_output():
    def walk(items):
        for t in items:
            yield t
            for key in ("block", "rescue", "always"):
                if key in t:
                    yield from walk(t[key])

    tasks = list(walk(yaml.safe_load(_TASKS.read_text())))
    run = next(t for t in tasks if t.get("name") == "Create per-service log artefacts")
    assert "_log_archive_setup.stdout" in str(run["changed_when"])
    assert run["changed_when"] is not True
