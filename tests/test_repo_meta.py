"""Repository metadata that a first-time visitor sees.

The `.github/` directory held one file (the CI workflow) for the repo's whole
public life: no issue templates, no PR template, and the README carried no
build status at all. These tests pin the contribution furniture so it cannot
quietly disappear again.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ISSUE_TEMPLATES = _REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
_PR_TEMPLATE = _REPO_ROOT / ".github" / "pull_request_template.md"
_README = _REPO_ROOT / "README.md"

_REQUIRED_FRONT_MATTER = ("name", "about", "title", "labels")


def _front_matter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path.name} has no YAML front matter"
    _, block, _ = text.split("---\n", 2)
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, dict), f"{path.name} front matter is not a mapping"
    return parsed


def test_issue_templates_exist_with_front_matter() -> None:
    """GitHub only offers a template in the picker if the front matter parses."""
    expected = {"bug_report.md", "feature_request.md"}
    present = {p.name for p in _ISSUE_TEMPLATES.glob("*.md")}
    assert expected <= present, f"missing issue templates: {expected - present}"
    for name in sorted(expected):
        meta = _front_matter(_ISSUE_TEMPLATES / name)
        for key in _REQUIRED_FRONT_MATTER:
            assert meta.get(key), f"{name} front matter is missing `{key}`"


def test_issue_templates_bug_report_warns_about_real_infrastructure() -> None:
    """This repo is scrubbed and leak-scanned; a pasted vault dump undoes that."""
    text = _ISSUE_TEMPLATES / "bug_report.md"
    body = text.read_text().lower()
    assert "do not paste" in body
    for term in ("domain", "ip address", "token"):
        assert term in body, f"bug_report.md does not warn about {term}s"
    assert "bin/bay status" in body, "bug_report.md must ask for the version string"


def test_pull_request_template_names_the_checklist() -> None:
    assert _PR_TEMPLATE.is_file(), "no .github/pull_request_template.md"
    body = _PR_TEMPLATE.read_text()
    for item in ("make test", "make lint", "CHANGELOG", "make release"):
        assert item in body, f"PR template does not name `{item}`"


def test_readme_carries_the_ci_badge_in_its_head() -> None:
    """A badge below the fold is a badge nobody sees."""
    head = "\n".join(_README.read_text().splitlines()[:20])
    assert "actions/workflows/ci.yml/badge.svg" in head


def test_readme_test_section_leads_with_make_install() -> None:
    """`make test` in a fresh clone runs against mock Galaxy stubs."""
    lines = _README.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "### Test suites")
    end = next(
        i for i, line in enumerate(lines[start + 1 :], start + 1)
        if line.startswith("### ") or line.startswith("## ")
    )
    section = "\n".join(lines[start:end])
    intro = section.split("| Command |")[0]
    assert "make install" in intro, "the test section does not open with `make install`"
    assert "vendor/" in intro, "the test section does not say Galaxy deps land in vendor/"
    assert "core.hooksPath" in intro, "the test section does not mention core.hooksPath"
