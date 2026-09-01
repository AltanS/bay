"""`bay setup --defaults` must work from a script, not just from a terminal.

`_scaffold_project` checked `sys.stdin.isatty()` *before* it looked at
`--defaults`, so a scripted `bay setup --defaults` silently copied
`example/` instead: the tree gained group_vars/eu, group_vars/na,
group_vars/staging and network.yml, and the following `bay validate`
failed. The flag was documented and unreachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from bay_cli.catalog import _package_framework_root
from bay_cli.commands.framework import _build_result_from_defaults, _SetupFlags
from bay_cli.errors import BayError

ROOT = _package_framework_root()
# Split so the literal never sits in the tree: scripts/leak-scan.sh flags any
# 32+ char high-entropy blob, and a fake key is shaped exactly like a real one.
KEY = "ssh-ed25519 " "AAAAC3NzaC1lZDI1NTE5" "AAAAI" "TESTKEY" " tester@example.com"


def _flags(**kwargs) -> _SetupFlags:
    base = dict(
        name=None, server_ip=None, domain=None, gateway=None,
        headscale_domain=None, services=None, letsencrypt_email=None,
        multi_region=False, vpn_peer_ips=None, ssh_key=[KEY], ssh_key_file=[],
    )
    base.update(kwargs)
    return _SetupFlags(**base)


# ── The result builder ───────────────────────────────────────────────────


def test_flags_override_the_built_in_defaults(tmp_path: Path) -> None:
    result = _build_result_from_defaults(
        _flags(server_ip="203.0.113.10", domain="example.test"), tmp_path / "myproj"
    )
    assert result.server_ip == "203.0.113.10"
    assert result.domain_base == "example.test"
    # Derived values follow the override — not the built-in example.com.
    assert result.headscale_domain == "hs.example.test"
    assert result.letsencrypt_email == "admin@example.test"
    assert result.project_name == "myproj"
    assert [k.public_key for k in result.ssh_keys] == [KEY]


def test_defaults_alone_still_work(tmp_path: Path) -> None:
    result = _build_result_from_defaults(_flags(), tmp_path / "myproj")
    assert result.domain_base == "example.com"
    assert result.selected_services == ["gatus"]


def test_explicit_services_and_gateway(tmp_path: Path) -> None:
    result = _build_result_from_defaults(
        _flags(domain="example.test", services="plausible", gateway="none"), tmp_path / "p"
    )
    assert result.access_gateway == "none"
    assert "postgres" in result.selected_services  # dependency auto-added


def test_unknown_service_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BayError, match="Unknown service"):
        _build_result_from_defaults(_flags(services="nope"), tmp_path / "p")


def test_keyless_defaults_are_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(BayError, match="No SSH public key"):
        _build_result_from_defaults(_flags(ssh_key=[]), tmp_path / "p")


# ── The command, with a non-tty stdin (what CliRunner gives us) ──────────


def test_setup_defaults_scaffolds_instead_of_copying_examples(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    (project / ".bay").symlink_to(ROOT)
    monkeypatch.chdir(project)

    from bay_cli.cli import app

    result = CliRunner().invoke(
        app,
        ["setup", "--defaults", "--server-ip", "203.0.113.10",
         "--domain", "example.test", "--ssh-key", KEY],
    )

    assert result.exit_code == 0, result.output

    # The example-copy tell-tales must be absent...
    assert not (project / "group_vars" / "eu").exists()
    assert not (project / "group_vars" / "staging").exists()
    assert not (project / "group_vars" / "production" / "network.yml").exists()

    # ...and the flags must have reached the rendered files.
    assert "203.0.113.10" in (project / "hosts" / "production").read_text()
    domains = yaml.safe_load((project / "group_vars/production/domains.yml").read_text())
    assert domains["domain_base"] == "example.test"
    users = yaml.safe_load((project / "group_vars/all/users.yml").read_text())
    assert users["users"][0]["keys"] == [KEY]
    assert (project / "files" / "gatus" / "config.yaml").is_file()
