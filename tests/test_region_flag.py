"""Tests for --region flag wiring on deploy/provision/restore and admin-shell."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bay_cli.commands import ops
from bay_cli.context import Context
from bay_cli.errors import BayError

_CX = Context.for_fleet_root(Path("/tmp"), Path("/tmp/.bay"))


def test_region_extra_args_no_region_returns_base():
    result = ops._region_extra_args(_CX, None, "production", ["-e", "foo=bar"])
    assert result == ["-e", "foo=bar"]


def test_region_extra_args_unknown_region_raises(monkeypatch):
    monkeypatch.setattr(ops, "_resolve_target_host", lambda cx, region: None)
    with pytest.raises(BayError, match="Unknown region 'xx'"):
        ops._region_extra_args(_CX, "xx", "production", [])


def test_region_extra_args_resolves_and_prepends_limit(monkeypatch):
    monkeypatch.setattr(ops, "_resolve_target_host", lambda cx, region: "10.0.0.1")
    result = ops._region_extra_args(_CX, "eu", "production", ["-e", "x=y"])
    assert result == ["-l", "10.0.0.1", "-e", "x=y"]


def test_admin_shell_registered():
    assert callable(ops.admin_shell)


def test_region_flag_exposed_on_ops_commands():
    import inspect
    for cmd in (ops.deploy, ops.provision, ops.restore):
        sig = inspect.signature(cmd)
        assert "region" in sig.parameters, f"{cmd.__name__} missing --region"
