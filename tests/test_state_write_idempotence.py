"""Every writer under {{ stack_dir }}/state must leave a group-writable file.

roles/git_deploy/tasks/webhook.yml's "Re-group pre-existing state files" task
recursively sweeps state/ to group git_deploy_build_group with g+w. Anything
that writes a file there without that group + write bit gets flipped by the
sweep on the very next run, and reports "changed" forever instead of once.

Two concrete regressions of this shape:

1. roles/git_deploy/tasks/main.yml wrote the git-deploy-config.timestamp
   sentinel with mode 0644 and no explicit group, unconditionally, on every
   single deploy. Since the sentinel is always rewritten, this guaranteed
   "changed" every run even when nothing else in the deploy changed.
2. roles/git_deploy/tasks/main.yml's "Pull freshly-pushed images on
   deployment server" task (the git_deploy-side counterpart to build_image's
   batched pull, see test_image_pull_batch.py) had changed_when: true,
   unconditionally, regardless of whether docker pull actually downloaded
   anything.

This test pins the fix for both, plus the same group-write requirement on
the other state/ writers named in the M112 fix: cb_state_migration.yml's
migration script, rebuild.sh's _write_state, and the stall watchdog's audit
log + rate-limit file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GIT_DEPLOY_TASKS = _REPO_ROOT / "roles" / "git_deploy" / "tasks"
_TEMPLATES = _REPO_ROOT / "roles" / "git_deploy" / "templates"

_EXPECTED_GROUP = "{{ git_deploy_build_group }}"
_EXPECTED_MODE = "0664"


def _flatten(tasks):
    for task in tasks:
        if task is None:
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _flatten(task[key])


def _load_tasks(name: str) -> list[dict]:
    loaded = yaml.safe_load((_GIT_DEPLOY_TASKS / name).read_text())
    return list(_flatten(loaded))


def _find_by_name(tasks, prefix: str) -> list[dict]:
    return [t for t in tasks if isinstance(t, dict) and str(t.get("name", "")).startswith(prefix)]


def test_sentinel_tasks_declare_shared_group_and_mode_0664():
    tasks = _find_by_name(_load_tasks("main.yml"), "Write git_deploy config sentinel timestamp")
    assert len(tasks) >= 2, "expected the deploy-server and build-server sentinel tasks"
    for task in tasks:
        args = task["ansible.builtin.copy"]
        assert args.get("group") == _EXPECTED_GROUP, (
            f"{task['name']!r} sets group={args.get('group')!r}, expected "
            f"{_EXPECTED_GROUP!r} — without it the next deploy's re-group "
            f"sweep finds this file and reports changed again"
        )
        assert args.get("mode") == _EXPECTED_MODE, (
            f"{task['name']!r} sets mode={args.get('mode')!r}, expected "
            f"{_EXPECTED_MODE!r} (0644 has no group write bit)"
        )


def test_remote_pull_changed_when_checks_downloaded_marker():
    tasks = _find_by_name(_load_tasks("main.yml"), "Pull freshly-pushed images")
    assert len(tasks) == 1
    changed_when = tasks[0]["changed_when"]
    assert changed_when is not True, (
        "changed_when: true reports changed on every deploy, even when "
        "docker pull found nothing new to download"
    )
    assert "Downloaded newer image" in changed_when


def test_cb_state_migration_script_writes_group_writable_files():
    text = (_GIT_DEPLOY_TASKS / "cb_state_migration.yml").read_text()
    assert "os.chmod(tmp, 0o664)" in text
    assert "os.chmod(tmp, 0o644)" not in text


def test_rebuild_sh_write_state_is_group_writable():
    text = (_TEMPLATES / "rebuild.sh.j2").read_text()
    assert "chmod 0664 \"${tmpfile}\"" in text
    assert "chmod 0644 \"${tmpfile}\"" not in text


def test_watchdog_state_files_are_group_writable():
    text = (_TEMPLATES / "bay-trigger-watchdog.sh.j2").read_text()
    assert 'chmod 0664 "${AUDIT_LOG}"' in text
    assert 'chmod 0664 "${tmpfile}"' in text
