"""rebuild.sh refuses a recreate the deploy would refuse (2.23.0 review fixes).

A webhook rebuild recreates a container from the copy of its spec in
rebuild.sh. Before the release and before `docker rm` it checks, like
roles/deploy_stack/tasks/host_preflight.yml does for the deploy:

- a service with tailnet hosts needs the live tailnet address guard for this
  box's tailnet address (`ip daddr <addr> jump tailnet_dst` in chain
  prerouting, a drop in chain tailnet_dst);
- `cpus` must not be above the box's CPU count.

The bash check and the filter `bay_tailnet_guard_live` must agree.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "filter_plugins"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bay_filters import bay_tailnet_guard_live  # noqa: E402
from test_rebuild_config import _render_rebuild_sh  # noqa: E402

_ADDR = "100.64.0.6"

_TABLE = """table inet bay_prerouting {
	chain tailnet_dst {
		iifname "tailscale0" return
		iifname "lo" return
		counter packets 3 bytes 180 drop
	}

	chain prerouting {
		type filter hook prerouting priority -350; policy accept;
		ip daddr 100.64.0.6 jump tailnet_dst
		ip protocol tcp ip hdrlength != 5 counter packets 0 bytes 0 drop
	}
}
"""

_TABLES = {
    "guard": (_TABLE, _ADDR, True),
    "other_address": (_TABLE, "100.64.0.7", False),
    "prefix_address": (_TABLE.replace("100.64.0.6 jump", "100.64.0.60 jump"), _ADDR, False),
    "no_jump": (_TABLE.replace("\t\tip daddr 100.64.0.6 jump tailnet_dst\n", ""), _ADDR, False),
    "jump_in_wrong_chain": (
        _TABLE.replace("\t\tip daddr 100.64.0.6 jump tailnet_dst\n", "").replace(
            '\t\tiifname "lo" return\n',
            '\t\tiifname "lo" return\n\t\tip daddr 100.64.0.6 jump tailnet_dst\n',
        ),
        _ADDR,
        False,
    ),
    "no_drop": (_TABLE.replace("counter packets 3 bytes 180 drop", "accept"), _ADDR, False),
    "empty": ("", _ADDR, False),
    "no_address": (_TABLE, "", False),
}


def _service(**over) -> dict:
    svc = {
        "build": {"repo": "https://github.com/acme/app.git", "branch": "main",
                  "strategy": "remote"},
        "image": "zot.example.com/acme/app:latest",
        "access": "public",
        "domains": ["app.example.com"],
        "ports": {"internal": 8000},
    }
    svc.update(over)
    return svc


def _render(services: dict, **extra) -> str:
    extra.setdefault("gateway_bind_ip", _ADDR)
    extra.setdefault("traefik_split_entrypoints", True)
    extra.setdefault("nftables_tailnet_ip_guard", True)
    return _render_rebuild_sh(services, list(services), **extra)


def _function(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", script, re.DOTALL | re.MULTILINE)
    assert match, name
    return match.group(0)


def _run_preflight(script: str, env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess:
    """Run _preflight_recreate from the rendered script with fake sudo/nproc."""
    harness = "\n".join(
        [
            "set -euo pipefail",
            '_log() { printf "[rebuild] %s\\n" "$*"; }',
            '_record_failure() { printf "RECORD|%s|%s|%s\\n" "$1" "$2" "$3"; }',
            # The script hides sudo's stderr; record the call in a file.
            'sudo() { printf "SUDO %s\\n" "$*" >>"${SUDO_LOG}"; printf "%s" "${FAKE_TABLE}"; }',
            'nproc() { printf "%s\\n" "${FAKE_NPROC}"; }',
            _function(script, "_tailnet_guard_live"),
            _function(script, "_preflight_recreate"),
            "_preflight_recreate",
            'echo "PASSED"',
        ]
    )
    base = {"PATH": "/usr/bin:/bin", "SERVICE": "app", "SHA": "abc123def456",
            "NEEDS_TAILNET_GUARD": "", "TAILNET_GUARD_IP": "", "CPUS": "",
            "FAKE_TABLE": "", "FAKE_NPROC": "8", "SUDO_LOG": str(tmp_path / "sudo.log")}
    base.update(env)
    (tmp_path / "sudo.log").write_text("")
    proc = subprocess.run(["bash", "-c", harness], env=base, capture_output=True,
                          text=True, check=False)
    proc.sudo_calls = (tmp_path / "sudo.log").read_text()
    return proc


@pytest.fixture(scope="module")
def script() -> str:
    return _render({"app": _service(tailnet_domains=["admin.example.com"], cpus="2")})


@pytest.mark.parametrize("case", sorted(_TABLES))
def test_the_bash_guard_check_agrees_with_the_filter(script: str, case: str) -> None:
    table, addr, want = _TABLES[case]
    assert bay_tailnet_guard_live(table, addr) is want
    proc = subprocess.run(
        ["bash", "-c", _function(script, "_tailnet_guard_live") + '\n_tailnet_guard_live "$T" "$A"'],
        env={"PATH": "/usr/bin:/bin", "T": table, "A": addr},
        capture_output=True, text=True, check=False,
    )
    assert (proc.returncode == 0) is want, proc.stderr


def test_a_tailnet_service_with_the_guard_passes(script: str, tmp_path: Path) -> None:
    proc = _run_preflight(script, {"NEEDS_TAILNET_GUARD": "1", "TAILNET_GUARD_IP": _ADDR,
                                   "FAKE_TABLE": _TABLE}, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().endswith("PASSED")
    # The one command the sudoers rule allows.
    assert "SUDO -n /usr/sbin/nft list table inet bay_prerouting" in proc.sudo_calls


@pytest.mark.parametrize("case", ["other_address", "no_jump", "no_drop", "empty"])
def test_a_tailnet_service_without_the_guard_for_its_address_is_refused(
    script: str, case: str, tmp_path: Path
) -> None:
    table, addr, _ = _TABLES[case]
    proc = _run_preflight(script, {"NEEDS_TAILNET_GUARD": "1", "TAILNET_GUARD_IP": addr,
                                   "FAKE_TABLE": table}, tmp_path)
    assert proc.returncode == 1
    assert "PASSED" not in proc.stdout
    assert "[rebuild] Tailnet guard missing for app" in proc.stdout
    assert "RECORD|abc123def456|Tailnet guard|" in proc.stdout
    assert "--tags nftables" in proc.stdout


def test_a_service_without_tailnet_hosts_never_reads_the_firewall(
    script: str, tmp_path: Path
) -> None:
    proc = _run_preflight(script, {"FAKE_TABLE": ""}, tmp_path)
    assert proc.returncode == 0
    assert proc.sudo_calls == ""


@pytest.mark.parametrize(("cpus", "nproc", "passes"),
                         [("2", "8", True), ("8", "8", True), ("8.5", "8", False),
                          ("16", "8", False), ("0.5", "1", True)])
def test_cpus_above_the_cpu_count_is_refused(script: str, cpus: str, nproc: str,
                                             passes: bool, tmp_path: Path) -> None:
    proc = _run_preflight(script, {"CPUS": cpus, "FAKE_NPROC": nproc}, tmp_path)
    assert (proc.returncode == 0) is passes, proc.stdout + proc.stderr
    if not passes:
        assert "[rebuild] CPU limit too high for app" in proc.stdout
        assert "RECORD|abc123def456|CPU limit|" in proc.stdout


def test_the_per_service_block_sets_the_check_inputs(script: str) -> None:
    assert '  NEEDS_TAILNET_GUARD="1"' in script
    assert f'  TAILNET_GUARD_IP="{_ADDR}"' in script
    assert '  CPUS="2"' in script
    plain = _render({"app": _service()})
    assert '  NEEDS_TAILNET_GUARD=""' in plain
    assert '  TAILNET_GUARD_IP=""' in plain
    assert '  CPUS=""' in plain


def test_pull_only_blocks_set_the_check_inputs_too() -> None:
    services = {
        "app": _service(),
        "worker": {"image": "zot.example.com/acme/app:latest", "cpus": 1.5,
                   "tailnet_domains": ["w.example.com"], "domains": ["w.example.com"],
                   "access": "public", "ports": {"internal": 8001}},
    }
    rendered = _render(services)
    block = rendered[rendered.index("if [[ \"${SERVICE}\" == 'worker' ]]; then"):]
    block = block[: block.index("HEALTH_CHECK_TIMEOUT")]
    assert '  NEEDS_TAILNET_GUARD="1"' in block
    assert '  CPUS="1.5"' in block


def _path_section(rendered: str, start: str, stop: str) -> str:
    i = rendered.index(start)
    return rendered[i : rendered.index(stop, i)]


def test_both_recreate_paths_check_before_the_release_and_the_rm(script: str) -> None:
    # Pull path.
    pull = script[script.index('_log "Pulling ${IMAGE_REF} from registry"'):]
    assert pull.index("_preflight_recreate") < pull.index('_release_or_fail "${IMAGE_REF}"')
    assert pull.index("_preflight_recreate") < pull.index('docker rm "${SERVICE}"')
    # Build path.
    build = script[script.index("# ── Release, with the new commit's image"):]
    assert build.index("_preflight_recreate") < build.index('_release_or_fail "${IMAGE_NAME}:${SHA}"')
    assert build.index("_preflight_recreate") < build.index('docker rm "${SERVICE}"')


def test_a_tailnet_service_of_another_box_is_never_started_here() -> None:
    """A build server renders blocks for the services it builds for other
    boxes. With the guard off here, the labels of such a service cannot be
    made (bay_traefik_labels refuses), and it must never start here anyway."""
    services = {"app": _service(tailnet_domains=["admin.example.com"], regions=["eu2"])}
    rendered = _render(services, group_names=["infra"], nftables_tailnet_ip_guard=False,
                       traefik_split_entrypoints=False)
    run = rendered[rendered.index("_run_container() {"):]
    run = run[: run.index("\n  }\n")]
    assert "does not run on this box: not started here" in run
    assert "return 1" in run
    assert "docker" not in run
    assert "app-tailnet" not in rendered


def test_a_tailnet_service_of_this_box_gets_its_labels() -> None:
    services = {"app": _service(tailnet_domains=["admin.example.com"], regions=["eu2"])}
    rendered = _render(services, group_names=["eu2"])
    assert "-l 'traefik.http.routers.app-tailnet.entrypoints=websecure_tailnet'" in rendered
    assert "not started here" not in rendered


def test_the_script_still_parses(script: str) -> None:
    proc = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True,
                          check=False)
    assert proc.returncode == 0, proc.stderr
