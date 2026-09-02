"""The CrowdSec apt source is replaced, not duplicated (audit F2).

Scoping the repository key with `signed-by=` changed the source LINE. apt
refuses to read a repository listed twice with different signing options:

    E: Conflicting values set for option Signed-By regarding source
       https://packagecloud.io/crowdsec/crowdsec/ubuntu/

and that error is not local to CrowdSec — it fails **every** apt operation on
the host from then on, including the ones later roles depend on. Any host
provisioned before the change already carries the old unsigned line in
/etc/apt/sources.list.d/crowdsec.list, so the new line cannot simply be added
beside it.

This pins the removal: it exists, it names the OLD line verbatim, and it runs
BEFORE the add.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_TASKS = (
    Path(__file__).resolve().parent.parent
    / "roles" / "crowdsec" / "tasks" / "main.yml"
)

_OLD_REPO = (
    "deb https://packagecloud.io/crowdsec/crowdsec/"
    "{{ ansible_distribution | lower }}/ {{ ansible_distribution_release }} main"
)
_NEW_REPO = (
    "deb [signed-by={{ crowdsec_gpg_keyring }}] "
    "https://packagecloud.io/crowdsec/crowdsec/"
    "{{ ansible_distribution | lower }}/ {{ ansible_distribution_release }} main"
)


def _repo_tasks() -> list[tuple[int, dict]]:
    tasks = yaml.safe_load(_TASKS.read_text())
    return [
        (i, t) for i, t in enumerate(tasks)
        if "ansible.builtin.apt_repository" in t
    ]


def test_the_old_unsigned_line_is_removed():
    absent = [
        t for _, t in _repo_tasks()
        if t["ansible.builtin.apt_repository"].get("state") == "absent"
    ]
    assert len(absent) == 1, "exactly one removal of the legacy source line"
    mod = absent[0]["ansible.builtin.apt_repository"]
    assert mod["repo"] == _OLD_REPO, (
        "apt_repository matches on the exact line; the legacy one must be "
        "named verbatim or the removal silently does nothing"
    )
    assert mod["filename"] == "crowdsec"
    # No `signed-by` in the line being removed — that is the whole point.
    assert "signed-by" not in mod["repo"]


def test_the_removal_runs_before_the_add():
    order = _repo_tasks()
    states = [t["ansible.builtin.apt_repository"].get("state") for _, t in order]
    assert states == ["absent", "present"], (
        "adding the signed line first leaves both in the file for the length "
        "of the play, and the very next apt task fails"
    )


def test_the_signed_line_is_the_one_added():
    present = [
        t for _, t in _repo_tasks()
        if t["ansible.builtin.apt_repository"].get("state") == "present"
    ]
    assert len(present) == 1
    mod = present[0]["ansible.builtin.apt_repository"]
    assert mod["repo"] == _NEW_REPO
    assert mod["filename"] == "crowdsec", "same file, so the lines cannot split"


def test_both_tasks_honour_the_enable_flag():
    for _, task in _repo_tasks():
        assert task["when"] == "crowdsec_enabled | bool"
