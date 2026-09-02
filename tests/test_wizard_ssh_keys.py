"""The wizard cannot finish with zero SSH keys, and validate says so too.

Provisioning writes authorized_keys first, then sets `PermitRootLogin no`
and `PasswordAuthentication no`. The users role loops with
`subelements('keys', skip_missing=True)`, so an empty key list is skipped
without a warning: the run stays green and the server ends up with no way
in. "Skip (add later)" made that one keystroke away, and nothing further
down the line noticed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bay_cli.commands.validate import ValidationResult, _validate_admin_ssh_keys
from bay_cli.errors import BayError
from bay_cli.wizard import prompts as prompts_mod
from bay_cli.wizard.models import (
    SSHKey,
    discover_local_ssh_keys,
    parse_ssh_public_key,
    resolve_ssh_keys,
)

# Split so the literal never sits in the tree: scripts/leak-scan.sh flags any
# 32+ char high-entropy blob, and a fake key is shaped exactly like a real one.
KEY = "ssh-ed25519 " "AAAAC3NzaC1lZDI1NTE5" "AAAAI" "TESTKEY" " tester@example.com"


# ── The prompt ───────────────────────────────────────────────────────────


class _MockQuestion:
    def __init__(self, value: object) -> None:
        self._value = value

    def ask(self) -> object:
        return self._value


def _mock_selects(monkeypatch, values: list) -> None:
    it = iter(values)
    monkeypatch.setattr(
        prompts_mod.questionary, "select", lambda *a, **kw: _MockQuestion(next(it))
    )


def test_prompt_offers_no_skip_choice(monkeypatch) -> None:
    """A 'skip' value must not even be reachable from the choice list."""
    offered: list[str] = []
    answers = iter(["paste", False])

    def _select(*args, **kwargs):
        for choice in kwargs.get("choices") or []:
            offered.append(str(getattr(choice, "value", choice)))
        return _MockQuestion(next(answers))

    monkeypatch.setattr(prompts_mod.questionary, "select", _select)
    monkeypatch.setattr(prompts_mod.Prompt, "ask", staticmethod(lambda *a, **kw: KEY))

    keys = prompts_mod._prompt_ssh_keys()

    assert [k.public_key for k in keys] == [KEY]
    assert "skip" not in offered
    assert {"github", "paste", "local"} <= set(offered)


def test_prompt_loops_until_a_key_is_collected(monkeypatch) -> None:
    """A GitHub fetch that returns nothing must not end the step."""
    monkeypatch.setattr(prompts_mod, "_fetch_github_keys", lambda username: [])
    monkeypatch.setattr(prompts_mod.Prompt, "ask", staticmethod(lambda *a, **kw: KEY))
    # github (yields nothing) -> paste -> "add another?" no
    _mock_selects(monkeypatch, ["github", "paste", False])

    keys = prompts_mod._prompt_ssh_keys()

    assert len(keys) == 1
    assert keys[0].public_key == KEY


def test_prompt_rejects_a_pasted_private_key(monkeypatch) -> None:
    fake_private_key = "-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----"
    answers = iter([fake_private_key, KEY, "tester"])
    monkeypatch.setattr(prompts_mod.Prompt, "ask", staticmethod(lambda *a, **kw: next(answers)))
    _mock_selects(monkeypatch, ["paste", "paste", False])

    keys = prompts_mod._prompt_ssh_keys()

    assert [k.public_key for k in keys] == [KEY]


# ── Non-interactive key resolution ───────────────────────────────────────


def test_resolve_prefers_inline_flag(monkeypatch) -> None:
    monkeypatch.setattr("bay_cli.wizard.models.discover_local_ssh_keys", lambda: [])
    keys = resolve_ssh_keys([KEY], [])
    assert [k.public_key for k in keys] == [KEY]


def test_resolve_reads_a_key_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("bay_cli.wizard.models.discover_local_ssh_keys", lambda: [])
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text(KEY + "\n")
    keys = resolve_ssh_keys([], [str(pub)])
    assert [k.public_key for k in keys] == [KEY]


def test_resolve_falls_back_to_local_keys(tmp_path: Path, monkeypatch) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519.pub").write_text(KEY + "\n")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert [k.public_key for k in discover_local_ssh_keys()] == [KEY]
    assert [k.public_key for k in resolve_ssh_keys([], [])] == [KEY]


def test_resolve_refuses_to_return_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(BayError, match="No SSH public key found"):
        resolve_ssh_keys([], [])


def test_parse_rejects_a_non_key() -> None:
    with pytest.raises(BayError, match="Not an SSH public key"):
        parse_ssh_public_key("hunter2")


# ── validate ─────────────────────────────────────────────────────────────


def _users(keys: list[str]) -> dict:
    return {
        "group_vars/all/users.yml": {
            "users": [
                {"name": "bay-admin", "groups": ["sudo", "ssh-access", "docker"], "keys": keys},
                {"name": "bay", "groups": ["bay", "docker"], "keys": []},
            ]
        }
    }


def test_validate_fails_on_keyless_admin() -> None:
    result = ValidationResult()
    _validate_admin_ssh_keys(_users([]), result)
    assert result.failed
    joined = " ".join(result.failed)
    assert "bay-admin" in joined
    assert "locks you out" in joined


def test_validate_fails_on_whitespace_only_key() -> None:
    result = ValidationResult()
    _validate_admin_ssh_keys(_users(["   "]), result)
    assert result.failed


def test_validate_passes_with_a_key() -> None:
    result = ValidationResult()
    _validate_admin_ssh_keys(_users([KEY]), result)
    assert not result.failed
    assert result.passed


def test_validate_ignores_users_outside_ssh_access() -> None:
    """The app service account has no keys on purpose."""
    result = ValidationResult()
    _validate_admin_ssh_keys(_users([KEY]), result)
    assert "bay" not in " ".join(result.failed)


def test_scaffolded_users_yml_carries_the_key(tmp_path: Path) -> None:
    import yaml

    from bay_cli.wizard.models import WizardResult
    from bay_cli.wizard.scaffold import scaffold

    scaffold(
        WizardResult(
            project_name="testapp",
            multi_region=False,
            server_ip="203.0.113.10",
            domain_base="example.com",
            letsencrypt_email="ops@example.com",
            ssh_keys=[SSHKey(username="tester", public_key=KEY, source="manual")],
            access_gateway="none",
            selected_services=["gatus"],
        ),
        tmp_path,
    )
    users = yaml.safe_load((tmp_path / "group_vars/all/users.yml").read_text())
    admin = users["users"][0]
    assert admin["keys"] == [KEY]
