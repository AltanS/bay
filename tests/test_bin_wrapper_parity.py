"""Both `bin/bay` writers emit identical content.

`bootstrap.sh` and `bay setup` (`_ensure_bin_wrapper`) each create the
consumer's `bin/bay`, and both guard on "does it already exist" — so whichever
ran first won. They had already drifted: only the bootstrap version unset
`VIRTUAL_ENV`, and the two printed different missing-framework hints. Both now
copy `scripts/bin-bay-wrapper.sh` verbatim; this pins that.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bay_cli.commands.framework import WRAPPER_SOURCE, _ensure_bin_wrapper

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WRAPPER = _REPO_ROOT / WRAPPER_SOURCE


def test_wrapper_source_exists_and_is_executable() -> None:
    assert _WRAPPER.is_file(), f"{WRAPPER_SOURCE} is the single source of truth"
    assert _WRAPPER.stat().st_mode & 0o111, "the wrapper source must be executable"


def test_wrapper_source_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(_WRAPPER)], check=True)


def test_wrapper_unsets_virtualenv_and_execs_uv() -> None:
    text = _WRAPPER.read_text()
    # An inherited VIRTUAL_ENV makes `uv run --project .bay` warn and, in some
    # shells, resolve the wrong interpreter.
    assert "unset VIRTUAL_ENV" in text
    assert 'exec uv run --project "${SCRIPT_DIR}/.bay" bay "$@"' in text


def test_wrapper_hint_names_the_one_entry_path() -> None:
    """A missing .bay/ cannot be fixed by `make bay:setup` alone in every layout."""
    text = _WRAPPER.read_text()
    assert ".bay/bootstrap.sh" in text
    assert "make bay:setup" not in text


def test_bootstrap_copies_the_shared_wrapper() -> None:
    text = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert f'cp "$BAY_DIR/{WRAPPER_SOURCE}" bin/bay' in text, (
        "bootstrap.sh must copy the shared wrapper, not inline its own copy"
    )
    assert "WRAPPER" not in text, "bootstrap.sh still has an inline wrapper heredoc"


def test_ensure_bin_wrapper_writes_the_shared_wrapper(tmp_path: Path) -> None:
    root = tmp_path / "consumer"
    root.mkdir()
    _ensure_bin_wrapper(root, _REPO_ROOT)
    written = root / "bin" / "bay"
    assert written.read_text() == _WRAPPER.read_text()
    assert written.stat().st_mode & 0o111
