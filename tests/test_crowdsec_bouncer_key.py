"""Guards the crowdsec firewall-bouncer API key task against blanking the key.

Context (2026-07-15). A `--check` run of `provision` predicted this diff on every
production server:

    -api_key: <redacted>
    +api_key:

An empty api_key silently unauthenticates the firewall bouncer against the
LAPI: it stops pulling decisions and IPS enforcement dies quietly, with no
error anywhere. Read literally, provisioning was about to do that everywhere.

It was a false diff, from an ansible-core >= 2.19 semantics change. A command
task skipped BY CHECK MODE now registers `{skipped: true, rc: 0, stdout: ""}`
— a *fake success rc*. Under --check that cascades:

    "Check if firewall bouncer is already registered"  -> check-skipped, stdout ""
    "Register firewall bouncer with LAPI"  -> `when` passes ('' contains nothing)
                                           -> check-skipped, rc 0, stdout ""
    "Configure firewall bouncer API key"   -> `rc is defined and rc == 0` PASSES
                                           -> predicts `api_key: <empty>`

Nothing is written in check mode, and a `when`-skipped task in a REAL run
registers no `rc` at all — which is why this never fired for real. Verified on
the live hosts: the task skipped on all 4, keys intact, bouncers kept pulling.

`rc is defined` no longer means "the task really ran". Hence `is not skipped`.
The `stdout | length > 0` guard closes the one genuine blank-key path left:
cscli itself exiting 0 with empty output.

Do NOT relax these guards to make a check-mode diff look tidier.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CROWDSEC_TASKS = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "crowdsec"
    / "tasks"
    / "main.yml"
)


def _task_named(name: str) -> dict:
    tasks = yaml.safe_load(_CROWDSEC_TASKS.read_text())
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in {_CROWDSEC_TASKS}")


@pytest.fixture(scope="module")
def api_key_task() -> dict:
    return _task_named("Configure firewall bouncer API key")


class TestApiKeyTaskGuards:
    def test_guards_against_check_mode_skip_result(self, api_key_task: dict) -> None:
        when = [str(c) for c in api_key_task["when"]]
        assert "bouncer_key is not skipped" in when, (
            "without `is not skipped`, a check-mode skip result (rc: 0, stdout: '') "
            "satisfies the rc guard and predicts blanking the api_key"
        )

    def test_guards_against_empty_stdout(self, api_key_task: dict) -> None:
        when = [str(c) for c in api_key_task["when"]]
        assert any("stdout | length > 0" in c for c in when), (
            "an empty api_key silently unauthenticates the bouncer — never write "
            "one, even if cscli exits 0"
        )

    def test_still_requires_a_successful_registration(self, api_key_task: dict) -> None:
        when = [str(c) for c in api_key_task["when"]]
        assert "bouncer_key.rc is defined" in when
        assert "bouncer_key.rc == 0" in when

    def test_writes_the_registered_key(self, api_key_task: dict) -> None:
        assert api_key_task["ansible.builtin.lineinfile"]["line"] == (
            "api_key: {{ bouncer_key.stdout }}"
        )


class TestHttpProbingOverrideRemoval:
    """Removing "our override" must not remove the hub's scenario.

    Bay's override deliberately shadows crowdsecurity/http-probing at the same
    path, so `state: absent` there is ambiguous. cscli installs hub content as a
    SYMLINK (/etc/crowdsec/scenarios/x.yaml -> /etc/crowdsec/hub/...) while
    everything Ansible renders is a regular file — verified on every
    production host, where every bay scenario is a file and every hub one
    (CVE-*, http-probing) is a symlink.

    Unconditionally deleting the path removed the HUB's symlink on every
    provision (exclude_404 defaults to false, so the task always fired).
    `cscli collections install` later in the same role restored it, so the end
    state looked fine while both tasks reported `changed` forever, reloaded
    crowdsec every run, and left http-probing genuinely absent in between.
    """

    def test_removal_is_gated_on_a_stat(self) -> None:
        task = _task_named("Remove http-probing override when disabled")
        when = " ".join(str(c) for c in task["when"])
        assert "_http_probing_path.stat.exists" in when, (
            "removal must be gated on the path existing, or it reports changed "
            "on every run"
        )

    def test_hub_symlink_is_never_removed(self) -> None:
        task = _task_named("Remove http-probing override when disabled")
        when = " ".join(str(c) for c in task["when"])
        assert "islnk" in when, (
            "a symlink at this path is the HUB's scenario, not our override — "
            "deleting it disables crowdsecurity/http-probing"
        )

    def test_stat_task_exists_and_targets_the_same_path(self) -> None:
        stat_task = _task_named("Stat http-probing scenario path")
        assert stat_task["ansible.builtin.stat"]["path"] == (
            "/etc/crowdsec/scenarios/http-probing.yaml"
        )
        assert stat_task["register"] == "_http_probing_path"

    def test_override_still_deployed_when_enabled(self) -> None:
        """The opposite branch must keep writing our file when opted in."""
        task = _task_named("Deploy http-probing override (exclude 404)")
        assert task["ansible.builtin.template"]["dest"] == (
            "/etc/crowdsec/scenarios/http-probing.yaml"
        )


class TestCscliInstallChangedWhen:
    """`cscli ... install` must not report changed on a no-op.

    cscli >= 1.7 prints "Nothing to install or remove." to STDOUT and reserves
    stderr for its own log lines (the "new release available" warning). The
    original test looked only at stderr, for 'overwrite'/'already' markers that
    modern cscli never emits — so it could only ever evaluate True and these
    tasks reported changed on every provision, which is what kept `provision`
    from ever converging. Verified against live cscli v1.7.6:

        stdout: "Nothing to install or remove."
        stderr: level=warning msg="A new CrowdSec release is available ..."
    """

    @pytest.mark.parametrize(
        "task_name,register",
        [
            ("Install CrowdSec collections", "collection_install"),
            ("Install additional scenarios", "scenario_install"),
            ("Install additional parsers", "parser_install"),
        ],
    )
    def test_checks_stdout_for_the_noop_marker(
        self, task_name: str, register: str
    ) -> None:
        task = _task_named(task_name)
        cw = str(task["changed_when"])
        assert f"'Nothing to install or remove' not in {register}.stdout" in cw, (
            f"{task_name} tests only stderr for markers cscli no longer emits — "
            "it reports changed on every run"
        )

    @pytest.mark.parametrize(
        "task_name,register",
        [
            ("Install CrowdSec collections", "collection_install"),
            ("Install additional scenarios", "scenario_install"),
            ("Install additional parsers", "parser_install"),
        ],
    )
    def test_legacy_markers_kept_for_older_cscli(
        self, task_name: str, register: str
    ) -> None:
        task = _task_named(task_name)
        cw = str(task["changed_when"])
        assert f"'overwrite' not in {register}.stderr" in cw
        assert f"'already' not in {register}.stderr" in cw


class TestRegisterGuardStaysSubstring:
    """The substring guard is accidentally load-bearing — keep it.

    Every production host runs a bouncer named `cs-firewall-bouncer-<epoch>`,
    self-registered by the Debian package's postinst, which also writes its own
    key. A bouncer named exactly `firewall-bouncer` exists nowhere. The
    substring test matches inside the package's name, so Register never fires
    and bay's block stays the fallback it is meant to be.

    Tightening this to an exact match would look more rigorous and would mint a
    second bouncer on every host, rewrite the api_key, restart the bouncers,
    and orphan the package's registration with a stale last_pull.
    """

    def test_guard_is_a_substring_test(self) -> None:
        task = _task_named("Register firewall bouncer with LAPI")
        when = [str(c) for c in task["when"]]
        assert "'firewall-bouncer' not in bouncer_list.stdout" in when
