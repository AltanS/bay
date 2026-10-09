"""`bay skill`: install, track, update and remove the agent skill per harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bay_cli import agent_skill, cli

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENCODE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    return home


def _manifest(home: Path) -> dict:
    return json.loads((home / ".config" / "bay" / "skills.json").read_text())


def test_render_stamps_the_root_skill_md() -> None:
    text = agent_skill.render("9.9.9")
    assert text.startswith("---\nname: bay\n")
    stamp = agent_skill.read_stamp(text)
    assert stamp is not None and stamp[0] == "9.9.9"
    assert text.splitlines()[-1].startswith("<!-- bay-skill 9.9.9 ")


def test_skill_paths_per_harness(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = {h.id: h.skill_file() for h in agent_skill.HARNESSES}
    assert paths == {
        "claude": home / ".claude/skills/bay/SKILL.md",
        "codex": home / ".codex/skills/bay/SKILL.md",
        "opencode": home / ".config/opencode/skills/bay/SKILL.md",
        "pi": home / ".pi/agent/skills/bay/SKILL.md",
    }
    monkeypatch.setenv("CODEX_HOME", str(home / "cx"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    assert agent_skill.harness_by_id("codex").skill_file() == home / "cx/skills/bay/SKILL.md"
    assert agent_skill.harness_by_id("opencode").skill_file() == home / "xdg/opencode/skills/bay/SKILL.md"


def test_install_finds_harnesses_by_config_dir_and_records_them(home: Path) -> None:
    (home / ".claude").mkdir()
    (home / ".pi" / "agent").mkdir(parents=True)
    result = runner.invoke(cli.app, ["skill", "install"])
    assert result.exit_code == 0, result.output
    assert (home / ".claude/skills/bay/SKILL.md").is_file()
    assert (home / ".pi/agent/skills/bay/SKILL.md").is_file()
    assert not (home / ".codex").exists()
    manifest = _manifest(home)
    assert sorted(manifest["installs"]) == ["claude", "pi"]
    assert manifest["installs"]["claude"]["path"] == str(home / ".claude/skills/bay/SKILL.md")


def test_install_with_no_harness_found_fails(home: Path) -> None:
    result = runner.invoke(cli.app, ["skill", "install"])
    assert result.exit_code != 0
    assert "no agent harness" in str(result.exception)


def test_update_rewrites_an_outdated_file_and_keeps_an_edited_one(home: Path) -> None:
    agent_skill.install(["claude", "codex"], "1.0.0")
    claude = home / ".claude/skills/bay/SKILL.md"
    codex = home / ".codex/skills/bay/SKILL.md"
    codex.write_text(codex.read_text().replace("# Bay", "# Bay (mine)", 1))

    assert agent_skill.file_state(claude, agent_skill.render("2.0.0")) == "outdated"
    assert agent_skill.file_state(codex, agent_skill.render("2.0.0")) == "edited"

    outcomes = {o.harness: o.result for o in agent_skill.update("2.0.0")}
    assert outcomes == {"claude": "written", "codex": "refused"}
    assert agent_skill.read_stamp(claude.read_text())[0] == "2.0.0"
    assert _manifest(home)["installs"]["codex"]["version"] == "1.0.0"

    forced = {o.harness: o.result for o in agent_skill.update("2.0.0", force=True)}
    assert forced == {"claude": "unchanged", "codex": "written"}


def test_install_never_overwrites_a_file_bay_did_not_write(home: Path) -> None:
    mine = home / ".claude/skills/bay/SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("my own bay notes\n")
    result = runner.invoke(cli.app, ["skill", "install", "-H", "claude"])
    assert result.exit_code == 1
    assert "refused" in result.output
    assert mine.read_text() == "my own bay notes\n"
    assert "claude" not in _manifest(home)["installs"]


def test_update_writes_a_deleted_file_again(home: Path) -> None:
    agent_skill.install(["pi"], "1.0.0")
    path = home / ".pi/agent/skills/bay/SKILL.md"
    path.unlink()
    assert [o.result for o in agent_skill.update("1.0.0")] == ["written"]
    assert path.is_file()


def test_status_exit_code_follows_recorded_installs(home: Path) -> None:
    assert runner.invoke(cli.app, ["skill", "status"]).exit_code == 0
    runner.invoke(cli.app, ["skill", "install", "-H", "opencode"])
    ok = runner.invoke(cli.app, ["--json", "skill", "status"])
    assert ok.exit_code == 0, ok.output
    rows = {r["harness"]: r for r in json.loads(ok.output)["data"]["harnesses"]}
    assert rows["opencode"]["state"] == "ok" and rows["opencode"]["recorded"]
    assert rows["claude"]["state"] == "missing" and not rows["claude"]["recorded"]

    (home / ".config/opencode/skills/bay/SKILL.md").unlink()
    assert runner.invoke(cli.app, ["skill", "status"]).exit_code == 1


def test_uninstall_removes_files_dirs_and_records(home: Path) -> None:
    agent_skill.install(["claude", "pi"], "1.0.0")
    result = runner.invoke(cli.app, ["skill", "uninstall", "-H", "pi"])
    assert result.exit_code == 0, result.output
    assert not (home / ".pi/agent/skills/bay").exists()
    assert (home / ".pi/agent/skills").is_dir()
    assert sorted(_manifest(home)["installs"]) == ["claude"]
    assert runner.invoke(cli.app, ["skill", "uninstall"]).exit_code == 0
    assert _manifest(home)["installs"] == {}


def test_show_prints_the_stamped_text(home: Path) -> None:
    result = runner.invoke(cli.app, ["skill", "show"])
    assert result.exit_code == 0
    assert result.output.startswith("---\nname: bay\n")
    assert agent_skill.read_stamp(result.output) is not None


def test_unknown_harness_writes_nothing(home: Path) -> None:
    result = runner.invoke(cli.app, ["skill", "install", "-H", "claude", "-H", "bogus"])
    assert result.exit_code != 0
    assert not (home / ".claude/skills/bay/SKILL.md").exists()
    assert runner.invoke(cli.app, ["skill", "uninstall", "-H", "bogus"]).exit_code != 0


def test_uninstall_leaves_a_file_bay_did_not_write(home: Path) -> None:
    agent_skill.install(["claude"], "1.0.0")
    path = home / ".claude/skills/bay/SKILL.md"
    path.write_text("replaced by hand\n")
    result = runner.invoke(cli.app, ["skill", "uninstall"])
    assert result.exit_code == 1
    assert path.read_text() == "replaced by hand\n"


def test_install_force_overwrites_an_edited_file(home: Path) -> None:
    agent_skill.install(["claude"], "1.0.0")
    path = home / ".claude/skills/bay/SKILL.md"
    path.write_text(path.read_text().replace("# Bay", "# Bay (mine)", 1))
    assert runner.invoke(cli.app, ["skill", "install", "-H", "claude"]).exit_code == 1
    forced = runner.invoke(cli.app, ["skill", "install", "-H", "claude", "--force"])
    assert forced.exit_code == 0, forced.output
    assert "# Bay (mine)" not in path.read_text()


def test_a_corrupt_manifest_names_the_fix(home: Path) -> None:
    manifest = home / ".config/bay/skills.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{not json")
    result = runner.invoke(cli.app, ["skill", "status"])
    assert result.exit_code != 0
    assert "skills.json" in str(result.exception)


def test_update_skips_a_removed_harness(home: Path) -> None:
    agent_skill.install(["codex"], "1.0.0")
    import shutil

    shutil.rmtree(home / ".codex")
    assert [o.result for o in agent_skill.update("2.0.0")] == ["skipped"]
    assert not (home / ".codex").exists()
