"""Error hints must name a command that can actually run.

The "bay not found" hint used to say `run 'bin/bay setup' first`. That cannot
work: `setup()` resolves the framework through `find_bay_dir()` itself, and the
`bin/bay` wrapper refuses to exec without `.bay/`. The hint named the one
command guaranteed to fail. The version-drift hints had a milder version of the
same problem — they named a Makefile alias for something `bin/bay install`
already does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bay_cli import paths
from bay_cli.errors import BayError

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_bay_not_found_hint_is_not_circular(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BAY_DIR", raising=False)
    with pytest.raises(BayError) as excinfo:
        paths.find_bay_dir(start=tmp_path)
    message = str(excinfo.value)
    assert "bay not found" in message
    # The bootstrap sequence, not the command that needs .bay/ to already exist.
    assert ".bay/bootstrap.sh" in message
    assert "git clone" in message
    assert "run 'bin/bay setup' first" not in message


@pytest.mark.parametrize("playbook", ["provision.yml", "deploy.yml"])
def test_version_drift_hint_names_the_cli(playbook: str) -> None:
    text = (_REPO_ROOT / playbook).read_text()
    assert "make bay:install" not in text, (
        f"{playbook} points at a Makefile alias instead of the CLI"
    )
    assert text.count("bin/bay install") == 1
