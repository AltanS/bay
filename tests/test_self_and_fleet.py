"""`bay self` (version, update) and `bay fleet` (init, ls)."""

from __future__ import annotations

import re
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


def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "t")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.test")


def test_fleet_init_commits(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new fleet has its first commit, so `bay init` and `bay adopt` accept it at once."""
    _identity(monkeypatch)
    result = runner.invoke(cli.app, ["fleet", "init", "prod"])
    assert result.exit_code == 0, result.output
    path = home / ".config" / "bay" / "fleets" / "prod"
    assert _git(path, "log", "--format=%s") == "bay: fleet init"
    assert set(_git(path, "ls-tree", "-r", "--name-only", "HEAD").split()) == {
        FLEET_FILE,
        ".gitignore",
    }
    assert _git(path, "status", "--porcelain") == ""


def test_fleet_init_commits_warns_when_the_commit_fails(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No git identity: the fleet stays, and the warning names the commit line to run."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.delenv(f"GIT_{var}_NAME", raising=False)
        monkeypatch.delenv(f"GIT_{var}_EMAIL", raising=False)
    (tmp_path / "gitconfig").write_text("[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # `useConfigOnly` makes git refuse to guess a name and email from the machine.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    result = runner.invoke(cli.app, ["fleet", "init", "prod"])
    assert result.exit_code == 0, result.output
    path = home / ".config" / "bay" / "fleets" / "prod"
    assert (path / FLEET_FILE).is_file() and (path / ".git").exists()
    flat = "".join(result.output.split())  # the console wraps long lines
    assert "firstcommitfailed" in flat
    assert f'git -C {path} commit -m "bay: fleet init"'.replace(" ", "") in flat


def test_fleet_init_gitignores_vault_pass(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _identity(monkeypatch)
    assert runner.invoke(cli.app, ["fleet", "init", "prod"]).exit_code == 0
    path = home / ".config" / "bay" / "fleets" / "prod"
    (path / ".vault_pass").write_text("not-a-real-password\n")
    ignored = subprocess.run(
        ["git", "-C", str(path), "check-ignore", ".vault_pass"], capture_output=True, text=True
    )
    assert ignored.returncode == 0 and ignored.stdout.strip() == ".vault_pass"
    # plans are committed, so plans/ is not ignored
    (path / "plans").mkdir()
    (path / "plans" / "x.json").write_text("{}")
    kept = subprocess.run(
        ["git", "-C", str(path), "check-ignore", "plans/x.json"], capture_output=True, text=True
    )
    assert kept.returncode == 1


def test_fleet_init_from_warns_when_the_clone_does_not_ignore_vault_pass(
    home: Path, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet")
    (source / FLEET_FILE).write_text('name = "cloned"\n')
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "fleet")
    result = runner.invoke(cli.app, ["fleet", "init", "cloned", "--from", str(source)])
    assert result.exit_code == 0, result.output
    assert ".vault_pass is not in the .gitignore" in " ".join(result.output.split())
    # and `--from` makes no commit of its own
    cloned = home / ".config" / "bay" / "fleets" / "cloned"
    assert _git(cloned, "log", "--format=%s") == "fleet"


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


# ── bay doctor (M117/08) ────────────────────────────────────────────────────


def _doctor_fleet(path: Path, *, fmt: int | None = 2) -> Path:
    path.mkdir(parents=True)
    head = f"format = {fmt}\n" if fmt is not None else ""
    (path / FLEET_FILE).write_text(_DEMO_FLEET.replace('name = "demo"\n', f'name = "demo"\n{head}'))
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "fleet")
    return path


def _doctor(args: list[str], cwd: Path) -> tuple[dict, int]:
    import json
    import os

    old = Path.cwd()
    os.chdir(cwd)
    try:
        result = runner.invoke(cli.app, [*args, "doctor", "--json", "--no-remote"])
    finally:
        os.chdir(old)
    return json.loads(result.stdout), result.exit_code


def _line(doc: dict, check: str) -> dict:
    return next(line for line in doc["lines"] if line["check"] == check)


def test_doctor_reports_fleet_pick_reason(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = _doctor_fleet(tmp_path / "real-fleet")
    link = tmp_path / "linked"
    link.symlink_to(fleet)
    outside = tmp_path / "outside"
    outside.mkdir()

    doc, _ = _doctor(["--fleet", str(link)], outside)
    line = _line(doc, "Fleet")
    assert line["status"] == "ok"
    assert f"demo ({fleet.resolve()}), picked by --fleet {link}" in line["detail"]
    assert f"{link} resolves to {fleet.resolve()}" in line["detail"]
    assert doc["fleet"]["root"] == str(fleet.resolve())

    monkeypatch.setenv("BAY_FLEET", str(fleet))
    doc, _ = _doctor([], outside)
    assert f"picked by BAY_FLEET={fleet}" in _line(doc, "Fleet")["detail"]
    monkeypatch.delenv("BAY_FLEET")

    named = home / ".config" / "bay" / "fleets" / "demo"
    named.parent.mkdir(parents=True)
    named.symlink_to(fleet)
    monkeypatch.setenv("BAY_FLEET_NAME", "demo")
    doc, _ = _doctor([], outside)
    assert "picked by BAY_FLEET_NAME=demo" in _line(doc, "Fleet")["detail"]
    monkeypatch.delenv("BAY_FLEET_NAME")

    app = tmp_path / "app"
    app.mkdir()
    (app / "bay.toml").write_text('name = "shop"\nfleet = "demo"\n')
    doc, _ = _doctor([], app)
    assert f'picked by fleet = "demo" in {app / "bay.toml"}' in _line(doc, "Fleet")["detail"]

    doc, _ = _doctor([], fleet)
    assert "picked by the fleet directory you stand in" in _line(doc, "Fleet")["detail"]

    doc, code = _doctor([], outside)
    assert code == 1 and _line(doc, "Fleet")["status"] == "fail"
    assert "no fleet selected" in _line(doc, "Fleet")["detail"]


def test_doctor_reports_format_and_cli_version(home: Path, tmp_path: Path) -> None:
    from bay_cli.context import package_root

    fleet = _doctor_fleet(tmp_path / "two")
    doc, _ = _doctor(["--fleet", str(fleet)], tmp_path)
    assert _line(doc, "Fleet format") == {
        "check": "Fleet format", "status": "ok", "detail": "format 2"
    }
    cli_line = _line(doc, "CLI")
    assert cli_line["detail"].startswith("bay ") and cli_line["detail"].endswith(
        f" at {package_root()}"
    )

    old = _doctor_fleet(tmp_path / "one", fmt=None)
    doc, _ = _doctor(["--fleet", str(old)], tmp_path)
    line = _line(doc, "Fleet format")
    assert line["status"] == "warn" and line["detail"].startswith("format 1:")

    future = _doctor_fleet(tmp_path / "nine", fmt=9)
    doc, code = _doctor(["--fleet", str(future)], tmp_path)
    assert _line(doc, "Fleet format")["status"] == "fail" and code == 1
    assert "bay self update" in _line(doc, "Fleet format")["detail"]


def test_doctor_reports_fleet_behind_origin(home: Path, tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    fleet = _doctor_fleet(tmp_path / "fleet")
    _git(fleet, "remote", "add", "origin", str(origin))
    _git(fleet, "push", "-q", "-u", "origin", "main")

    doc, _ = _doctor(["--fleet", str(fleet)], tmp_path)
    assert _line(doc, "Fleet clone") == {
        "check": "Fleet clone", "status": "ok", "detail": "not behind its remote"
    }

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    (other / "README").write_text("newer\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "newer")
    _git(other, "push", "-q", "origin", "main")

    doc, code = _doctor(["--fleet", str(fleet)], tmp_path)
    line = _line(doc, "Fleet clone")
    assert line["status"] == "fail" and "behind its remote; run git pull" in line["detail"]
    assert code == 1 and doc["ok"] is False


def test_doctor_reports_v1_leftovers(home: Path, tmp_path: Path) -> None:
    fleet = _doctor_fleet(tmp_path / "fleet")
    doc, _ = _doctor(["--fleet", str(fleet)], tmp_path)
    assert _line(doc, "v1 leftovers")["status"] == "ok"

    (fleet / "bin").mkdir()
    (fleet / "bin" / "bay").write_text("#!/bin/sh\n")
    (fleet / ".bay-version").write_text("v1.0.0\n")
    doc, _ = _doctor(["--fleet", str(fleet)], tmp_path)
    line = _line(doc, "v1 leftovers")
    assert line["status"] == "warn"
    assert line["detail"].startswith("bin, .bay-version in the fleet")
    assert "bay='bin/bay'" in line["detail"]


def test_doctor_reports_untracked_plans(home: Path, tmp_path: Path) -> None:
    fleet = _doctor_fleet(tmp_path / "fleet")
    doc, _ = _doctor(["--fleet", str(fleet)], tmp_path)
    assert _line(doc, "Plans")["status"] == "ok"

    (fleet / "plans").mkdir()
    (fleet / "plans" / "0123456789ab.json").write_text("{}\n")
    (fleet / "plans" / "ba9876543210.json").write_text("{}\n")
    doc, _ = _doctor(["--fleet", str(fleet)], tmp_path)
    line = _line(doc, "Plans")
    assert line["status"] == "warn"
    assert line["detail"].startswith("2 plan file(s) are not committed")
    assert "plans/0123456789ab.json" in line["detail"]

    _git(fleet, "add", "plans")
    _git(fleet, "commit", "-q", "-m", "plans")
    doc, _ = _doctor(["--fleet", str(fleet)], tmp_path)
    assert _line(doc, "Plans")["status"] == "ok"


def test_self_update_help_names_current_version() -> None:
    from bay_cli.context import package_root
    from bay_cli.paths import read_installed_version

    declared = read_installed_version(package_root())
    assert declared
    result = runner.invoke(cli.app, ["self", "update", "--help"])
    assert result.exit_code == 0, result.output
    assert f"bay self update --to v{declared}" in " ".join(result.output.split())
    # The help is computed from version.yml; no literal version stays in the source.
    assert not re.search(r"v\d+\.\d+\.\d+", Path(self_cmd.__file__).read_text())
