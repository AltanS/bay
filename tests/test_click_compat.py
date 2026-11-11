"""``bay_cli`` must not import ``click``.

Newer Typer ships Click inside itself, so a ``uv tool install`` has no top-level
``click``. v2.19.0 crashed on ``bay deploy --log`` and ``bay provision --log``
with ``ModuleNotFoundError: No module named 'click'``, because the test env has
``click`` through its dev dependencies. These tests close that gap two ways:
an AST scan that bans the import, and a subprocess where ``click`` cannot be
imported.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
HELPER = SRC / "bay_cli" / "click_compat.py"


def _click_imports(path: Path) -> list[int]:
    """Line numbers of every ``import click[...]`` and ``from click[...] import`` (any depth)."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            if any(a.name == "click" or a.name.startswith("click.") for a in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and (mod == "click" or mod.startswith("click.")):
                lines.append(node.lineno)
    return lines


def test_no_import_click_under_src() -> None:
    offenders = {
        str(p.relative_to(ROOT)): lines
        for pkg in ("bay_cli", "bay_reconcile")
        for p in (SRC / pkg).rglob("*.py")
        if (lines := _click_imports(p))
    }
    assert offenders == {}, (
        "import click is banned in src: newer Typer vendors Click and the installed "
        f"CLI has no top-level click. Use bay_cli.click_compat. Found: {offenders}"
    )


def test_the_scan_sees_every_import_form(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text(
        "import click\n"
        "from click import exceptions\n"
        "from click.exceptions import Exit\n"
        "import click.exceptions as ce\n"
        "def g():\n    import click\n",
        encoding="utf-8",
    )
    assert _click_imports(f) == [1, 2, 3, 4, 6]
    ok = tmp_path / "y.py"
    ok.write_text("import clicker\nfrom . import click\nimport typer\n", encoding="utf-8")
    assert _click_imports(ok) == []


_SCRIPT = textwrap.dedent(
    """
    import sys, tempfile
    from pathlib import Path

    # Typer first: with an old Typer it loads the real click and uses it by
    # reference. After this, any "import click" made by code outside click and
    # typer must fail, as in a tool install with a new Typer that has no top-level
    # click. A hook on __import__ also catches the cached case.
    import builtins
    import typer  # noqa: F401

    _real_import = builtins.__import__

    def _no_click(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and (name == "click" or name.startswith("click.")):
            who = (globals or {}).get("__name__", "") or ""
            if who.split(".")[0] not in ("click", "typer"):
                raise ModuleNotFoundError("No module named 'click'", name="click")
        return _real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = _no_click
    try:
        import click  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        raise SystemExit("blocker did not work")

    from bay_cli.cli import app
    from bay_cli.commands import output
    from bay_cli import click_compat

    # 1. the helper gives working classes
    assert issubclass(click_compat.Exit, BaseException)
    assert issubclass(click_compat.ClickException, Exception)
    assert click_compat.exit_code_of(typer.Exit(3)) == 3
    assert click_compat.exit_code_of(ValueError("x")) is None

    # 2. the --log summary context manager
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "run.log"
    log.write_text("", encoding="utf-8")
    seen = []
    orig = output.print_summary
    output.print_summary = lambda lines: seen.append(list(lines))
    with output._summary_at_end("bay deploy", log):
        pass
    try:
        with output._summary_at_end("bay deploy", log):
            raise typer.Exit(0)
    except typer.Exit:
        pass
    try:
        with output._summary_at_end("bay deploy", log):
            raise typer.Exit(2)
    except typer.Exit:
        pass
    try:
        with output._summary_at_end("bay deploy", log):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    output.print_summary = orig
    assert len(seen) == 4, seen
    joined = ["\\n".join(s).lower() for s in seen]
    assert "fail" not in joined[0] and "fail" not in joined[1], joined
    assert "fail" in joined[2] and "fail" in joined[3], joined

    # 3. --help of the commands that use it
    for argv in (["deploy", "--help"], ["provision", "--help"], ["host", "upgrade", "--help"]):
        try:
            app(argv, prog_name="bay")
        except SystemExit as exc:
            assert exc.code in (0, None), (argv, exc.code)
    print("OK")
    """
)


def test_helper_and_log_summary_work_without_click() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().endswith("OK"), proc.stdout + proc.stderr
