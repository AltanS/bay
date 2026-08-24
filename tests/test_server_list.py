"""`server list` must show hosts from a multi-region inventory (GH bay#29).

The command filtered "children-section entries" by excluding every group named
in a `[<parent>:children]` block. In a multi-region inventory those names —
eu / na / infra — are exactly the groups the *real* hosts live in, so the
filter dropped all of them and printed an empty Host/IP/Region table against a
fully populated inventory. Reported from a live onboarding session on a
3-host inventory.

The parser tags lines inside `[production:children]` with the group
`production:children`, so that suffix is the only thing that needs excluding.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bay_cli.cli import app
from bay_cli.console import output as console_output
from bay_cli.inventory import InventoryConfig

runner = CliRunner()

_MULTI_REGION = """\
[eu]
192.0.2.10

[na]
198.51.100.10

[infra]
203.0.113.20

[production:children]
eu
na
infra
"""

_SINGLE_HOST = """\
[production]
203.0.113.10
"""


def _patch_inventory(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    from bay_cli.commands import server as server_mod

    def mock_get_inventory(requested_env: str = "production") -> tuple[InventoryConfig, Path]:
        inv = InventoryConfig()
        inv.load(root / "hosts" / requested_env)
        return inv, root

    monkeypatch.setattr(server_mod, "_get_inventory", mock_get_inventory)


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> Path:
    inv_path = tmp_path / "hosts" / "production"
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text(content)
    _patch_inventory(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_console_state():
    console_output.set_json_mode(False)
    console_output._message_buffer.clear()
    yield
    console_output.set_json_mode(False)
    console_output._message_buffer.clear()


def test_multi_region_hosts_are_listed(tmp_path, monkeypatch):
    """The reported bug: 3 active hosts rendered as an empty table."""
    _setup(tmp_path, monkeypatch, _MULTI_REGION)
    result = runner.invoke(app, ["server", "list"])
    assert result.exit_code == 0, result.output
    for ip in ("192.0.2.10", "198.51.100.10", "203.0.113.20"):
        assert ip in result.output, f"{ip} missing from `server list` output"


def test_multi_region_hosts_carry_their_region(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, _MULTI_REGION)
    result = runner.invoke(app, ["--json", "server", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    servers = payload["data"]["servers"]
    assert len(servers) == 3
    assert {s["region"] for s in servers} == {"eu", "na", "infra"}


def test_children_pseudo_hosts_are_not_listed(tmp_path, monkeypatch):
    """The group names inside `[production:children]` are not hosts."""
    _setup(tmp_path, monkeypatch, _MULTI_REGION)
    result = runner.invoke(app, ["--json", "server", "list"])
    names = {s["name"] for s in json.loads(result.output)["data"]["servers"]}
    assert names.isdisjoint({"eu", "na", "infra"}), (
        f"child group names leaked into the host list: {names}"
    )


def test_single_host_inventory_still_lists(tmp_path, monkeypatch):
    """The single-region layout has no children block — must not regress."""
    _setup(tmp_path, monkeypatch, _SINGLE_HOST)
    result = runner.invoke(app, ["--json", "server", "list"])
    servers = json.loads(result.output)["data"]["servers"]
    assert [s["ip"] for s in servers] == ["203.0.113.10"]
