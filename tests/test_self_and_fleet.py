"""`bay self` (version, update) and `bay fleet` (init, ls)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bay_cli import cli
from bay_cli.commands import self_cmd
from bay_cli.fleet import FLEET_FILE, load_fleet_file

runner = CliRunner()

_GIT = ["git", "-c", "user.email=t@example.test", "-c", "user.name=t"]


def _git(path: Path, *args: str) -> str:
    out = subprocess.run([*_GIT, "-C", str(path), *args], check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BAY_FLEET", raising=False)
    monkeypatch.delenv("BAY_FLEET_NAME", raising=False)
    return home


# ── bay fleet ───────────────────────────────────────────────────────────────


def test_fleet_init_writes_a_valid_minimal_fleet_file(home: Path) -> None:
    result = runner.invoke(cli.app, ["fleet", "init", "prod"])
    assert result.exit_code == 0, result.output
    path = home / ".config" / "bay" / "fleets" / "prod"
    assert (path / ".git").exists()
    assert load_fleet_file(path)["name"] == "prod"
    # It tells the operator the three ways to pick the fleet.
    assert "BAY_FLEET_NAME=prod" in result.output
    assert "BAY_FLEET=" in result.output
    assert "--fleet" in result.output
    # The per-fleet cache directory is never committed.
    assert ".bay-cache/" in (path / ".gitignore").read_text().splitlines()


def test_fleet_init_refuses_an_existing_fleet_and_a_bad_name(home: Path) -> None:
    assert runner.invoke(cli.app, ["fleet", "init", "prod"]).exit_code == 0
    again = runner.invoke(cli.app, ["fleet", "init", "prod"])
    assert again.exit_code != 0
    assert "already exists" in str(again.exception)

    bad = runner.invoke(cli.app, ["fleet", "init", "Not A Name"])
    assert bad.exit_code != 0
    assert "not a fleet name" in str(bad.exception)
    assert not (home / ".config" / "bay" / "fleets" / "Not A Name").exists()


def test_fleet_init_from_clones_the_repo(home: Path, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet")
    (source / FLEET_FILE).write_text('name = "cloned"\n')
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "fleet")

    result = runner.invoke(cli.app, ["fleet", "init", "cloned", "--from", str(source)])
    assert result.exit_code == 0, result.output
    cloned = home / ".config" / "bay" / "fleets" / "cloned"
    assert (cloned / FLEET_FILE).read_text() == 'name = "cloned"\n'


def test_fleet_ls_lists_fleets_and_says_when_there_are_none(home: Path) -> None:
    empty = runner.invoke(cli.app, ["fleet", "ls"])
    assert empty.exit_code == 0
    assert "No fleets" in empty.output

    runner.invoke(cli.app, ["fleet", "init", "alpha"])
    runner.invoke(cli.app, ["fleet", "init", "beta"])
    (home / ".config" / "bay" / "fleets" / "stray").mkdir()
    listed = runner.invoke(cli.app, ["fleet", "ls"])
    assert listed.exit_code == 0
    assert "alpha" in listed.output
    assert "beta" in listed.output
    assert f"no {FLEET_FILE}" in listed.output


# ── bay self ────────────────────────────────────────────────────────────────


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    """A framework checkout cloned from an origin that has tags v1.0.0 and v1.1.0."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet")
    for tag in ("v1.0.0", "v1.1.0"):
        (origin / "version.yml").write_text(f'bay_version: "{tag[1:]}"\n')
        _git(origin, "add", "-A")
        _git(origin, "commit", "--quiet", "-m", tag)
        _git(origin, "tag", tag)
    root = tmp_path / "framework"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(root)], check=True)
    _git(root, "checkout", "--quiet", "v1.0.0")

    steps: list[str] = []
    monkeypatch.setattr(self_cmd, "package_root", lambda: root)
    monkeypatch.setattr(self_cmd.ansible, "sync_deps", lambda path: steps.append(f"deps {path.name}"))
    monkeypatch.setattr(self_cmd, "_install_tool", lambda path: steps.append(f"install {path.name}"))
    return root, steps


def test_self_version_prints_tag_and_checkout(checkout: tuple[Path, list[str]]) -> None:
    root, _ = checkout
    result = runner.invoke(cli.app, ["self", "version"])
    assert result.exit_code == 0, result.output
    assert "bay v1.0.0" in result.output
    assert f"checkout: {root}" in result.output


def test_self_update_moves_to_the_newest_tag_and_installs_again(checkout: tuple[Path, list[str]]) -> None:
    root, steps = checkout
    result = runner.invoke(cli.app, ["self", "update"])
    assert result.exit_code == 0, result.output
    assert "v1.0.0 -> v1.1.0" in result.output
    assert _git(root, "describe", "--tags", "--exact-match") == "v1.1.0"
    assert steps == ["deps framework", "install framework"]


def test_self_update_to_a_tag_can_go_back(checkout: tuple[Path, list[str]]) -> None:
    root, _ = checkout
    assert runner.invoke(cli.app, ["self", "update"]).exit_code == 0
    result = runner.invoke(cli.app, ["self", "update", "--to", "v1.0.0"])
    assert result.exit_code == 0, result.output
    assert "v1.1.0 -> v1.0.0" in result.output


def test_self_update_unknown_tag_changes_nothing(checkout: tuple[Path, list[str]]) -> None:
    root, steps = checkout
    result = runner.invoke(cli.app, ["self", "update", "--to", "v9.9.9"])
    assert result.exit_code != 0
    assert "no tag v9.9.9" in str(result.exception)
    assert _git(root, "describe", "--tags", "--exact-match") == "v1.0.0"
    assert steps == []


def test_self_update_refuses_a_checkout_with_edits(checkout: tuple[Path, list[str]]) -> None:
    root, steps = checkout
    (root / "version.yml").write_text("edited\n")
    result = runner.invoke(cli.app, ["self", "update"])
    assert result.exit_code != 0
    assert "uncommitted changes" in str(result.exception)
    assert (root / "version.yml").read_text() == "edited\n"
    assert steps == []


def test_self_update_when_already_current_does_nothing(checkout: tuple[Path, list[str]]) -> None:
    _, steps = checkout
    result = runner.invoke(cli.app, ["self", "update", "--to", "v1.0.0"])
    assert result.exit_code == 0
    assert "Already at v1.0.0" in result.output
    assert steps == []


# ── the fleet line ──────────────────────────────────────────────────────────

_DEMO_FLEET = (
    'name = "demo"\ndefault_box = "box-1"\ndefault_domain = "example.com"\n'
    'primary_env = "production"\n\n[boxes.box-1]\nenv = "production"\n'
)


def _leaf_paths() -> set[str]:
    import click
    import typer.main

    def walk(cmd: click.Command, prefix: tuple[str, ...]) -> list[str]:
        if isinstance(cmd, click.Group):
            out: list[str] = []
            for name, sub in sorted(cmd.commands.items()):
                out += walk(sub, (*prefix, name))
            return out
        return [" ".join(prefix)]

    return set(walk(typer.main.get_command(cli.app), ()))


def _first_stderr_line(args: list[str], cwd: Path) -> tuple[str, int]:
    import os

    old = Path.cwd()
    os.chdir(cwd)
    try:
        result = runner.invoke(cli.app, args)
    finally:
        os.chdir(old)
    lines = result.stderr.splitlines()
    return (lines[0] if lines else ""), result.exit_code


def test_mutating_verbs_print_fleet_line_on_stderr(home: Path, tmp_path: Path) -> None:
    from bay_cli import fleet_line

    # Every command is classified, so a new verb cannot skip the line.
    paths = _leaf_paths()
    unclassified = paths - fleet_line.MUTATING_VERBS - fleet_line.QUIET_VERBS
    assert not unclassified, f"add these to MUTATING_VERBS or QUIET_VERBS: {sorted(unclassified)}"
    assert not fleet_line.MUTATING_VERBS & fleet_line.QUIET_VERBS
    wrapped = {
        path
        for path, info in fleet_line.registered(cli.app)
        if getattr(info.callback, fleet_line.MARK, False)
    }
    assert wrapped == paths & fleet_line.MUTATING_VERBS

    fleet = tmp_path / "demo-fleet"
    fleet.mkdir()
    (fleet / FLEET_FILE).write_text(_DEMO_FLEET)
    _git(fleet, "init", "-q")
    _git(fleet, "add", "-A")
    _git(fleet, "commit", "-q", "-m", "fleet")
    outside = tmp_path / "outside"
    outside.mkdir()
    want = f"fleet: demo ({fleet.resolve()})"
    g = ["--fleet", str(fleet)]
    # The verbs the spec names that exist today. Each stops early (no box,
    # no project), but only after the line.
    runs = {
        "plan": [*g, "plan", "production", "--json"],
        "up": [*g, "up", "production", "--json"],
        "approve": [*g, "approve", "0" * 12, "--reason", "x"],
        "rollback": [*g, "rollback", "--project", "nope", "--json"],
        "compile": [*g, "compile", "--out", str(tmp_path / "compiled")],
        "import": ["import", "--fleet", str(outside), "--out", str(fleet), "--name", "demo"],
        "init": [*g, "init", "--json"],
        "deploy": [*g, "deploy", "production"],
        "provision": [*g, "provision", "production"],
    }
    for verb, args in runs.items():
        first, _ = _first_stderr_line(args, outside)
        assert first == want, (verb, first)
    # --json keeps stdout for the one document: the line is on stderr only.
    import os

    old = Path.cwd()
    os.chdir(outside)
    try:
        result = runner.invoke(cli.app, runs["plan"])
    finally:
        os.chdir(old)
    assert result.stderr.startswith(want)
    assert "fleet:" not in result.stdout
    # Standing in the fleet directory without --fleet: the line names it too.
    first, _ = _first_stderr_line(["plan", "production", "--json"], fleet)
    assert first == want
    assert set(runs) <= fleet_line.MUTATING_VERBS

    # Pure readers do not print it.
    readers = {
        "show": [*g, "show", "nope", "--json"],
        "status": [*g, "status", "--json"],
        "toml validate": ["toml", "validate", str(outside / "bay.toml")],
        "self version": ["self", "version"],
        "fleet ls": ["fleet", "ls"],
    }
    for verb, args in readers.items():
        assert verb in fleet_line.QUIET_VERBS
        first, _ = _first_stderr_line(args, outside)
        assert not first.startswith("fleet:"), (verb, first)
