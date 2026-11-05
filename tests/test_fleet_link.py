"""`bay fleet link [PATH]`, the link display of `bay fleet ls`, and the not-found hint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bay_cli import cli
from bay_cli.context import fleet_dir_for_name
from bay_cli.errors import BayError
from bay_cli.fleet import FLEET_FILE

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BAY_FLEET", raising=False)
    monkeypatch.delenv("BAY_FLEET_NAME", raising=False)
    return home


def _checkout(tmp_path: Path, name: str = "acme", folder: str = "checkout") -> Path:
    root = tmp_path / folder
    root.mkdir()
    (root / FLEET_FILE).write_text(f'name = "{name}"\n')
    return root


def _entry(home: Path, name: str = "acme") -> Path:
    return home / ".config" / "bay" / "fleets" / name


def test_link_makes_a_symlink_to_the_resolved_path(home, tmp_path):
    checkout = _checkout(tmp_path)
    result = runner.invoke(cli.app, ["fleet", "link", str(checkout)])
    assert result.exit_code == 0, result.output
    entry = _entry(home)
    assert entry.is_symlink()
    assert entry.resolve() == checkout.resolve()
    assert "BAY_FLEET_NAME=acme" in result.output


def test_link_defaults_to_the_working_directory(home, tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    monkeypatch.chdir(checkout)
    result = runner.invoke(cli.app, ["fleet", "link"])
    assert result.exit_code == 0, result.output
    assert _entry(home).resolve() == checkout.resolve()


def test_link_resolves_a_symlinked_path(home, tmp_path):
    checkout = _checkout(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(checkout)
    assert runner.invoke(cli.app, ["fleet", "link", str(alias)]).exit_code == 0
    assert _entry(home).readlink() == checkout.resolve()


def test_link_twice_says_so_and_exits_zero(home, tmp_path):
    checkout = _checkout(tmp_path)
    assert runner.invoke(cli.app, ["fleet", "link", str(checkout)]).exit_code == 0
    again = runner.invoke(cli.app, ["fleet", "link", str(checkout)])
    assert again.exit_code == 0, again.output
    assert "already linked" in again.output
    assert _entry(home).resolve() == checkout.resolve()


def test_link_json(home, tmp_path):
    checkout = _checkout(tmp_path)
    result = runner.invoke(cli.app, ["--json", "fleet", "link", str(checkout)])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["data"]["name"] == "acme"
    assert doc["data"]["already_linked"] is False


def test_link_refuses_a_link_that_points_elsewhere(home, tmp_path):
    other = _checkout(tmp_path, folder="other")
    mine = _checkout(tmp_path, folder="mine")
    assert runner.invoke(cli.app, ["fleet", "link", str(other)]).exit_code == 0
    refused = runner.invoke(cli.app, ["fleet", "link", str(mine)])
    assert refused.exit_code != 0
    assert isinstance(refused.exception, BayError)
    assert "already linked" in str(refused.exception)
    assert _entry(home).resolve() == other.resolve()


def test_link_refuses_a_real_directory(home, tmp_path):
    existing = _entry(home)
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("keep")
    refused = runner.invoke(cli.app, ["fleet", "link", str(_checkout(tmp_path))])
    assert refused.exit_code != 0
    assert "not a link" in str(refused.exception)
    assert not existing.is_symlink()
    assert (existing / "keep.txt").read_text() == "keep"


def test_link_refuses_a_broken_link(home, tmp_path):
    entry = _entry(home)
    entry.parent.mkdir(parents=True)
    entry.symlink_to(tmp_path / "gone")
    refused = runner.invoke(cli.app, ["fleet", "link", str(_checkout(tmp_path))])
    assert refused.exit_code != 0
    assert entry.readlink() == tmp_path / "gone"


def test_link_needs_a_directory_with_a_fleet_file(home, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    no_file = runner.invoke(cli.app, ["fleet", "link", str(empty)])
    assert no_file.exit_code != 0
    assert FLEET_FILE in str(no_file.exception)

    missing = runner.invoke(cli.app, ["fleet", "link", str(tmp_path / "nope")])
    assert missing.exit_code != 0
    assert "not a directory" in str(missing.exception)
    assert not _entry(home).parent.exists()


def test_link_needs_a_string_name(home, tmp_path):
    for body in ("", "name = 3\n", 'name = ""\n', "name = [\n"):
        root = tmp_path / f"f{abs(hash(body))}"
        root.mkdir()
        (root / FLEET_FILE).write_text(body)
        result = runner.invoke(cli.app, ["fleet", "link", str(root)])
        assert result.exit_code != 0, body
        assert isinstance(result.exception, BayError), body
    assert not _entry(home).parent.exists()


def test_link_refuses_a_name_that_is_not_a_folder_name(home, tmp_path):
    root = _checkout(tmp_path, name="../evil")
    result = runner.invoke(cli.app, ["fleet", "link", str(root)])
    assert result.exit_code != 0
    assert "not a fleet name" in str(result.exception)
    assert not (home / ".config" / "bay" / "evil").exists()


def test_a_linked_fleet_is_found_by_name(home, tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    runner.invoke(cli.app, ["fleet", "link", str(checkout)])
    assert fleet_dir_for_name("acme").resolve() == checkout.resolve()


def test_ls_shows_the_real_path_of_a_link(home, tmp_path):
    checkout = _checkout(tmp_path)
    runner.invoke(cli.app, ["fleet", "link", str(checkout)])
    runner.invoke(cli.app, ["fleet", "init", "plain"])
    listed = runner.invoke(cli.app, ["fleet", "ls"], env={"COLUMNS": "500"})
    assert listed.exit_code == 0, listed.output
    assert f"-> {checkout.resolve()}" in listed.output
    plain_line = next(ln for ln in listed.output.splitlines() if "plain" in ln)
    assert "->" not in plain_line


def test_ls_json_has_real_path_for_links_only(home, tmp_path):
    checkout = _checkout(tmp_path)
    runner.invoke(cli.app, ["fleet", "link", str(checkout)])
    runner.invoke(cli.app, ["fleet", "init", "plain"])
    result = runner.invoke(cli.app, ["--json", "fleet", "ls"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    rows = doc["data"]["fleets"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["acme"]["real_path"] == str(checkout.resolve())
    assert by_name["plain"]["real_path"] is None


def test_not_found_hint_names_the_three_ways(home):
    with pytest.raises(BayError) as caught:
        fleet_dir_for_name("ghost")
    hint = caught.value.hint or ""
    assert "bay fleet init ghost --from <url>" in hint
    assert "bay fleet link <path>" in hint
    assert "--fleet <path>" in hint
