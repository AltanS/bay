"""M108-S03: a freshly scaffolded consumer must contain zero old-name strings.

The rename is a hard cut on the scaffolding surface — a consumer created on
1.0.0 never sees the pre-1.0 vocabulary, so there is nothing for it to migrate
later. This test renders the whole wizard template set into a tmpdir and greps
it, which is the only check that covers *every* template at once (a per-file
review misses the one nobody thought to open).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bay_cli.wizard.models import RegionConfig, WizardResult
from bay_cli.wizard.scaffold import scaffold

_OLD_NAME_RE = re.compile(r"argo", re.IGNORECASE)  # legacy-argo: regex detects old-name strings


@pytest.fixture()
def single_server_result() -> WizardResult:
    return WizardResult(
        project_name="testapp",
        multi_region=False,
        server_ip="10.0.0.1",
        domain_base="example.com",
        letsencrypt_email="ops@example.com",
        ssh_keys=[],
        access_gateway="headscale",
        headscale_domain="hs.example.com",
        selected_services=["gatus", "postgres", "redis"],
    )


@pytest.fixture()
def multi_region_result() -> WizardResult:
    return WizardResult(
        project_name="testapp",
        multi_region=True,
        regions=[
            RegionConfig(name="eu", server_ip="10.0.0.1"),
            RegionConfig(name="na", server_ip="10.0.0.2"),
        ],
        domain_base="example.com",
        letsencrypt_email="ops@example.com",
        ssh_keys=[],
        access_gateway="headscale",
        headscale_domain="hs.example.com",
        selected_services=["gatus", "postgres", "redis"],
    )


def _offenders(target: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _OLD_NAME_RE.search(line):
                hits.append(f"{path.relative_to(target)}:{line_no}: {line.strip()}")
    return hits


def test_scaffold_clean_single_server(
    tmp_path: Path, single_server_result: WizardResult
) -> None:
    scaffold(single_server_result, tmp_path)
    offenders = _offenders(tmp_path)
    assert not offenders, "scaffolded consumer still names the old framework:\n" + "\n".join(
        offenders
    )


def test_scaffold_clean_multi_region(
    tmp_path: Path, multi_region_result: WizardResult
) -> None:
    scaffold(multi_region_result, tmp_path)
    offenders = _offenders(tmp_path)
    assert not offenders, "scaffolded consumer still names the old framework:\n" + "\n".join(
        offenders
    )


def test_scaffold_clean_check_is_not_vacuous(tmp_path: Path) -> None:
    """Re-injection self-test — the grep must actually be able to go red."""
    (tmp_path / "Makefile").write_text("ARGO_DIR := .argo\n")  # legacy-argo: re-injection self-test fixture
    assert _offenders(tmp_path)
