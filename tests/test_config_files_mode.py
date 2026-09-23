"""`config_files_mode`: opt-in world-readable config files.

deploy_stack writes every `config_files` entry as 0640 and its directories as
0750, owner app_user, group docker. A container whose process runs as a uid
outside that group (a node image runs as uid 1000) cannot read a folder
bind-mounted from there. `config_files_mode: public` on a service or accessory
makes that definition's files 0644 and every directory on the way to them,
config/ itself included, 0755. Absent or `private`, nothing changes.

The mode tests run the real roles/deploy_stack/tasks/validate.yml and
config_files.yml with ansible-playbook against a scratch stack_dir, then stat
what landed on disk. They need the `docker` group (the tasks chgrp to it) and
skip without it; the CI runner user is in it.

Every positive assertion has a control that must fail: the default tree is
checked against the public expectation and vice versa, and the refused value
is paired with an accepted one on the same fixture.
"""

from __future__ import annotations

import getpass
import grp
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TASKS = _REPO_ROOT / "roles" / "deploy_stack" / "tasks"
_FILTERS = _REPO_ROOT / "filter_plugins"

sys.path.insert(0, str(_FILTERS))
from bay_filters import bay_config_dirs  # noqa: E402

from bay_cli.commands.validate import (  # noqa: E402
    ValidationResult,
    _validate_services_schema,
)

# ── helpers ──────────────────────────────────────────────────────────────


def _in_docker_group() -> bool:
    try:
        group = grp.getgrnam("docker")
    except KeyError:
        return False
    return group.gr_gid in os.getgroups() or getpass.getuser() in group.gr_mem


needs_docker_group = pytest.mark.skipif(
    not _in_docker_group(),
    reason="config_files.yml chgrps to 'docker'; this user is not in it",
)


def _mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))[2:].zfill(4)


def _run(
    tmp_path: Path,
    services: dict,
    accessories: dict | None = None,
    files: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run validate.yml then config_files.yml on localhost.

    Layout mirrors a consumer: the playbook sits in <root>/.bay/ so the copy
    task's `playbook_dir | dirname`/files resolves to <root>/files.
    """
    root = tmp_path / "consumer"
    for rel, body in (files or {}).items():
        target = root / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    bay = root / ".bay"
    bay.mkdir(parents=True, exist_ok=True)
    extra = {
        "app_user": getpass.getuser(),
        "stack_dir": str(tmp_path / "stack"),
        "active_services": services,
        "active_accessories": accessories or {},
    }
    (bay / "vars.json").write_text(json.dumps(extra))
    # An empty config, so the framework's own ansible.cfg (inventory, roles)
    # does not leak into the run.
    (bay / "ansible.cfg").write_text("[defaults]\n")
    (bay / "play.yml").write_text(
        "- hosts: localhost\n"
        "  connection: local\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        f"    - ansible.builtin.include_tasks: {_TASKS / 'validate.yml'}\n"
        f"    - ansible.builtin.include_tasks: {_TASKS / 'config_files.yml'}\n"
    )
    env = {
        **os.environ,
        "ANSIBLE_FILTER_PLUGINS": str(_FILTERS),
        "ANSIBLE_CONFIG": str(bay / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
    }
    return subprocess.run(
        [
            sys.executable, "-m", "ansible.cli.playbook",
            "-i", "localhost,",
            "-e", f"@{bay / 'vars.json'}",
            str(bay / "play.yml"),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _ok(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]


def _changed(proc: subprocess.CompletedProcess[str]) -> int:
    for line in proc.stdout.splitlines():
        if line.startswith("localhost") and "changed=" in line:
            return int(line.split("changed=")[1].split()[0])
    raise AssertionError(f"no PLAY RECAP line in:\n{proc.stdout[-2000:]}")


_FILES = {
    "legal/en/terms.md": "# Terms\n",
    "legal/de/terms.md": "# Bedingungen\n",
    "app/secret.conf": "token=not-a-real-one\n",
}


def _assert_tree(stack: Path, *, public: bool) -> None:
    config = stack / "config"
    want_dir = "0755" if public else "0750"
    want_file = "0644" if public else "0640"
    assert _mode(config) == want_dir
    for d in ("legal", "legal/en", "legal/de"):
        assert _mode(config / d) == want_dir, d
    for f in ("legal/en/terms.md", "legal/de/terms.md"):
        assert _mode(config / f) == want_file, f


# ── bay_config_dirs ──────────────────────────────────────────────────────


def test_config_dirs_lists_every_parent_once_parents_first():
    assert bay_config_dirs(
        ["legal/en/terms.md", "legal/de/terms.md", "top.conf"]
    ) == ["", "legal", "legal/en", "legal/de"]


def test_config_dirs_is_empty_without_paths():
    # The default path depends on this: no public file, no extra loop item.
    assert bay_config_dirs([]) == []
    assert bay_config_dirs(None) == []
    # Control: one top-level file still needs config/ itself.
    assert bay_config_dirs(["top.conf"]) == [""]


def test_config_dirs_spells_entries_like_ansible_dirname():
    # A mismatch would give one directory two modes and flip it every run.
    assert "a//b" in bay_config_dirs(["a//b/c.md"])
    assert "./x" in bay_config_dirs(["./x/y"])


# ── bin/bay validate: the schema enum ────────────────────────────────────


def _schema_failures(tmp_path: Path, mode: object, kind: str = "services") -> list[str]:
    from bay_cli.console.output import set_json_mode

    entry: dict = {
        "image": "example/app:1",
        "config_files": ["legal/en/terms.md"],
        "config_files_mode": mode,
    }
    if kind == "services":
        entry.update(access="public", domains=["app.example.com"], ports={"internal": 3000})
    data = {"services": {}, "accessories": {}}
    data[kind]["web"] = entry
    result = ValidationResult()
    set_json_mode(True)
    try:
        _validate_services_schema(
            tmp_path, {"group_vars/all/services.yml": data}, result
        )
    finally:
        set_json_mode(False)
    return result.failed


@pytest.mark.parametrize("kind", ["services", "accessories"])
def test_schema_refuses_an_unknown_mode(tmp_path, kind):
    failed = _schema_failures(tmp_path, "world", kind)
    assert any("config_files_mode" in f for f in failed), failed


@pytest.mark.parametrize("kind", ["services", "accessories"])
@pytest.mark.parametrize("mode", ["public", "private"])
def test_schema_accepts_the_two_modes(tmp_path, kind, mode):
    # Control for the refusal above: same fixture, a valid value, no failure.
    assert _schema_failures(tmp_path, mode, kind) == []


# ── deploy_stack: modes on disk ──────────────────────────────────────────


@needs_docker_group
def test_default_keeps_0640_files_and_0750_dirs(tmp_path):
    proc = _run(
        tmp_path,
        {"web": {"image": "x", "config_files": list(_FILES)}},
        files=_FILES,
    )
    _ok(proc)
    stack = tmp_path / "stack"
    _assert_tree(stack, public=False)
    assert _mode(stack / "config" / "app" / "secret.conf") == "0640"
    # Control: the public expectation must fail on this tree.
    with pytest.raises(AssertionError):
        _assert_tree(stack, public=True)


@needs_docker_group
def test_explicit_private_matches_the_default(tmp_path):
    proc = _run(
        tmp_path,
        {"web": {"image": "x", "config_files": list(_FILES), "config_files_mode": "private"}},
        files=_FILES,
    )
    _ok(proc)
    _assert_tree(tmp_path / "stack", public=False)


@needs_docker_group
def test_public_sets_0644_files_and_0755_dirs(tmp_path):
    public = ["legal/en/terms.md", "legal/de/terms.md"]
    services = {
        "docs": {"image": "x", "config_files": public, "config_files_mode": "public"},
        "app": {"image": "x", "config_files": ["app/secret.conf"]},
    }
    # First run as a private-only host, so config/ and legal/ start at 0750
    # and the public run has to widen directories that already exist.
    _ok(_run(tmp_path, {"docs": {"image": "x", "config_files": public}}, files=_FILES))
    _assert_tree(tmp_path / "stack", public=False)

    proc = _run(tmp_path, services, files=_FILES)
    _ok(proc)
    stack = tmp_path / "stack"
    _assert_tree(stack, public=True)
    # A private definition beside it keeps its own modes.
    assert _mode(stack / "config" / "app") == "0750"
    assert _mode(stack / "config" / "app" / "secret.conf") == "0640"
    # Control: the default expectation must fail on this tree.
    with pytest.raises(AssertionError):
        _assert_tree(stack, public=False)

    # No directory gets two modes in one run: a second run changes nothing.
    again = _run(tmp_path, services, files=_FILES)
    _ok(again)
    assert _changed(again) == 0, again.stdout[-3000:]

    # Dropping the key narrows the files, config/ and each file's own folder
    # on the next deploy. A folder that only holds folders (legal/) is not in
    # the default loop, so it keeps 0755; the docs say so.
    del services["docs"]["config_files_mode"]
    _ok(_run(tmp_path, services, files=_FILES))
    config = stack / "config"
    assert _mode(config) == "0750"
    for d in ("legal/en", "legal/de"):
        assert _mode(config / d) == "0750", d
    for f in ("legal/en/terms.md", "legal/de/terms.md"):
        assert _mode(config / f) == "0640", f
    assert _mode(config / "legal") == "0755"


@needs_docker_group
def test_public_on_an_accessory(tmp_path):
    proc = _run(
        tmp_path,
        {},
        {"cache": {"config_files": ["legal/en/terms.md"], "config_files_mode": "public"}},
        files=_FILES,
    )
    _ok(proc)
    config = tmp_path / "stack" / "config"
    assert _mode(config / "legal" / "en" / "terms.md") == "0644"
    assert _mode(config / "legal") == "0755"


@needs_docker_group
def test_a_file_listed_public_and_private_is_public(tmp_path):
    shared = ["legal/en/terms.md"]
    proc = _run(
        tmp_path,
        {
            "a": {"image": "x", "config_files": shared},
            "b": {"image": "x", "config_files": shared, "config_files_mode": "public"},
        },
        files=_FILES,
    )
    _ok(proc)
    assert _mode(tmp_path / "stack" / "config" / "legal" / "en" / "terms.md") == "0644"


@needs_docker_group
def test_deploy_refuses_an_unknown_mode(tmp_path):
    bad = _run(
        tmp_path,
        {"docs": {
            "image": "x",
            "config_files": ["legal/en/terms.md"],
            "config_files_mode": "world",
        }},
        files=_FILES,
    )
    assert bad.returncode != 0
    assert "config_files_mode" in bad.stdout
    # Refused before anything is written.
    assert not (tmp_path / "stack" / "config").exists()

    # Control: the same fixture with a valid value deploys.
    good = _run(
        tmp_path,
        {"docs": {
            "image": "x",
            "config_files": ["legal/en/terms.md"],
            "config_files_mode": "public",
        }},
        files=_FILES,
    )
    _ok(good)
