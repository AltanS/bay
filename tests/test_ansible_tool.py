"""The Ansible commands the CLI calls directly resolve inside its own install.

``uv tool install`` puts only ``bay`` on PATH. A bare ``ansible-vault`` then
fails with "not found", and ``bay plan`` blocks on every project that has a
secret (the missing-secret check fails closed).
"""

from __future__ import annotations

import re
import sysconfig
from pathlib import Path

from bay_cli import ansible

SRC = Path(__file__).resolve().parents[1] / "src" / "bay_cli"


def test_tool_finds_ansible_vault_without_path(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    found = Path(ansible.tool("ansible-vault"))
    assert found.is_absolute() and found.is_file()
    assert found.parent == Path(sysconfig.get_path("scripts"))


def test_tool_falls_back_to_the_bare_name(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    assert ansible.tool("no-such-ansible-tool") == "no-such-ansible-tool"


def test_no_bare_ansible_vault_argv_in_the_cli() -> None:
    bare = re.compile(r'^\s*\[?\s*"ansible-vault",', re.MULTILINE)
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if bare.search(path.read_text())
    ]
    assert offenders == [], f"use ansible.tool('ansible-vault') in: {offenders}"
