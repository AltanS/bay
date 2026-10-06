"""Fleet discovery: `--fleet`, `BAY_FLEET`, the fleets dir, and the walk-up fallback.

`Context.resolve` is the one place that finds the project. The order is
`--fleet`, then `BAY_FLEET`, then `~/.config/bay/fleets/<name>` (only with
`BAY_FLEET_NAME` set), then (this
transition only, removed in S08) walking up from the working directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from bay_cli import cli
from bay_cli.context import (
    SOURCE_CWD,
    SOURCE_ENV,
    SOURCE_FLAG,
    SOURCE_FLEETS_DIR,
    SOURCE_WALK_UP,
    Context,
    context_from,
    context_or_cwd,
    package_root,
)
from bay_cli.errors import BayError


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty HOME with no fleet variables set, so the real machine never leaks in."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BAY_FLEET", raising=False)
    monkeypatch.delenv("BAY_FLEET_NAME", raising=False)
    return home


def _probe_app() -> typer.Typer:
    """The real root callback (`--fleet` and friends) with one probe command."""
    app = typer.Typer()
    app.callback()(cli.main)

    @app.command()
    def probe(ctx: typer.Context) -> None:
        cx = context_from(ctx)
        typer.echo(f"fleet={cx.fleet_root}")
        typer.echo(f"framework={cx.framework_root}")
        typer.echo(f"source={cx.source}")
        typer.echo(f"group_vars={cx.group_vars}")
        typer.echo(f"vault_pass={cx.vault_pass}")

    return app


def _fake_consumer(tmp_path: Path) -> Path:
    """A consumer repo with a `.bay/` framework clone (what walk-up looks for)."""
    root = tmp_path / "consumer"
    (root / ".bay" / ".git").mkdir(parents=True)
    (root / "sub" / "dir").mkdir(parents=True)
    return root


def _fleet_dir(tmp_path: Path, name: str = "fleet") -> Path:
    fleet = tmp_path / name
    fleet.mkdir()
    return fleet


def _lines(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def test_fleet_flag_overrides_discovery(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bay --fleet <dir> <cmd>` wins over BAY_FLEET and over walk-up."""
    consumer = _fake_consumer(tmp_path)
    flag_fleet = _fleet_dir(tmp_path, "flag-fleet")
    env_fleet = _fleet_dir(tmp_path, "env-fleet")
    monkeypatch.chdir(consumer / "sub" / "dir")
    monkeypatch.setenv("BAY_FLEET", str(env_fleet))

    result = CliRunner().invoke(_probe_app(), ["--fleet", str(flag_fleet), "probe"])

    assert result.exit_code == 0, result.output
    out = _lines(result.output)
    assert out["fleet"] == str(flag_fleet.resolve())
    assert out["source"] == SOURCE_FLAG
    assert out["group_vars"] == str(flag_fleet.resolve() / "group_vars")
    assert out["vault_pass"] == str(flag_fleet.resolve() / ".vault_pass")
    # A fleet found by path carries no .bay/ clone: the framework is this package's repo.
    assert out["framework"] == str(package_root())


def test_bay_fleet_env_var_resolves_to_the_given_dir(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = _fleet_dir(tmp_path)
    monkeypatch.setenv("BAY_FLEET", str(fleet))

    result = CliRunner().invoke(_probe_app(), ["probe"])

    assert result.exit_code == 0, result.output
    out = _lines(result.output)
    assert out["fleet"] == str(fleet.resolve())
    assert out["source"] == SOURCE_ENV


def test_missing_fleet_dir_is_an_error_not_a_fallback(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer = _fake_consumer(tmp_path)
    monkeypatch.chdir(consumer)
    with pytest.raises(BayError, match="fleet directory not found"):
        Context.resolve(tmp_path / "nope")
    monkeypatch.setenv("BAY_FLEET", str(tmp_path / "nope"))
    with pytest.raises(BayError, match="fleet directory not found"):
        Context.resolve(None)


def test_a_lone_fleet_is_not_auto_picked_over_walk_up(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One fleet dir and no BAY_FLEET_NAME: a consumer repo must still win.

    Otherwise the first fleet created on a machine would capture every
    `bin/bay` run inside fleet-a or fleet-b. S08 may reinstate it.
    """
    (home / ".config" / "bay" / "fleets" / "acme").mkdir(parents=True)
    consumer = _fake_consumer(tmp_path)
    monkeypatch.chdir(consumer / "sub")

    cx = Context.resolve(None)

    assert cx.source == SOURCE_WALK_UP
    assert cx.fleet_root == consumer


def test_bay_fleet_name_selects_a_fleet_over_walk_up(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    only = home / ".config" / "bay" / "fleets" / "acme"
    only.mkdir(parents=True)
    monkeypatch.chdir(_fake_consumer(tmp_path))
    monkeypatch.setenv("BAY_FLEET_NAME", "acme")

    cx = Context.resolve(None)

    assert cx.fleet_root == only.resolve()
    assert cx.source == SOURCE_FLEETS_DIR
    assert cx.framework_root == package_root()


def test_bay_fleet_name_picks_one_of_several(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = home / ".config" / "bay" / "fleets"
    (base / "acme").mkdir(parents=True)
    (base / "beta").mkdir()
    monkeypatch.setenv("BAY_FLEET_NAME", "beta")

    assert Context.resolve(None).fleet_root == (base / "beta").resolve()

    monkeypatch.setenv("BAY_FLEET_NAME", "gamma")
    with pytest.raises(BayError, match="fleet 'gamma' not found"):
        Context.resolve(None)


def test_fleets_without_a_name_do_not_guess(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several fleets and no name: step 3 is off, so walk-up decides (the transition)."""
    base = home / ".config" / "bay" / "fleets"
    (base / "acme").mkdir(parents=True)
    (base / "beta").mkdir()
    consumer = _fake_consumer(tmp_path)
    monkeypatch.chdir(consumer)

    cx = Context.resolve(None)

    assert cx.source == SOURCE_WALK_UP
    assert cx.fleet_root == consumer


def test_walk_up_fallback_still_works_from_a_consumer_tree(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transition fallback: every current consumer command keeps resolving unchanged."""
    consumer = _fake_consumer(tmp_path)
    monkeypatch.chdir(consumer / "sub" / "dir")

    result = CliRunner().invoke(_probe_app(), ["probe"])

    assert result.exit_code == 0, result.output
    out = _lines(result.output)
    assert out["fleet"] == str(consumer)
    assert out["framework"] == str(consumer / ".bay")
    assert out["source"] == SOURCE_WALK_UP


def test_walk_up_fails_outside_a_consumer_tree(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    with pytest.raises(BayError, match="bay not found"):
        Context.resolve(None)


def test_derived_paths_come_from_the_fleet_root(tmp_path: Path) -> None:
    cx = Context.for_fleet_root(tmp_path / "f", tmp_path / "fw")
    root = tmp_path / "f"
    assert cx.group_vars == root / "group_vars"
    assert cx.hosts_dir == root / "hosts"
    assert cx.vault_pass == root / ".vault_pass"
    assert cx.services_file == root / "group_vars" / "all" / "services.yml"
    assert cx.inventory("production") == root / "hosts" / "production"
    assert cx.secrets_file("eu") == root / "group_vars" / "eu" / "secrets.yml"
    assert cx.env_file("all", "main.yml") == cx.main_vars_file
    assert cx.framework_root == tmp_path / "fw"


def test_context_or_cwd_falls_back_to_cwd_but_never_over_an_explicit_fleet(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)

    cx = context_or_cwd(None)
    assert cx.fleet_root == here
    assert cx.source == SOURCE_CWD

    monkeypatch.setenv("BAY_FLEET", str(tmp_path / "nope"))
    with pytest.raises(BayError):
        context_or_cwd(None)
