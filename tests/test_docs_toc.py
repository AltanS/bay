"""Tests for scripts/docs-toc.py, the generator for contents lists in the docs."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "docs-toc.py"

_spec = importlib.util.spec_from_file_location("docs_toc", SCRIPT)
toc = importlib.util.module_from_spec(_spec)
sys.modules["docs_toc"] = toc
_spec.loader.exec_module(toc)


def test_check_passes_on_repo():
    r = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--check"], capture_output=True, text=True, cwd=ROOT
    )
    assert r.returncode == 0, f"run `make toc`:\n{r.stdout}{r.stderr}"


@pytest.mark.parametrize(
    "heading, slug",
    [
        ("Plain words", "plain-words"),
        ("The `bay up` verb", "the-bay-up-verb"),
        ("`bay.toml`", "baytoml"),
        ("`[deploy.<env>]`", "deployenv"),
        ("[deploy.<env>] table", "deployenv-table"),
        ("Rig vs fast: what/why (and when)?", "rig-vs-fast-whatwhy-and-when"),
        ("snake_case and kebab-name", "snake_case-and-kebab-name"),
        ("A  double space", "a--double-space"),
        ("A - dash", "a---dash"),
        ("[Link text](http://example.com/x)", "link-text"),
        ("Ünïcode Ärger", "ünïcode-ärger"),
    ],
)
def test_slug(heading, slug):
    assert toc.slug_base(heading) == slug


def test_duplicate_slugs_numbered_in_order():
    heads = [(2, "Setup"), (3, "Setup"), (2, "Other"), (2, "Setup")]
    assert toc.anchors(heads) == ["setup", "setup-1", "other", "setup-2"]


def test_heading_in_code_fence_is_ignored():
    lines = ["# T", "## Real", "```", "## Fake", "```", "~~~", "### Fake2", "~~~", "### Sub"]
    assert toc.headings(lines) == [(1, "T"), (2, "Real"), (3, "Sub")]


def test_block_lists_only_h2_and_h3(tmp_path):
    lines = ["# T", "", "intro", "", "## A", "#### deep", "### B"]
    block = toc.build_block(lines)
    assert "- [A](#a)" in block and "  - [B](#b)" in block
    assert not any("deep" in b or "[T]" in b for b in block)


def _sample(extra=150):
    body = [f"line {i}" for i in range(extra)]
    return "\n".join(["# Title", "", "First paragraph.", "still first.", "", "## One", *body, "### Two", ""])


def test_render_is_idempotent_and_placed_after_first_paragraph(tmp_path):
    once = toc.render(_sample())
    assert once is not None
    assert once.index(toc.START) > once.index("still first.")
    assert once.index(toc.START) < once.index("## One")
    assert toc.render(once) == once


def test_short_file_gets_no_list():
    assert toc.render("# T\n\n## A\n") is None


def test_no_h1_places_block_at_top():
    text = "\n".join(["## A", *["x"] * 160, ""])
    out = toc.render(text)
    assert out.startswith(toc.START)
    assert toc.render(out) == out


def test_cli_rewrites_then_check_passes(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "docs-toc.py").write_text(SCRIPT.read_text())
    (tmp_path / "README.md").write_text(_sample())
    (tmp_path / "docs" / "a.md").write_text("# Short\n\n## X\n")
    run = lambda *a: subprocess.run(
        [sys.executable, "-I", str(tmp_path / "scripts" / "docs-toc.py"), *a],
        capture_output=True, text=True,
    )
    assert run("--check").returncode == 1
    assert run().returncode == 0
    first = (tmp_path / "README.md").read_text()
    assert run("--check").returncode == 0
    run()
    assert (tmp_path / "README.md").read_text() == first
    assert toc.START not in (tmp_path / "docs" / "a.md").read_text()


def test_no_h1_after_leading_fence_keeps_banner_and_badge_on_top():
    text = "\n".join(["```", "BANNER", "```", "", "[badge](x)", "", "## A", *["x"] * 160, ""])
    out = toc.render(text)
    assert out.index("[badge](x)") < out.index(toc.START) < out.index("## A")
    assert toc.render(out) == out
