"""Fleet discovery: `--fleet`, `BAY_FLEET`, the fleet a bay.toml names, `BAY_FLEET_NAME`.

`Context.resolve` is the one place that finds the fleet. The order is
`--fleet`, then `BAY_FLEET`, then the fleet named by the nearest bay.toml,
then `~/.config/bay/fleets/<name>` (only with `BAY_FLEET_NAME` set). With none
of these there is no fleet and the error lists the ways to pick one. Nothing
walks up to a framework clone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from bay_cli import cli
from bay_cli.context import (
    SOURCE_BAY_TOML,
    SOURCE_CWD,
    SOURCE_ENV,
    SOURCE_FLAG,
    SOURCE_FLEETS_DIR,
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


def _app_repo(tmp_path: Path, fleet: str | None = "acme") -> Path:
    """An app repo with a bay.toml (naming a fleet, unless ``fleet`` is None)."""
    root = tmp_path / "app"
    (root / "sub" / "dir").mkdir(parents=True)
    named = f'fleet = "{fleet}"\n' if fleet else ""
    (root / "bay.toml").write_text(f'name = "app"\n{named}')
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
    """`bay --fleet <dir> <cmd>` wins over BAY_FLEET and over a bay.toml."""
    app = _app_repo(tmp_path)
    flag_fleet = _fleet_dir(tmp_path, "flag-fleet")
    env_fleet = _fleet_dir(tmp_path, "env-fleet")
    monkeypatch.chdir(app / "sub" / "dir")
    monkeypatch.setenv("BAY_FLEET", str(env_fleet))

    result = CliRunner().invoke(_probe_app(), ["--fleet", str(flag_fleet), "probe"])

    assert result.exit_code == 0, result.output
    out = _lines(result.output)
    assert out["fleet"] == str(flag_fleet.resolve())
    assert out["source"] == SOURCE_FLAG
    assert out["group_vars"] == str(flag_fleet.resolve() / "group_vars")
    assert out["vault_pass"] == str(flag_fleet.resolve() / ".vault_pass")
    # The framework is the repo this package runs from.
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
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BayError, match="fleet directory not found"):
        Context.resolve(tmp_path / "nope")
    monkeypatch.setenv("BAY_FLEET", str(tmp_path / "nope"))
    with pytest.raises(BayError, match="fleet directory not found"):
        Context.resolve(None)


def test_a_lone_fleet_is_not_auto_picked(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One fleet dir and no name anywhere: still an error, never a guess."""
    (home / ".config" / "bay" / "fleets" / "acme").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(BayError, match="no fleet selected"):
        Context.resolve(None)


def test_the_error_lists_the_ways_to_pick_a_fleet(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(BayError) as caught:
        Context.resolve(None)

    hint = caught.value.hint or ""
    assert "--fleet <path>" in hint
    assert "BAY_FLEET=<path>" in hint
    assert "BAY_FLEET_NAME=<name>" in hint
    assert "bay fleet init" in hint


def test_bay_fleet_name_selects_a_fleet(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    only = home / ".config" / "bay" / "fleets" / "acme"
    only.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BAY_FLEET_NAME", "beta")

    assert Context.resolve(None).fleet_root == (base / "beta").resolve()

    monkeypatch.setenv("BAY_FLEET_NAME", "gamma")
    with pytest.raises(BayError, match="fleet 'gamma' not found"):
        Context.resolve(None)


def test_the_fleet_named_in_bay_toml_is_found_from_a_subdirectory(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = home / ".config" / "bay" / "fleets"
    (base / "acme").mkdir(parents=True)
    (base / "beta").mkdir()
    app = _app_repo(tmp_path, fleet="acme")
    monkeypatch.chdir(app / "sub" / "dir")

    result = CliRunner().invoke(_probe_app(), ["probe"])

    assert result.exit_code == 0, result.output
    out = _lines(result.output)
    assert out["fleet"] == str((base / "acme").resolve())
    assert out["source"] == SOURCE_BAY_TOML
    assert out["framework"] == str(package_root())


def test_bay_toml_beats_bay_fleet_name_and_loses_to_bay_fleet(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = home / ".config" / "bay" / "fleets"
    (base / "acme").mkdir(parents=True)
    (base / "beta").mkdir()
    env_fleet = _fleet_dir(tmp_path, "env-fleet")
    monkeypatch.chdir(_app_repo(tmp_path, fleet="acme"))
    monkeypatch.setenv("BAY_FLEET_NAME", "beta")

    assert Context.resolve(None).fleet_root == (base / "acme").resolve()

    monkeypatch.setenv("BAY_FLEET", str(env_fleet))
    assert Context.resolve(None).fleet_root == env_fleet.resolve()


def test_a_bay_toml_without_a_fleet_does_not_select_one(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_app_repo(tmp_path, fleet=None))
    with pytest.raises(BayError, match="no fleet selected"):
        Context.resolve(None)


def test_a_bay_toml_naming_a_missing_fleet_is_an_error(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_app_repo(tmp_path, fleet="ghost"))
    with pytest.raises(BayError, match="fleet 'ghost' not found"):
        Context.resolve(None)


def test_a_framework_clone_in_the_tree_is_not_walked_up_to(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old consumer layout (a `.bay/` clone above the working directory) finds nothing."""
    legacy = tmp_path / "legacy"
    (legacy / ".bay" / ".git").mkdir(parents=True)
    (legacy / "sub").mkdir()
    monkeypatch.chdir(legacy / "sub")

    with pytest.raises(BayError, match="no fleet selected"):
        Context.resolve(None)


def test_resolving_a_fleet_binds_ansible_to_it(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bay_cli import ansible

    fleet = _fleet_dir(tmp_path)
    assert "--directory" not in ansible._uv_run_cmd(tmp_path)

    Context.resolve(fleet)

    cmd = ansible._uv_run_cmd(tmp_path)
    assert cmd[cmd.index("--directory") + 1] == str(fleet.resolve())


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
