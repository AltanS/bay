"""Deploy reliability: trigger consumption, revision check, push retry, alert dedup.

Born of the 2026-10-02 incident: four builds of one service failed on registry blob PUTs,
retries hit "blob upload invalid", repeat alerts for one commit were swallowed,
a trigger written mid-build was deleted, and nothing checked that the running
container was the built commit. Like test_rebuild_config.py these tests render
the templates and run the relevant bash against stubs.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_cb_state_schema import _extract_helper, _helpers_bash  # noqa: E402
from test_correlation_id import _extract_corr_block, _render_for_corr_tests  # noqa: E402
from test_rebuild_config import (  # noqa: E402
    _local_service,
    _remote_service_with_token,
    _render_rebuild_sh,
    _render_systemd_unit,
)


@pytest.fixture(scope="module")
def rendered() -> str:
    return _render_for_corr_tests()


@pytest.fixture(scope="module")
def rendered_remote() -> str:
    services = _remote_service_with_token()
    services["animals"]["regions"] = ["eu"]
    return _render_rebuild_sh(
        services,
        ["animals"],
        git_deploy_services=["animals"],
        git_deploy_build_strategy="remote",
        peer_urls={"eu": "https://eu.example.com"},
    )


# ── R2: the trigger is consumed at the start of a run ───────────────────


def _run_corr(rendered: str, tmp_path: Path, content: str | None) -> dict[str, str]:
    triggers = tmp_path / "triggers"
    triggers.mkdir()
    if content is not None:
        (triggers / "localapp.trigger").write_text(content)
    script = f"""set -euo pipefail
STACK_DIR="{tmp_path}"
SERVICE="localapp"
EPOCHSECONDS=$(date +%s)
{_extract_corr_block(rendered)}
echo "CORR_ID=${{CORR_ID}}"
echo "PULL_SIGNAL=${{PULL_SIGNAL}}"
echo "EXPECTED_REVISION=${{EXPECTED_REVISION}}"
echo "BUILT_AT=${{BUILT_AT}}"
# a trigger written during the run must survive the EXIT trap
printf 'new\\n' > "${{STACK_DIR}}/triggers/${{SERVICE}}.trigger"
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return dict(
        line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line and line.split("=", 1)[0].isupper()
    )


class TestTriggerConsumption:
    def test_trigger_is_moved_to_running_and_cleaned_on_exit(self, rendered, tmp_path):
        out = _run_corr(rendered, tmp_path, "abc-123\npull")
        assert out["CORR_ID"] == "abc-123"
        assert out["PULL_SIGNAL"] == "1"
        # EXIT trap removed the .running file; the mid-run trigger is untouched.
        assert not (tmp_path / "triggers" / "localapp.trigger.running").exists()
        assert (tmp_path / "triggers" / "localapp.trigger").read_text() == "new\n"

    def test_no_trigger_means_manual_and_no_running_file(self, rendered, tmp_path):
        out = _run_corr(rendered, tmp_path, None)
        assert out["CORR_ID"].startswith("manual-")
        assert out["EXPECTED_REVISION"] == ""

    def test_revision_and_built_at_parsed(self, rendered, tmp_path):
        out = _run_corr(rendered, tmp_path, "cid\npull\nabcdef123456\n1760000000")
        assert out["EXPECTED_REVISION"] == "abcdef123456"
        assert out["BUILT_AT"] == "1760000000"

    def test_old_two_line_trigger_still_works(self, rendered, tmp_path):
        out = _run_corr(rendered, tmp_path, "cid\npull")
        assert out["PULL_SIGNAL"] == "1"
        assert out["EXPECTED_REVISION"] == ""
        assert out["BUILT_AT"] == ""

    def test_garbage_revision_is_ignored(self, rendered, tmp_path):
        out = _run_corr(rendered, tmp_path, "cid\npull\n$(touch x)\nsoon")
        assert out["EXPECTED_REVISION"] == ""
        assert out["BUILT_AT"] == ""

    def test_record_failure_does_not_remove_trigger(self, rendered):
        body = _extract_helper(rendered, "_record_failure")
        assert ".trigger" not in re.sub(r"#.*", "", body)

    def test_service_unit_no_longer_deletes_the_live_trigger(self):
        unit = _render_systemd_unit("bay-build@.service.j2")
        assert "ExecStartPost" not in unit
        assert "ExecStopPost=/usr/bin/rm -f /opt/teststack/triggers/%i.trigger.running" in unit
        assert "rm -f /opt/teststack/triggers/%i.trigger\n" not in unit

    def test_path_unit_watches_only_the_live_trigger(self):
        env_unit = _render_systemd_unit("bay-build@.path.j2")
        assert "PathExists=/opt/teststack/triggers/%i.trigger\n" in env_unit


