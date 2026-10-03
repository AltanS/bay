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


# ── R3: revision label, pull-signal body, post-pull check ───────────────


class TestRevision:
    def test_buildx_sets_label_and_build_arg_from_the_tag_sha(self, rendered_remote):
        assert '--label "org.opencontainers.image.revision=${SHA}"' in rendered_remote
        assert '--build-arg "BAY_GIT_SHA=${SHA}"' in rendered_remote
        assert '-t "${IMAGE_REPO}:${SHA}"' in rendered_remote

    def test_pull_signal_body_carries_revision_and_built_at(self, rendered_remote):
        assert "--arg revision \"${SHA}\"" in rendered_remote
        assert "{image: $image, revision: $revision, built_at: $built_at}" in rendered_remote
        # the HMAC is computed over the same variable that is POSTed
        assert "printf '%s' \"${PULL_BODY}\" | openssl dgst" in rendered_remote
        assert '-d "${PULL_BODY}"' in rendered_remote

    def test_pull_body_is_valid_json(self, rendered_remote):
        m = re.search(r"PULL_BODY=\$\(jq -cn.*?\)\n", rendered_remote, re.DOTALL)
        assert m, "PULL_BODY assignment not found"
        script = f'IMAGE_REF="r/x:latest"; SHA=abcdef123456; BUILT_AT_EPOCH=1760000000\n{m.group(0)}echo "$PULL_BODY"'
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == '{"image":"r/x:latest","revision":"abcdef123456","built_at":1760000000}'

    def test_revision_check_runs_after_health_and_before_reset(self, rendered):
        health = rendered.index('if ! _wait_healthy "${SERVICE}" "${HEALTH_CHECK_TIMEOUT}"; then\n    _handle_rollback')
        check = rendered.index("Revision check failed for")
        reset = rendered.index("_reset_cb", check)
        assert health < check < reset
        assert '_record_failure "" "Revision check"' in rendered
        assert 'org.opencontainers.image.revision' in rendered
        assert "live ${EXPECTED_REVISION} in" in rendered

    def _run_check(
        self,
        rendered: str,
        live: str,
        expected: str,
        tmp_path: Path,
        live_img: str = "sha256:aaa",
        pulled_img: str = "sha256:bbb",
    ):
        m = re.search(
            r"  if \[\[ -n \"\$\{EXPECTED_REVISION\}\" \]\]; then.*?\n  fi\n", rendered, re.DOTALL
        )
        assert m, "revision check block not found"
        script = f"""set -uo pipefail
SERVICE=svc; IMAGE_REF=r/svc:latest; EXPECTED_REVISION={expected!r}; BUILT_AT=$(( $(date +%s) - 7 ))
_log() {{ echo "LOG: $*"; }}
docker() {{
  case "$*" in
    *"image inspect"*) printf '%s' {pulled_img!r} ;;
    *".Image}}"*) printf '%s' {live_img!r} ;;
    *) printf '%s' {live!r} ;;
  esac
}}
_record_failure() {{ echo "FAIL: [$1] [$2] [$3]"; }}
for _ in 1; do
{m.group(0)}
echo reached-end
done
"""
        # `exit 1` inside the block ends the shell; reached-end means success.
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_match_logs_live_line(self, rendered, tmp_path):
        proc = self._run_check(rendered, "abcdef123456", "abcdef123456", tmp_path)
        assert "reached-end" in proc.stdout
        assert re.search(r"LOG: live abcdef123456 in [78]s", proc.stdout)
        assert "FAIL" not in proc.stdout

    def test_mismatch_fails_with_empty_sha_so_it_always_notifies(self, rendered, tmp_path):
        proc = self._run_check(rendered, "111111111111", "abcdef123456", tmp_path)
        assert proc.returncode == 1
        assert "FAIL: [] [Revision check] [Revision check: running 111111111111, expected abcdef123456]" in proc.stdout

    def test_missing_label_fails(self, rendered, tmp_path):
        proc = self._run_check(rendered, "", "abcdef123456", tmp_path)
        assert proc.returncode == 1
        assert "running <none>, expected abcdef123456" in proc.stdout

    def test_unlabeled_image_matching_the_pulled_image_passes(self, rendered, tmp_path):
        # An image built before the label existed, re-announced because its
        # :<sha> tag was already in the registry.
        proc = self._run_check(
            rendered, "", "abcdef123456", tmp_path, live_img="sha256:same", pulled_img="sha256:same"
        )
        assert "reached-end" in proc.stdout
        assert "no revision label" in proc.stdout
        assert re.search(r"LOG: live abcdef123456 in [78]s", proc.stdout)
        assert "FAIL" not in proc.stdout

    def test_unlabeled_stale_container_still_fails(self, rendered, tmp_path):
        proc = self._run_check(
            rendered, "", "abcdef123456", tmp_path, live_img="sha256:old", pulled_img="sha256:new"
        )
        assert proc.returncode == 1
        assert "running <none>, expected abcdef123456" in proc.stdout

    def test_no_expected_revision_skips_the_check(self, rendered, tmp_path):
        proc = self._run_check(rendered, "whatever", "", tmp_path)
        assert "reached-end" in proc.stdout
        assert "LOG" not in proc.stdout


# ── R4: push retry and honest alert suppression ─────────────────────────


def _run_retry(rendered: str, outputs: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    """Run _build_with_push_retry; the i-th attempt writes outputs[i] and fails
    (an empty string means that attempt succeeds)."""
    for i, text in enumerate(outputs):
        (tmp_path / f"out{i}").write_text(text)
    pattern = re.search(r"^BAY_PUSH_RETRY_PATTERN=.*$", rendered, re.MULTILINE).group(0)
    delays = re.search(r"^BAY_PUSH_RETRY_DELAYS=.*$", rendered, re.MULTILINE).group(0)
    script = f"""set -uo pipefail
{pattern}
{delays}
{_extract_helper(rendered, "_build_with_push_retry")}
_log() {{ echo "LOG: $*"; }}
sleep() {{ echo "SLEEP: $1"; }}
BUILD_OUTPUT={tmp_path}/current
n=0
attempt() {{
  cp {tmp_path}/out$n "$BUILD_OUTPUT"
  local rc=0; [[ -s "$BUILD_OUTPUT" ]] && rc=1
  n=$((n+1)); echo "ATTEMPT on $1"
  return $rc
}}
_build_with_push_retry attempt bay-remote; echo "RC=$?"
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


class TestPushRetry:
    @pytest.mark.parametrize(
        "err",
        [
            "blob upload invalid",
            "unexpected EOF",
            "unexpected status code 499",
            "received unexpected HTTP status: 504 Gateway Timeout; status code 504",
            "status code 502",
            "dial tcp: i/o timeout",
            "read: connection reset by peer",
        ],
    )
    def test_transient_error_is_retried_with_backoff_then_succeeds(self, rendered, tmp_path, err):
        proc = _run_retry(rendered, [err, err, ""], tmp_path)
        assert proc.stdout.count("ATTEMPT on bay-remote") == 3
        assert proc.stdout.index("SLEEP: 15") < proc.stdout.index("SLEEP: 45")
        assert proc.stdout.count("retry") == 2
        assert "RC=0" in proc.stdout

    def test_gives_up_after_two_retries(self, rendered, tmp_path):
        proc = _run_retry(rendered, ["status code 504"] * 4, tmp_path)
        assert proc.stdout.count("ATTEMPT") == 3
        assert "RC=1" in proc.stdout

    def test_compile_error_is_not_retried(self, rendered, tmp_path):
        proc = _run_retry(rendered, ["error TS2322: Type 'x' is not assignable"], tmp_path)
        assert proc.stdout.count("ATTEMPT") == 1
        assert "SLEEP" not in proc.stdout
        assert "RC=1" in proc.stdout

    def test_both_builder_paths_use_the_retry(self, rendered):
        body = _extract_helper(rendered, "_build_with_fallback")
        assert body.count("_build_with_push_retry") == 2


class TestAlertSuppression:
    def _failures(self, rendered: str, calls: list[tuple[str, str, str]], tmp_path: Path) -> list[str]:
        state = tmp_path / "state"
        state.mkdir()
        sf = state / "svc.json"
        lines = "\n".join(
            f"_record_failure {sha!r} {ctx!r} {detail!r}" for sha, ctx, detail in calls
        )
        script = f"""set -uo pipefail
STATE_DIR={str(state)!r}; STATE_FILE={str(sf)!r}; STACK_DIR={str(tmp_path)!r}
CB_MAX_FAILURES=99; SERVICE=svc; HOSTNAME=h
notify_build() {{ echo NOTIFY; }}
format_timestamp() {{ echo now; }}
{_helpers_bash(rendered)}
{lines}
"""
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return [ln for ln in proc.stdout.splitlines() if ln == "NOTIFY"]

    def test_same_sha_same_failure_is_suppressed(self, rendered, tmp_path):
        out = self._failures(
            rendered,
            [("c1", "Remote build", "PUT 504 60001ms"), ("c1", "Remote build", "PUT 504 60012ms")],
            tmp_path,
        )
        assert out == ["NOTIFY"]

    def test_same_sha_different_error_notifies(self, rendered, tmp_path):
        out = self._failures(
            rendered,
            [("c1", "Remote build", "PUT status code 504"), ("c1", "Remote build", "blob upload invalid")],
            tmp_path,
        )
        assert out == ["NOTIFY", "NOTIFY"]

    def test_same_sha_different_stage_notifies(self, rendered, tmp_path):
        out = self._failures(
            rendered, [("c1", "Remote build", "x"), ("c1", "Build", "x")], tmp_path
        )
        assert out == ["NOTIFY", "NOTIFY"]

    def test_new_sha_notifies(self, rendered, tmp_path):
        out = self._failures(rendered, [("c1", "Build", "x"), ("c2", "Build", "x")], tmp_path)
        assert out == ["NOTIFY", "NOTIFY"]

    def test_empty_sha_always_notifies(self, rendered, tmp_path):
        out = self._failures(
            rendered, [("", "Revision check", "x"), ("", "Revision check", "x")], tmp_path
        )
        assert out == ["NOTIFY", "NOTIFY"]
