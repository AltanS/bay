"""Tests for _rescue_interspersed_args — flag rescue from ctx.args."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bay_cli.commands.ops import _rescue_interspersed_args


def _make_ctx(args: list[str]) -> MagicMock:
    ctx = MagicMock()
    ctx.args = list(args)  # copy to avoid mutation surprises
    return ctx


# ── --rig flag ──────────────────────────────────────────────────────────


@patch("bay_cli.commands.ops.console")
def test_rig_rescued_from_ctx_args(mock_console: MagicMock) -> None:
    ctx = _make_ctx(["--rig"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert rig is True
    assert tags is None
    assert skip is False
    assert ctx.args == []
    mock_console.warning.assert_called_once()


@patch("bay_cli.commands.ops.console")
def test_rig_not_rescued_when_already_set(mock_console: MagicMock) -> None:
    ctx = _make_ctx(["--rig"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=True, tags=None)
    assert rig is True
    # --rig falls through to remaining since rig was already True
    assert ctx.args == ["--rig"]
    mock_console.warning.assert_not_called()


# ── --tags flag (space form) ────────────────────────────────────────────


@patch("bay_cli.commands.ops.console")
def test_tags_space_form(mock_console: MagicMock) -> None:
    ctx = _make_ctx(["--tags", "access_gateway"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert tags == "access_gateway"
    assert ctx.args == []


@patch("bay_cli.commands.ops.console")
def test_tags_short_form(mock_console: MagicMock) -> None:
    ctx = _make_ctx(["-t", "nftables"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert tags == "nftables"
    assert ctx.args == []


# ── --tags flag (equals form) ──────────────────────────────────────────


@patch("bay_cli.commands.ops.console")
def test_tags_equals_form(mock_console: MagicMock) -> None:
    ctx = _make_ctx(["--tags=access_gateway"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert tags == "access_gateway"
    assert ctx.args == []


# ── --skip-validate ────────────────────────────────────────────────────


@patch("bay_cli.commands.ops.console")
def test_skip_validate_rescued(mock_console: MagicMock) -> None:
    ctx = _make_ctx(["--skip-validate"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, skip_validate=False)
    assert skip is True
    assert ctx.args == []


# ── Mixed flags + passthrough args ─────────────────────────────────────


@patch("bay_cli.commands.ops.console")
def test_mixed_flags_with_ansible_passthrough(mock_console: MagicMock) -> None:
    """--rig and -l eu in ctx.args: rescue --rig, keep -l eu for ansible."""
    ctx = _make_ctx(["--rig", "-l", "eu"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert rig is True
    assert ctx.args == ["-l", "eu"]


@patch("bay_cli.commands.ops.console")
def test_tags_plus_ansible_args(mock_console: MagicMock) -> None:
    ctx = _make_ctx(["--tags", "access_gateway", "-l", "eu", "--check"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert tags == "access_gateway"
    assert ctx.args == ["-l", "eu", "--check"]


@patch("bay_cli.commands.ops.console")
def test_rig_and_tags_together(mock_console: MagicMock) -> None:
    ctx = _make_ctx(["--rig", "--tags=traefik", "-l", "eu"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert rig is True
    assert tags == "traefik"
    assert ctx.args == ["-l", "eu"]


# ── No-op cases ────────────────────────────────────────────────────────


@patch("bay_cli.commands.ops.console")
def test_empty_ctx_args(mock_console: MagicMock) -> None:
    ctx = _make_ctx([])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert rig is False
    assert tags is None
    assert ctx.args == []
    mock_console.warning.assert_not_called()


@patch("bay_cli.commands.ops.console")
def test_only_ansible_args_pass_through(mock_console: MagicMock) -> None:
    ctx = _make_ctx(["-l", "eu", "--check", "--diff"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert rig is False
    assert tags is None
    assert ctx.args == ["-l", "eu", "--check", "--diff"]
    mock_console.warning.assert_not_called()


# ── Tags not rescued when already set by Typer ─────────────────────────


@patch("bay_cli.commands.ops.console")
def test_tags_not_rescued_when_already_set(mock_console: MagicMock) -> None:
    """If Typer parsed --tags before env, don't double-set from ctx.args."""
    ctx = _make_ctx(["--tags", "stale"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags="already_set")
    assert tags == "already_set"
    # unrescued --tags falls through to remaining
    assert ctx.args == ["--tags", "stale"]


# ── Bare `--` separator ────────────────────────────────────────────────
#
# Click stops option parsing at the positional `env`, so the `--` separator
# itself lands in ctx.args and used to be forwarded to ansible-playbook, which
# rejects it ("unrecognized arguments: --") and exits 2. That broke the dry-run
# form printed in BOTH `provision --help` and `restore --help`; deploy only
# escaped it via a local filter list. Stripping it here fixes all three callers
# at the point they share.


@patch("bay_cli.commands.ops.console")
def test_bare_separator_dropped(mock_console: MagicMock) -> None:
    """The documented `provision production -- --check --diff` form."""
    ctx = _make_ctx(["--", "--check", "--diff"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert ctx.args == ["--check", "--diff"]
    assert rig is False and tags is None and skip is False
    mock_console.warning.assert_not_called()


@patch("bay_cli.commands.ops.console")
def test_separator_dropped_before_rescuable_flag(mock_console: MagicMock) -> None:
    """`--` must not shield a rescuable flag from being promoted."""
    ctx = _make_ctx(["--", "--tags", "nftables", "--check"])
    rig, tags, skip, _region = _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert tags == "nftables"
    assert ctx.args == ["--check"]


@patch("bay_cli.commands.ops.console")
def test_multiple_separators_all_dropped(mock_console: MagicMock) -> None:
    ctx = _make_ctx(["--", "--check", "--", "--diff"])
    _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert ctx.args == ["--check", "--diff"]


@patch("bay_cli.commands.ops.console")
def test_restore_documented_form(mock_console: MagicMock) -> None:
    """`restore production -- -e accessory=postgres -e confirm=yes` (ops.py docstring)."""
    ctx = _make_ctx(["--", "-e", "accessory=postgres", "-e", "confirm=yes"])
    _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert ctx.args == ["-e", "accessory=postgres", "-e", "confirm=yes"]


@patch("bay_cli.commands.ops.console")
def test_non_bare_double_dash_preserved(mock_console: MagicMock) -> None:
    """Only a BARE `--` is a separator — a value that merely contains it stays."""
    ctx = _make_ctx(["-e", "msg=--", "--check"])
    _rescue_interspersed_args(ctx, rig=False, tags=None)
    assert ctx.args == ["-e", "msg=--", "--check"]
