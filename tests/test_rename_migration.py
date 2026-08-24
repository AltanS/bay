"""Structural guards for the rename_migration role.

The role can only be exercised end-to-end against a host provisioned before
the Argo -> Bay rename, so what is pinned here is everything that can drift  # legacy-argo: describes the Argo->Bay migration by name
in the repo:

- it is wired into *both* playbooks (outbound_monitor and common own argo-*  # legacy-argo: pre-1.0 unit prefix
  artifacts and live in provision.yml only — a deploy-only cleanup would never
  reach them; that is the GH#33 shape of bug)
- its explicit unit list still covers every unit template the framework
  renders, so adding a unit without adding its pre-1.0 name to the migration
  list fails here rather than leaving an orphan on every host
- every destructive task is guarded, which is what makes a second run a no-op
- every task is named and every old-name literal is tagged `legacy-argo:`
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
ROLE = REPO / "roles" / "rename_migration"
_UNIT_SUFFIXES = (".service", ".timer", ".path")


def _defaults() -> dict:
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())


def _tasks() -> list[dict]:
    return yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())


def test_rename_migration_role_exists() -> None:
    assert (ROLE / "tasks" / "main.yml").is_file()
    assert (ROLE / "defaults" / "main.yml").is_file()


def test_rename_migration_runs_from_both_playbooks() -> None:
    for playbook in ("deploy.yml", "provision.yml"):
        text = (REPO / playbook).read_text()
        assert "rename_migration" in text, f"{playbook} does not import the role"
        assert "always" in text.split("rename_migration", 1)[1][:200], (
            f"{playbook} must tag the role `always` so a targeted --tags run "
            "cannot bring the new artifacts up alongside the old ones"
        )


def test_rename_migration_unit_list_covers_every_unit_template() -> None:
    """Every rendered unit template has a pre-1.0 name in the removal list."""
    listed = set(_defaults()["rename_migration_units"])
    missing = []
    for template in (REPO / "roles").rglob("templates/bay-*.j2"):
        unit = template.name[: -len(".j2")]
        if not unit.endswith(_UNIT_SUFFIXES):
            continue
        legacy = "argo-" + unit[len("bay-") :]  # legacy-argo: pre-1.0 unit basename
        if legacy not in listed:
            missing.append(legacy)
    assert not missing, (
        f"unit templates with no rename_migration entry: {sorted(missing)} — "
        "a host provisioned before the rename would keep them forever"
    )


# Scripts born after the rename — there is no pre-1.0 file to remove.
_POST_RENAME_SCRIPTS = {
    "bay-readlog",
    "bay-journal",
    "bay-systemctl-ro",
    "bay-docker-ro",
}


def test_rename_migration_covers_every_rendered_script() -> None:
    defaults = _defaults()
    covered = " ".join(
        defaults["rename_migration_files"]
        + [defaults["rename_migration_watchdog_script"]]
    )
    missing = []
    for template in (REPO / "roles").rglob("templates/bay-*.j2"):
        name = template.name[: -len(".j2")]
        if name.endswith(_UNIT_SUFFIXES) or name in _POST_RENAME_SCRIPTS:
            continue
        stem = name[len("bay-") :].removesuffix(".sh")
        if f"argo-{stem}" not in covered:  # legacy-argo: pre-1.0 script name
            missing.append(name)
    assert not missing, f"script templates with no rename_migration entry: {missing}"


def test_rename_migration_moves_the_old_config_dir() -> None:
    """The config dir is moved with its tree intact, not re-created.

    Note the alert-override file inside it is *not* what this preserves:
    alert_policy re-renders it from group_vars later in the same run. What the
    move guarantees is ownership, modes and any file the framework does not
    declare.
    """
    moves = {m["src"]: m["dest"] for m in _defaults()["rename_migration_moves"]}
    assert moves.get("/etc/argo") == "/etc/bay", (  # legacy-argo: pre-1.0 config dir
        "the pre-1.0 config dir must be moved, not left behind"
    )


def test_every_rename_migration_task_is_named() -> None:
    unnamed = [t for t in _tasks() if not t.get("name")]
    assert not unnamed, f"{len(unnamed)} unnamed task(s) in rename_migration"


def test_destructive_rename_migration_tasks_are_guarded() -> None:
    """Idempotence by construction: nothing destructive runs unconditionally."""
    destructive = {
        "ansible.builtin.file",
        "ansible.builtin.shell",
        "ansible.builtin.cron",
        "ansible.builtin.systemd_service",
    }
    unguarded = []
    for task in _tasks():
        module = next((k for k in task if k in destructive), None)
        if module is None:
            continue
        if module == "ansible.builtin.systemd_service" and task[module].get(
            "daemon_reload"
        ):
            # daemon-reload is not destructive, but it is guarded anyway.
            pass
        if not (task.get("when") or task.get("loop")):
            unguarded.append(task["name"])
    assert not unguarded, f"unguarded destructive task(s): {unguarded}"


def test_rename_migration_runs_as_root() -> None:
    """deploy.yml's main play runs as app_user; /etc and systemd need root."""
    local_only = {"ansible.builtin.set_fact", "ansible.builtin.debug"}
    for task in _tasks():
        if any(module in task for module in local_only):
            continue
        assert task.get("become") is True, f"{task['name']}: missing become"
        assert task.get("become_user") == "root", f"{task['name']}: missing become_user"


def test_rename_migration_old_names_are_tagged() -> None:
    """The sweep guard's mechanism, asserted at the source: every pre-1.0
    literal in this role carries the `legacy-argo:` tag."""
    for path in sorted(ROLE.rglob("*.yml")):
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            if "argo" in line.lower() and "legacy-argo" not in line:
                raise AssertionError(
                    f"{path.relative_to(REPO)}:{line_no} names a pre-1.0 artifact "
                    f"without a `legacy-argo:` tag: {line.strip()}"
                )


def _render(expr: str, **ctx) -> object:
    """Evaluate one of the role's Jinja expressions the way Ansible would."""
    import re as _re

    from jinja2 import Environment

    env = Environment()
    env.filters["regex_replace"] = lambda v, p, r: _re.sub(p, r, v)
    env.tests["match"] = lambda v, p: _re.match(p, v) is not None
    env.tests["search"] = lambda v, p: _re.search(p, v) is not None
    env.tests["failed"] = lambda v: bool((v or {}).get("failed"))
    return eval(env.from_string("{{ " + expr + " }}").render(**ctx))


def _fact(task_name: str, var: str) -> str:
    """The bare Jinja expression a set_fact task assigns to `var`."""
    task = {t["name"]: t for t in _tasks()}[task_name]
    return task["ansible.builtin.set_fact"][var].strip()[2:-2]


# A host that had a template instance FAIL before the rename keeps that
# instance resident with LOAD state "not-found": it is still listed, but any
# `systemctl stop` on it errors out. That is the v1.0.1 brick — the role is
# tagged `always`, so the error failed every later deploy *and* --check run.
_LIST_UNITS = [
    "argo-backup@pg.timer loaded active running backup",  # legacy-argo: fixture
    "● argo-build@whoami.service not-found failed failed",  # legacy-argo: fixture
    "argo-custom@x.service not-found failed failed",  # legacy-argo: fixture
    "argo-build@.service                        enabled",  # legacy-argo: fixture
    "bay-backup@pg.timer loaded active running backup",
]


def _split_discovered(lines: list[str]) -> tuple[list[str], list[str]]:
    """Run the role's discovery split over synthetic `list-units` output."""
    prefix = _defaults()["rename_migration_unit_prefix"]
    discovered = _render(
        _fact("Normalise the discovered pre-1.0 unit lines", "_rm_discovered"),
        _rm_loaded_units={"stdout_lines": lines},
        _rm_enabled_units={"stdout_lines": []},
        rename_migration_unit_prefix=prefix,
    )
    units = _render(
        _fact("Select the pre-1.0 units this role manages", "_rm_units"),
        _rm_discovered=discovered,
    )
    orphans = _render(
        _fact("Select the pre-1.0 units left resident by a failed instance",
              "_rm_orphan_units"),
        _rm_discovered=discovered,
    )
    return units, orphans


def _managed(candidates: list[str], guard: str) -> list[str]:
    units = _defaults()["rename_migration_units"]
    return [
        c for c in candidates
        if _render(guard, item=c, rename_migration_units=units)
    ]


def test_rename_migration_splits_not_found_units_out_of_the_stop_list() -> None:
    """A not-found unit must never reach `systemd_service: state=stopped`."""
    units, orphans = _split_discovered(_LIST_UNITS)

    assert "argo-build@whoami.service" not in units, (  # legacy-argo: fixture
        "a resident not-found instance handed to systemctl stop errors with "
        "'Could not find the requested service' and bricks every later run"
    )
    assert "argo-build@whoami.service" in orphans  # legacy-argo: fixture
    assert "argo-backup@pg.timer" in units  # legacy-argo: fixture
    assert "argo-backup@pg.timer" not in orphans  # legacy-argo: fixture
    assert "argo-build@.service" not in units + orphans, (  # legacy-argo: fixture
        "a bare template unit would make `systemctl stop` fail"
    )
    assert not any(u.startswith("bay-") for u in units + orphans)


def test_rename_migration_orphan_clear_is_gated_to_framework_units() -> None:
    """reset-failed obeys the same membership gate as stop/disable."""
    tasks = {t["name"]: t for t in _tasks()}
    stop_guard = tasks["Stop and disable pre-1.0 systemd units"]["when"]
    reset = tasks["Clear pre-1.0 systemd units left resident by a failed instance"]
    assert reset["when"] == stop_guard, (
        "the orphan clear must use the same collapse-to-template membership "
        "check, or it becomes the glob deletion this role refuses to be"
    )

    units, orphans = _split_discovered(_LIST_UNITS)
    assert _managed(orphans, reset["when"]) == [
        "argo-build@whoami.service",  # legacy-argo: fixture
    ], "an operator-created not-found unit must never be touched"
    assert _managed(units, stop_guard) == [
        "argo-backup@pg.timer",  # legacy-argo: fixture
    ]


def test_rename_migration_reset_failed_is_check_mode_safe() -> None:
    """`command` has no check mode, so the clear is skipped, not simulated."""
    reset = {t["name"]: t for t in _tasks()}[
        "Clear pre-1.0 systemd units left resident by a failed instance"
    ]
    assert "check_mode" not in reset, (
        "check_mode: false would make --check mutate the host"
    )
    assert "reset-failed" in reset["ansible.builtin.command"]["cmd"]
    report = {t["name"]: t for t in _tasks()}[
        "Report the pre-1.0 units cleared with reset-failed"
    ]
    assert "would clear" in report["ansible.builtin.debug"]["msg"], (
        "--check must still report what a real run would clear"
    )


def test_rename_migration_stop_tolerates_a_vanished_unit() -> None:
    """Belt and braces: discovery and stop are two round trips."""
    stop = {t["name"]: t for t in _tasks()}["Stop and disable pre-1.0 systemd units"]
    expr = " and ".join(f"({clause})" for clause in stop["failed_when"])

    vanished = {
        "failed": True,
        "msg": (
            "Could not find the requested service "
            "argo-build@whoami.service: host"  # legacy-argo: fixture
        ),
    }
    other = {"failed": True, "msg": "Job for unit failed with result 'timeout'."}

    assert _render(expr, _rm_stopped=vanished) is False, (
        "a unit that vanished between discovery and stop must not fail the run"
    )
    assert _render(expr, _rm_stopped=other) is True, "genuine errors stay fatal"
    assert _render(expr, _rm_stopped={"changed": True}) is False


def test_rename_migration_selects_only_framework_units() -> None:
    """The discovery glob is wide; the explicit list is what actually decides.

    A unit an operator created by hand under the old prefix is discovered and
    then dropped, and a bare template unit is never handed to `systemctl stop`
    (which would fail with "missing the instance name").
    """
    tasks = {t["name"]: t for t in _tasks()}
    guard = tasks["Stop and disable pre-1.0 systemd units"]["when"]

    systemctl_output = [
        "argo-backup@pg.timer loaded active waiting backup",  # legacy-argo: fixture
        "argo-build@.service                        enabled",  # legacy-argo: fixture
        "argo-disk-alert.timer loaded active waiting probe",  # legacy-argo: fixture
        "argo-operators-own-thing.service loaded active running",  # legacy-argo: fixture
        "bay-backup@pg.timer loaded active waiting backup",
    ]
    candidates, orphans = _split_discovered(systemctl_output)
    assert not orphans, "nothing in this fixture is resident-but-not-found"
    assert "argo-build@.service" not in candidates, (  # legacy-argo: bare template
        "a bare template unit would make `systemctl stop` fail"
    )
    assert "bay-backup@pg.timer" not in candidates, "new-name units are not touched"

    kept = _managed(candidates, guard)
    assert kept == [
        "argo-backup@pg.timer",  # legacy-argo: fixture
        "argo-disk-alert.timer",  # legacy-argo: fixture
    ], f"operator-created units must be dropped, got {kept}"
