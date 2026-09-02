"""Shipping the reconciler is gated, and the env digest did not move (M111 P8).

Two round-trip costs came out of `reconcile.yml`:

  * one `slurp` per container to hash its env file — now one batched read,
    with the digest still computed controller-side from the same bytes, and
  * a recursive `copy` of `src/bay_reconcile` every deploy, `__pycache__`
    included — now a tar shipped only when a marker file disagrees.

The digest assertion is the important one. `config_hash` feeds the planner's
NoOp decision, so a formula change would recreate every container in the fleet
once, on every consumer, on upgrade.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RECONCILE = (
    _REPO_ROOT / "roles" / "container_lifecycle" / "tasks" / "reconcile.yml"
)


def _tasks() -> list[dict]:
    out: list[dict] = []

    def walk(items):
        for task in items:
            out.append(task)
            for key in ("block", "rescue", "always"):
                if key in task:
                    walk(task[key])

    walk(yaml.safe_load(_RECONCILE.read_text()))
    return out


def _by_name(name: str) -> dict:
    for task in _tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task named {name!r}")


def test_no_env_file_is_slurped_any_more():
    text = _RECONCILE.read_text()
    assert "ansible.builtin.slurp" not in text
    read = _by_name("Read the rendered env files in one pass")
    assert "loop" not in read, "one read for the whole fleet, not one per spec"
    assert read["changed_when"] is False
    # A check-mode-skipped command registers rc 0 and an empty stdout, which
    # would take `from_json` down with it.
    assert read["check_mode"] is False
    assert read["no_log"] is True
    assert "argv" in read["ansible.builtin.command"]


def test_the_env_digest_formula_is_unchanged():
    """Any change here recreates every container on every consumer, once."""
    build = _by_name("Build reconcile bundle entries")
    digest = build["vars"]["_env_digest"]
    assert "b64decode" in digest
    assert "hash('sha256')" in digest
    assert build["vars"]["_env_b64"].strip().startswith("{{ _env_map[")


def test_the_ship_step_is_gated_on_the_marker():
    ship = _by_name("Ship the bay_reconcile package")
    when = " ".join(ship["when"].split())
    assert "_reconcile_marker_on_host.stdout" in when
    assert "_reconcile_pkg_marker" in when
    assert "!=" in when
    # The unpack lives inside that gate, so a matching marker transfers nothing.
    names = [t.get("name") for t in ship["block"]]
    assert "Unpack bay_reconcile on the host" in names
    assert "Record the shipped reconciler marker" in names
    # The marker is written last, so a failed ship retries next deploy.
    assert names[-1] == "Record the shipped reconciler marker"


def test_the_marker_carries_the_framework_version_and_a_content_digest():
    marker = _by_name("Compute the reconciler package marker")
    expr = marker["ansible.builtin.set_fact"]["_reconcile_pkg_marker"]
    assert "bay_version" in expr
    assert "hash('sha256')" in expr
    assert "bay_reconcile/*" in marker["vars"]["_pkg_files"]

    written = _by_name("Record the shipped reconciler marker")
    assert "_reconcile_pkg_marker" in written["ansible.builtin.copy"]["content"]
    read = _by_name("Read the reconciler marker on the host")
    assert read["failed_when"] is False
    assert read["check_mode"] is False


def test_the_package_ships_as_a_tar_without_pycache():
    pack = _by_name("Pack bay_reconcile without __pycache__")
    argv = pack["ansible.builtin.command"]["argv"]
    assert argv[0] == "tar"
    assert "--exclude=__pycache__" in argv
    assert "--exclude=*.pyc" in argv
    # Packed on the controller, and only once per marker.
    assert pack["delegate_to"] == "localhost"
    assert pack["become"] is False
    assert pack["ansible.builtin.command"]["creates"]

    unpack = _by_name("Unpack bay_reconcile on the host")
    assert unpack["ansible.builtin.unarchive"]["src"] == "{{ _pkg_tar }}"
    assert "recurse" not in str(unpack)
    assert "ansible.builtin.copy" not in str(unpack)


def test_the_recursive_directory_copy_is_gone():
    text = _RECONCILE.read_text()
    assert "src/bay_reconcile\"" not in text
    assert "playbook_dir }}/src/bay_reconcile\n" not in text


def test_the_controller_side_pack_runs_in_check_mode():
    """`--check` must not fail on a tar the dry run itself refused to build.

    The pack is a `command` with `creates:`, so check mode SKIPS it, and the
    `unarchive` below then reports a missing source. Both controller-side
    steps write only to the operator's own cache directory — nothing on a
    managed host — so both run for real in check mode, and neither counts as
    a change to the host being reported on.
    """
    for name in (
        "Ensure the controller-side package cache exists",
        "Pack bay_reconcile without __pycache__",
    ):
        task = _by_name(name)
        assert task["check_mode"] is False, f"{name} is skipped by --check"
        assert task["changed_when"] is False, f"{name} reports a false change"
        assert task["delegate_to"] == "localhost"


def test_a_missing_env_file_is_named_instead_of_tracebacking():
    """`no_log` swallows the traceback, so the paths come back as data.

    The read carries every secret in the fleet, so it must stay `no_log`; an
    env file deploy_stack never rendered used to surface as a bare MODULE
    FAILURE with nothing to go on.
    """
    read = _by_name("Read the rendered env files in one pass")
    body = read["ansible.builtin.command"]["argv"][2]
    assert "missing" in body and "except OSError" in body, (
        "the reader must collect unreadable paths rather than raise"
    )

    check = _by_name("Fail with the list of env files that could not be read")
    assert "no_log" not in check, "the whole point is that this one prints"
    assert "_reconcile_env_missing" in check["ansible.builtin.assert"]["that"]
    fail_msg = check["ansible.builtin.assert"]["fail_msg"]
    assert "_reconcile_env_missing | join" in fail_msg, "name the paths"
    assert "files" not in check["vars"]["_reconcile_env_missing"], (
        "the message must read the path list, never the file contents"
    )

    # The consumer of the read follows the new shape.
    build = _by_name("Build reconcile bundle entries")
    assert "'files'" in build["vars"]["_env_map"]
