"""`bin/bay admin-shell` must use the consumer's configured admin account.

It hard-coded the legacy-argo account until v1.5.1. A new consumer following
`example/group_vars`, where the account is `bay-admin`, got an SSH session to
an account that does not exist — and sshd rejects an unknown user in a way
that makes a correct password look wrong, so the symptom points nowhere near
the cause.
"""

from __future__ import annotations

import textwrap

import pytest

from bay_cli.commands.ops import _read_admin_user


def _consumer(tmp_path, body: str | None):
    gv = tmp_path / "group_vars" / "all"
    gv.mkdir(parents=True)
    if body is not None:
        (gv / "main.yml").write_text(textwrap.dedent(body))
    return tmp_path


def test_reads_the_configured_admin_user(tmp_path):
    root = _consumer(tmp_path, """
        ---
        stack_name: demo
        admin_user: bay-admin
        """)
    assert _read_admin_user(root) == "bay-admin"


def test_existing_consumers_keep_their_account(tmp_path):
    """Every consumer predating the rename pins it explicitly."""  # legacy-argo: account name below
    root = _consumer(tmp_path, """
        ---
        stack_name: demo
        admin_user: argo-admin  # legacy-argo: pre-1.0 account on existing hosts
        """)
    assert _read_admin_user(root) == "argo-admin"  # legacy-argo: pre-1.0 account


def test_arbitrary_account_is_honoured(tmp_path):
    root = _consumer(tmp_path, """
        ---
        admin_user: ops
        """)
    assert _read_admin_user(root) == "ops"


@pytest.mark.parametrize(
    "body",
    [
        None,                          # no main.yml at all
        "---\nstack_name: demo\n",     # admin_user simply absent
        "---\nadmin_user:\n",          # present but null
        "---\n[]\n",                   # not a mapping
    ],
)
def test_falls_back_to_the_legacy_account_not_the_example_one(tmp_path, body):
    """The fallback must not silently move an existing fleet's account.

    `bay-admin` is what `example/` ships, but defaulting to it would point
    every unconfigured consumer at an account their hosts do not have.
    """
    root = _consumer(tmp_path, body)
    assert _read_admin_user(root) == "argo-admin"  # legacy-argo: pre-1.0 account
