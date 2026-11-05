"""`bay vault set` must run ansible-vault for the fleet, from any folder.

ansible-vault finds the vault password through the fleet's settings. If the
command runs in the caller's folder with no fleet environment, it prompts for
a password and fails. `edit`, `view`, `encrypt` and `decrypt` were bound to the
fleet already. `set` built its own command and was not.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bay_cli import ansible, runner
from bay_cli.commands import vault
from bay_cli.context import Context
from helpers import typer_ctx


@pytest.fixture
def fleet(tmp_path):
    root = tmp_path / "myfleet"
    (root / "group_vars" / "production").mkdir(parents=True)
    (root / "group_vars" / "production" / "secrets.yml").write_text("ENCRYPTED\n")
    (root / ".vault_pass").write_text("not-a-real-password\n")
    yield root
    ansible.bind_fleet(None)


def _record_runs(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), **kwargs})
        # Behave like ansible-vault: decrypt writes plaintext, encrypt writes the target.
        if "--output" in cmd:
            out = Path(cmd[cmd.index("--output") + 1])
            action = cmd[cmd.index("ansible-vault") + 1]
            out.write_text("secrets: {}\n" if action == "decrypt" else "ENCRYPTED\n")

    monkeypatch.setattr(runner, "run", fake_run)
    return calls


def test_set_runs_ansible_vault_bound_to_the_fleet(fleet, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    calls = _record_runs(monkeypatch)

    cx = Context.resolve(fleet)
    vault.set_key(typer_ctx(cx), "production", "TOKEN", "s3cret")
    set_calls = [c for c in calls if "ansible-vault" in c["cmd"]]

    # The reference: what the bound verbs (view, edit, ...) run.
    calls.clear()
    ansible.vault_cmd("view", "x.yml", bay_dir=cx.framework_root)
    reference = calls[0]
    ref_prefix = reference["cmd"][: reference["cmd"].index("ansible-vault")]
    assert "--directory" in ref_prefix
    assert ref_prefix[ref_prefix.index("--directory") + 1] == str(fleet.resolve())

    actions = [c["cmd"][c["cmd"].index("ansible-vault") + 1] for c in set_calls]
    assert actions == ["decrypt", "encrypt"]
    for call in set_calls:
        prefix = call["cmd"][: call["cmd"].index("ansible-vault")]
        assert prefix == ref_prefix
        assert call["env"] == reference["env"]
        assert call["env"]["ANSIBLE_VAULT_PASSWORD_FILE"] == str(fleet.resolve() / ".vault_pass")
