"""Guard: an nftables comment-only change must not restart the Docker daemon.

Regression context:
  `roles/nftables/handlers/main.yml` chains `Reload nftables` ->
  `Restart Docker after nftables reload`. Reloading nftables wipes the chains
  Docker installs at daemon start, so the daemon has to be restarted and every
  container on the host bounces.

  `Deploy nftables configuration` (an `ansible.builtin.template`) used to
  `notify: Reload nftables` directly. `template` reports "changed" for ANY byte
  difference, so a comment-only edit to nftables.conf.j2 restarted Docker on
  four production hosts for a documentation change.

  The fix hashes the live file with comments and trailing whitespace stripped
  before and after the write, and notifies the handler from a separate flag
  task only when those hashes differ. The real file is still always written, so
  comments land on the host; only the reload is gated.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).parent.parent
_NFT_TASKS = _REPO_ROOT / "roles" / "nftables" / "tasks" / "main.yml"
_NFT_HANDLERS = _REPO_ROOT / "roles" / "nftables" / "handlers" / "main.yml"

_RELOAD = "Reload nftables"
_DEPLOY = "Deploy nftables configuration"
_FLAG = "Reload nftables only when the ruleset changed semantically"
_BEFORE = "Read live nftables ruleset fingerprint"
_AFTER = "Re-read nftables ruleset fingerprint"


def _tasks() -> list[dict]:
    with _NFT_TASKS.open() as f:
        return yaml.safe_load(f)


def _task(name: str) -> dict:
    for task in _tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in {_NFT_TASKS}")


def _names() -> list[str]:
    return [t.get("name") for t in _tasks()]


def test_template_task_does_not_notify_the_reload_handler() -> None:
    deploy = _task(_DEPLOY)
    notify = deploy.get("notify")
    assert notify is None, (
        f"{_DEPLOY!r} must not notify {_RELOAD!r}: `template` reports changed "
        "for a comment-only diff, and that handler restarts the Docker daemon "
        f"on every container host. Found notify: {notify!r}"
    )


def test_template_task_still_writes_the_real_config() -> None:
    """The gate is on the reload, not on the write — comments must still land."""
    deploy = _task(_DEPLOY)
    tmpl = deploy["ansible.builtin.template"]
    assert tmpl["src"] == "nftables.conf.j2"
    assert tmpl["dest"] == "{{ nftables_config_path }}"
    assert "when" not in deploy, (
        "the config write must be unconditional; only the reload is gated"
    )


def test_reload_is_notified_by_the_semantic_flag_task() -> None:
    flag = _task(_FLAG)
    notify = flag.get("notify")
    notify = [notify] if isinstance(notify, str) else notify
    assert notify == [_RELOAD]
    when = flag["when"]
    assert "_nftables_semantic_before.stdout" in when
    assert "_nftables_semantic_after.stdout" in when
    assert "!=" in when
    assert flag.get("changed_when") is True, (
        "the flag task is a debug; without `changed_when: true` it never "
        "notifies the handler and a real ruleset change would not reload"
    )


def test_exactly_one_task_notifies_the_reload_handler() -> None:
    notifiers = []
    for task in _tasks():
        notify = task.get("notify")
        notify = [notify] if isinstance(notify, str) else (notify or [])
        if _RELOAD in notify:
            notifiers.append(task.get("name"))
    assert notifiers == [_FLAG], (
        "exactly one task may notify the Docker-restarting reload chain; "
        f"found {notifiers}"
    )


def test_fingerprint_probes_bracket_the_write() -> None:
    names = _names()
    assert names.index(_BEFORE) < names.index(_DEPLOY) < names.index(_AFTER) < names.index(_FLAG)


def test_fingerprint_probes_are_readonly_and_run_in_check_mode() -> None:
    """Read-only probes, so they may bypass check mode to give a real answer.

    In check mode the template writes nothing, so before == after, the flag
    task's `when` is false, and nothing is reloaded or restarted — while the
    template task still reports its diff.
    """
    for name in (_BEFORE, _AFTER):
        probe = _task(name)
        assert probe["changed_when"] is False, f"{name} must never report changed"
        assert probe["check_mode"] is False, (
            f"{name} must run in check mode, or the dry run compares nothing"
        )
        cmd = probe["ansible.builtin.shell"]["cmd"]
        assert "sed" in cmd and "#" in cmd, "comments must be stripped"
        assert "sha256sum" in cmd


def test_missing_live_file_counts_as_a_change() -> None:
    """First install has no /etc/nftables.conf; the reload must still fire.

    The probe swallows sed's error and hashes empty input, which cannot equal
    the hash of a rendered ruleset.
    """
    probe = _task(_BEFORE)
    cmd = probe["ansible.builtin.shell"]["cmd"]
    assert "2>/dev/null" in cmd
    assert probe["failed_when"] is False, (
        "a missing file makes sed/grep exit non-zero under pipefail; the probe "
        "must not fail the play, it must report the empty-input hash"
    )


def test_reload_handler_still_restarts_docker() -> None:
    """The gating must not have been 'fixed' by unhooking the Docker restart."""
    with _NFT_HANDLERS.open() as f:
        handlers = yaml.safe_load(f)
    reload_handler = next(h for h in handlers if h["name"] == _RELOAD)
    assert reload_handler["notify"] == "Restart Docker after nftables reload"
