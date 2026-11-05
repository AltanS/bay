"""Fleet discovery step 5: the fleet you stand in, and the one fleet next to you.

5a: the working directory or a parent holds ``bay.fleet.toml``.
5b: only if 5a found nothing, exactly one fleet lives in a folder above you.
Steps 1 to 4 always win. The home directory, its parents, the filesystem
root and ``~/.config/bay/fleets`` are never searched in 5b.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from helpers import typer_ctx

from bay_cli import context as context_module
from bay_cli import fleet_line
from bay_cli.context import (
    SOURCE_BAY_TOML,
    SOURCE_CWD,
    SOURCE_ENV,
    SOURCE_FLAG,
    SOURCE_FLEET_ABOVE,
    SOURCE_FLEET_HERE,
    SOURCE_FLEETS_DIR,
    Context,
    GlobalOptions,
    context_or_cwd,
    fleets_root,
)
from bay_cli.errors import BayError


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BAY_FLEET", raising=False)
    monkeypatch.delenv("BAY_FLEET_NAME", raising=False)
    return home


def _fleet(parent: Path, name: str) -> Path:
    root = parent / name
    root.mkdir(parents=True)
    (root / "bay.fleet.toml").write_text(f'name = "{name}"\n')
    return root


def _empty(parent: Path, name: str = "empty") -> Path:
    path = parent / name
    path.mkdir(parents=True)
    return path


# ── 5a: you stand in a fleet ─────────────────────────────────────────


def test_5a_from_the_fleet_root(home, tmp_path, monkeypatch):
    fleet = _fleet(tmp_path / "work", "alpha")
    monkeypatch.chdir(fleet)
    cx = Context.resolve(None)
    assert cx.fleet_root == fleet.resolve()
    assert cx.source == SOURCE_FLEET_HERE


def test_5a_from_a_subfolder(home, tmp_path, monkeypatch):
    fleet = _fleet(tmp_path / "work", "alpha")
    deep = fleet / "projects" / "app"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    cx = Context.resolve(None)
    assert cx.fleet_root == fleet.resolve()
    assert cx.source == SOURCE_FLEET_HERE


def test_5a_wins_over_5b(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    here = _fleet(work, "here")
    _fleet(work, "other")
    monkeypatch.chdir(here)
    assert Context.resolve(None).fleet_root == here.resolve()


# ── 5b: exactly one fleet lives in a folder above you ────────────────


def test_5b_one_fleet_next_to_the_folder(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    fleet = _fleet(work, "alpha")
    monkeypatch.chdir(_empty(work))
    cx = Context.resolve(None)
    assert cx.fleet_root == fleet.resolve()
    assert cx.source == SOURCE_FLEET_ABOVE


def test_5b_looks_in_parents_nearest_first(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    far = _fleet(work, "far")
    near_parent = work / "group"
    near = _fleet(near_parent, "near")
    cwd = _empty(near_parent / "x", "y")
    monkeypatch.chdir(cwd)
    assert Context.resolve(None).fleet_root == near.resolve()
    assert far.is_dir()


def test_5b_goes_up_when_a_folder_has_none(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    fleet = _fleet(work, "alpha")
    monkeypatch.chdir(_empty(work / "a" / "b"))
    assert Context.resolve(None).fleet_root == fleet.resolve()


def test_5b_follows_a_symlinked_fleet(home, tmp_path, monkeypatch):
    real = _fleet(tmp_path / "elsewhere", "real-fleet")
    work = tmp_path / "work"
    work.mkdir()
    (work / "linked").symlink_to(real)
    monkeypatch.chdir(_empty(work))
    cx = Context.resolve(None)
    assert cx.fleet_root == real.resolve()
    assert cx.source == SOURCE_FLEET_ABOVE


def test_5b_counts_a_real_path_once(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    real = _fleet(work, "real")
    (work / "alias").symlink_to(real)
    monkeypatch.chdir(_empty(work))
    assert Context.resolve(None).fleet_root == real.resolve()


def test_5b_ignores_dot_folders_and_plain_files(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    fleet = _fleet(work, "alpha")
    _fleet(work, ".hidden")
    (work / "notes.txt").write_text("x")
    (work / "no-marker").mkdir()
    monkeypatch.chdir(_empty(work))
    assert Context.resolve(None).fleet_root == fleet.resolve()


def test_5b_two_fleets_is_an_error_that_lists_both(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    a = _fleet(work, "alpha")
    b = _fleet(work, "beta")
    monkeypatch.chdir(_empty(work))
    with pytest.raises(BayError) as caught:
        Context.resolve(None)
    message = str(caught.value)
    assert str(work) in message
    assert str(a) in message and str(b) in message
    assert "--fleet" in (caught.value.hint or "")
    assert "cd" in (caught.value.hint or "")


def test_5b_none_found_is_the_old_error(home, tmp_path, monkeypatch):
    monkeypatch.chdir(_empty(tmp_path / "work"))
    with pytest.raises(BayError, match="no fleet selected"):
        Context.resolve(None)


def test_5b_a_folder_that_cannot_be_listed_counts_as_none(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    good = _fleet(work, "good")
    sealed = work / "sealed"
    hidden = _fleet(sealed, "hidden")
    cwd = _empty(sealed, "cwd")
    sealed.chmod(stat.S_IWUSR | stat.S_IXUSR)  # enter, but not list
    try:
        if os.access(sealed, os.R_OK):
            pytest.skip("this user can list a folder without read permission")
        monkeypatch.chdir(cwd)
        assert Context.resolve(None).fleet_root == good.resolve()
    finally:
        sealed.chmod(stat.S_IRWXU)
    assert hidden.is_dir()


# ── 5b never searches these folders ──────────────────────────────────


@pytest.fixture
def visited(monkeypatch) -> list[Path]:
    seen: list[Path] = []
    real = context_module._fleets_inside

    def spy(folder: Path) -> list[Path]:
        seen.append(folder.resolve())
        return real(folder)

    monkeypatch.setattr(context_module, "_fleets_inside", spy)
    return seen


def test_5b_never_lists_home_its_parents_or_the_root(home, tmp_path, monkeypatch, visited):
    # A fleet in home, in a parent of home and in the root's reach would all be picked
    # if those folders were listed.
    _fleet(home, "in-home")
    _fleet(tmp_path, "in-parent-of-home")
    monkeypatch.chdir(_empty(home / "work"))
    with pytest.raises(BayError, match="no fleet selected"):
        Context.resolve(None)
    forbidden = {home.resolve(), tmp_path.resolve(), Path("/"), *tmp_path.resolve().parents}
    assert not forbidden & set(visited)
    assert (home / "work").resolve() in visited


def test_5b_from_the_home_directory_itself_finds_nothing(home, tmp_path, monkeypatch, visited):
    _fleet(home, "in-home")
    monkeypatch.chdir(home)
    with pytest.raises(BayError, match="no fleet selected"):
        Context.resolve(None)
    assert visited == []


def test_5b_never_lists_the_fleets_folder(home, tmp_path, monkeypatch, visited):
    base = fleets_root()
    _fleet(base, "acme")
    monkeypatch.chdir(base)
    with pytest.raises(BayError, match="no fleet selected"):
        Context.resolve(None)
    assert base.resolve() not in visited


def test_5a_still_works_inside_a_registered_fleet(home, tmp_path, monkeypatch):
    acme = _fleet(fleets_root(), "acme")
    monkeypatch.chdir(acme)
    cx = Context.resolve(None)
    assert cx.fleet_root == acme.resolve()
    assert cx.source == SOURCE_FLEET_HERE


# ── Steps 1 to 4 win over step 5 ─────────────────────────────────────


@pytest.fixture
def standing_in_fleet(home, tmp_path, monkeypatch):
    fleet = _fleet(tmp_path / "work", "here")
    monkeypatch.chdir(fleet)
    return fleet


def test_flag_beats_step_5(standing_in_fleet, tmp_path):
    other = _fleet(tmp_path / "x", "other")
    cx = Context.resolve(other)
    assert cx.fleet_root == other.resolve()
    assert cx.source == SOURCE_FLAG


def test_env_beats_step_5(standing_in_fleet, tmp_path, monkeypatch):
    other = _fleet(tmp_path / "x", "other")
    monkeypatch.setenv("BAY_FLEET", str(other))
    cx = Context.resolve(None)
    assert cx.fleet_root == other.resolve()
    assert cx.source == SOURCE_ENV


def test_bay_toml_beats_step_5(home, tmp_path, monkeypatch):
    registered = _fleet(fleets_root(), "reg")
    work = tmp_path / "work"
    _fleet(work, "neighbour")
    app = _empty(work, "app")
    (app / "bay.toml").write_text('name = "app"\nfleet = "reg"\n')
    monkeypatch.chdir(app)
    cx = Context.resolve(None)
    assert cx.fleet_root == registered.resolve()
    assert cx.source == SOURCE_BAY_TOML


def test_fleet_name_env_beats_step_5(standing_in_fleet, home, monkeypatch):
    registered = _fleet(fleets_root(), "reg")
    monkeypatch.setenv("BAY_FLEET_NAME", "reg")
    cx = Context.resolve(None)
    assert cx.fleet_root == registered.resolve()
    assert cx.source == SOURCE_FLEETS_DIR


def test_unregistered_bay_toml_fleet_does_not_fall_through(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    _fleet(work, "neighbour")
    app = _empty(work, "app")
    (app / "bay.toml").write_text('name = "app"\nfleet = "ghost"\n')
    monkeypatch.chdir(app)
    with pytest.raises(BayError, match="fleet 'ghost' not found"):
        Context.resolve(None)


def test_unregistered_bay_toml_fleet_does_not_fall_through_inside_a_fleet(
    standing_in_fleet,
):
    (standing_in_fleet / "bay.toml").write_text('name = "app"\nfleet = "ghost"\n')
    with pytest.raises(BayError, match="fleet 'ghost' not found"):
        Context.resolve(None)


# ── context_or_cwd ───────────────────────────────────────────────────


def test_context_or_cwd_5a_wins(standing_in_fleet):
    cx = context_or_cwd(None)
    assert cx.fleet_root == standing_in_fleet.resolve()
    assert cx.source == SOURCE_FLEET_HERE


def test_context_or_cwd_ignores_5b(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    _fleet(work, "alpha")
    empty = _empty(work)
    monkeypatch.chdir(empty)
    cx = context_or_cwd(None)
    assert cx.fleet_root == empty
    assert cx.source == SOURCE_CWD


def test_context_or_cwd_ignores_5b_when_two_fleets_live_above(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    _fleet(work, "alpha")
    _fleet(work, "beta")
    empty = _empty(work)
    monkeypatch.chdir(empty)
    assert context_or_cwd(None).fleet_root == empty


def test_context_or_cwd_replaces_a_cached_5b_fleet(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    _fleet(work, "alpha")
    empty = _empty(work)
    monkeypatch.chdir(empty)
    options = GlobalOptions()
    root = typer_ctx(options)
    cx = context_or_cwd(root)
    assert cx.fleet_root == empty
    assert options.context() is cx


def test_context_or_cwd_keeps_the_error_for_a_named_missing_fleet(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    _fleet(work, "alpha")
    app = _empty(work, "app")
    (app / "bay.toml").write_text('name = "app"\nfleet = "ghost"\n')
    monkeypatch.chdir(app)
    with pytest.raises(BayError, match="ghost"):
        context_or_cwd(None)


# ── The fleet line finds the same fleet ──────────────────────────────


def test_fleet_line_finds_5a_and_5b(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    fleet = _fleet(work, "alpha")
    monkeypatch.chdir(fleet)
    assert fleet_line.resolve("compile", {}) == (fleet.resolve(), None)
    empty = _empty(work)
    monkeypatch.chdir(empty)
    assert fleet_line.resolve("compile", {}) == (fleet.resolve(), None)


def test_fleet_line_fresh_folder_verbs_stay_in_the_folder(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    _fleet(work, "alpha")
    empty = _empty(work)
    monkeypatch.chdir(empty)
    assert fleet_line.resolve("server add", {}) == (empty.resolve(), None)


def test_fleet_line_prints_nothing_for_two_fleets(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    _fleet(work, "alpha")
    _fleet(work, "beta")
    monkeypatch.chdir(_empty(work))
    assert fleet_line.resolve("compile", {}) is None
