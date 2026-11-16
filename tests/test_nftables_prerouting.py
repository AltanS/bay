"""The nftables pre-routing extension point (GH#4).

`nftables_prerouting_rules` and `nftables_drop_ipv4_options` render into
/etc/nftables.d/bay-prerouting.conf as `table inet bay_prerouting` (hook
prerouting, priority -350). The file loads on its own with `nft -f`, so a
change must never reach the reload handler: that handler restarts Docker.

Context: CVE-2026-98432 needed an emergency pre-routing drop rule, and Bay had
no place for it, so it was hand-placed on every host.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from helpers import make_ansible_env, run_role_tasks

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "filter_plugins"))
from bay_filters import bay_gateway_interface  # noqa: E402

_ROOT = Path(__file__).parent.parent
_ROLE = _ROOT / "roles" / "nftables"
_TASKS = _ROLE / "tasks" / "main.yml"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_CROWDSEC_TASKS = _ROOT / "roles" / "crowdsec" / "tasks" / "main.yml"
_EGRESS_TASKS = _ROOT / "roles" / "container_lifecycle" / "tasks" / "egress.yml"

_DEST = "/etc/nftables.d/bay-prerouting.conf"
_DEPLOY = "Deploy nftables pre-routing rules"
_PROBE = "Check the live pre-routing table"
_APPLY = "Apply nftables pre-routing rules"
_FIND = "Find files in /etc/nftables.d that Bay does not manage"
_IPV4_OPTS_RULE = "ip protocol tcp ip hdrlength != 5 counter drop"


def _render(**variables) -> str:
    env = make_ansible_env(_ROLE / "templates")
    env.filters["bool"] = lambda v: (
        v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    )
    env.filters["bay_gateway_interface"] = bay_gateway_interface
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    ctx = {**defaults, "ansible_managed": "Ansible managed", **variables,
           "inventory_hostname": "box"}
    ctx["hostvars"] = {"box": dict(ctx)}
    # A default that is a template (nftables_tailnet_guard_iifname) renders
    # against the other values, as Ansible would.
    for key, value in list(ctx.items()):
        if key not in variables and isinstance(value, str) and "{{" in value:
            ctx[key] = env.from_string(value).render(**ctx)
    return env.get_template("bay-prerouting.conf.j2").render(**ctx)


def _tasks() -> list[dict]:
    return yaml.safe_load(_TASKS.read_text())


def _task(name: str) -> dict:
    for task in _tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in {_TASKS}")


def _rule_lines(text: str) -> list[str]:
    """The statements inside the prerouting chain, comments dropped."""
    body = text.split("type filter hook prerouting", 1)[1].split("\n", 1)[1]
    lines = [ln.strip() for ln in body.splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#") and ln != "}"]


# ── Defaults ────────────────────────────────────────────────────────────────


def test_defaults_drop_ipv4_options_and_add_no_rules() -> None:
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert defaults["nftables_drop_ipv4_options"] is True
    assert defaults["nftables_prerouting_rules"] == []


# ── Template ────────────────────────────────────────────────────────────────


def test_default_render_is_one_bay_table_at_prerouting_minus_350() -> None:
    text = _render()
    # create-then-delete: the file replaces only this table on `nft -f`.
    assert "table inet bay_prerouting {}\ndelete table inet bay_prerouting\n" in text
    assert "type filter hook prerouting priority -350; policy accept;" in text
    assert _rule_lines(text) == [_IPV4_OPTS_RULE]
    # Never touches any other table, Docker's least of all.
    tables = {ln.split("{")[0].strip() for ln in text.splitlines() if ln.startswith("table ")}
    assert tables == {"table inet bay_prerouting"}
    assert "flush ruleset" not in text


def test_drop_ipv4_options_false_leaves_an_empty_accept_chain() -> None:
    text = _render(nftables_drop_ipv4_options=False)
    assert _rule_lines(text) == []
    assert "policy accept;" in text


def test_operator_rules_render_in_order_with_comments() -> None:
    rules = [
        {"comment": "first hostile range", "rule": "ip saddr 192.0.2.0/24 counter drop"},
        {"rule": "ip6 saddr 2001:db8::/32 counter drop"},
    ]
    text = _render(nftables_prerouting_rules=rules)
    assert _rule_lines(text) == [
        _IPV4_OPTS_RULE,
        "ip saddr 192.0.2.0/24 counter drop",
        "ip6 saddr 2001:db8::/32 counter drop",
    ]
    assert "        # first hostile range\n        ip saddr 192.0.2.0/24 counter drop\n" in text


def _nft_check_available() -> bool:
    if shutil.which("nft") is None or shutil.which("unshare") is None:
        return False
    probe = subprocess.run(["unshare", "-rn", "nft", "list", "ruleset"], capture_output=True)
    return probe.returncode == 0


@pytest.mark.skipif(not _nft_check_available(), reason="needs nft and an unprivileged netns")
@pytest.mark.parametrize(
    "variables",
    [
        {},
        {"nftables_drop_ipv4_options": False},
        {"nftables_prerouting_rules": [{"comment": "x", "rule": "ip saddr 192.0.2.0/24 counter drop"}]},
    ],
)
def test_render_loads_twice_in_a_scratch_netns(tmp_path: Path, variables: dict) -> None:
    """The real `nft` accepts the file, and a second load replaces it cleanly."""
    conf = tmp_path / "bay-prerouting.conf"
    conf.write_text(_render(**variables))
    result = subprocess.run(
        ["unshare", "-rn", "sh", "-c", f"nft -c -f {conf} && nft -f {conf} && nft -f {conf}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# ── Tasks ───────────────────────────────────────────────────────────────────


def test_template_is_checked_by_nft_before_it_is_written() -> None:
    """nftables.conf includes the file at boot; a bad rule there would fail the
    whole ruleset load and leave the host with no firewall."""
    tmpl = _task(_DEPLOY)["ansible.builtin.template"]
    assert tmpl["src"] == "bay-prerouting.conf.j2"
    assert tmpl["dest"] == _DEST
    assert tmpl["validate"] == "nft -c -f %s"


def test_no_prerouting_task_notifies_the_docker_restarting_reload() -> None:
    for name in (_DEPLOY, _PROBE, _APPLY):
        assert "notify" not in _task(name), (
            f"{name!r} must not notify a handler: the nftables reload handler "
            "restarts Docker, and a pre-routing change needs no restart"
        )


def test_apply_loads_only_the_prerouting_file() -> None:
    apply = _task(_APPLY)
    assert apply["ansible.builtin.command"]["cmd"] == f"nft -f {_DEST}"
    when = apply["when"]
    assert "_nftables_prerouting_file is changed" in when
    # A host whose live table is missing (never reloaded since the file
    # landed, or flushed by hand) is repaired without a file change.
    assert "_nftables_prerouting_live.rc != 0" in when


def test_live_probe_is_readonly_and_runs_in_check_mode() -> None:
    probe = _task(_PROBE)
    assert probe["ansible.builtin.command"]["cmd"] == "nft list table inet bay_prerouting"
    assert probe["changed_when"] is False
    assert probe["failed_when"] is False
    assert probe["check_mode"] is False


def test_prerouting_lands_before_nftables_conf_is_written() -> None:
    """On a first install the reload of nftables.conf then includes the file."""
    names = [t.get("name") for t in _tasks()]
    assert names.index(_DEPLOY) < names.index(_APPLY) < names.index("Deploy nftables configuration")


def test_unmanaged_file_scan_excludes_exactly_the_bay_owned_files() -> None:
    find = _task(_FIND)["ansible.builtin.find"]
    assert find["paths"] == "/etc/nftables.d"
    owned = {Path(_DEST).name}
    for task in yaml.safe_load(_CROWDSEC_TASKS.read_text()):
        dest = (task.get("ansible.builtin.template") or {}).get("dest", "")
        if dest.startswith("/etc/nftables.d/"):
            owned.add(Path(dest).name)
    # The egress firewall (container_lifecycle) names its file in a block var.
    for task in yaml.safe_load(_EGRESS_TASKS.read_text()):
        dest = (task.get("vars") or {}).get("_egress_conf", "")
        if dest.startswith("/etc/nftables.d/"):
            owned.add(Path(dest).name)
    assert set(find["excludes"]) == owned, (
        "every file a Bay role writes into /etc/nftables.d must be excluded "
        "from the unmanaged-file warning, and nothing else"
    )


# ── Tailnet address guard (nftables_tailnet_ip_guard) ───────────────────────

_GUARD_JUMP = "ip daddr 100.64.0.6 jump tailnet_dst"


def _guard_chain(text: str) -> list[str]:
    body = text.split("chain tailnet_dst {", 1)[1].split("}", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_guard_is_off_by_default_and_renders_nothing() -> None:
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert defaults["nftables_tailnet_ip_guard"] is False
    text = _render(nftables_tailnet_guard_ip="100.64.0.6")
    assert "tailnet_dst" not in text


def test_guard_drops_the_tailnet_address_off_the_tailnet() -> None:
    text = _render(
        nftables_tailnet_ip_guard=True, nftables_tailnet_guard_ip="100.64.0.6",
        access_gateway="headscale",
    )
    assert _GUARD_JUMP in _rule_lines(text)
    assert _guard_chain(text) == [
        'iifname "tailscale0" return',
        'iifname "lo" return',
        'iifname "docker*" return',
        'iifname "br-*" return',
        'iifname "veth*" return',
        "counter drop",
    ]


def test_guard_reads_the_gateway_bind_ip_through_the_resolver() -> None:
    env = make_ansible_env(_ROLE / "templates")
    env.filters["bool"] = lambda v: (
        v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    )
    env.filters["bay_gateway_bind_ip"] = lambda hv: (hv or {}).get("gateway_bind_ip", "")
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    ctx = {
        **defaults,
        "ansible_managed": "x",
        "nftables_tailnet_ip_guard": True,
        "inventory_hostname": "box",
        "hostvars": {"box": {"gateway_bind_ip": "100.64.0.6"}},
    }
    text = env.get_template("bay-prerouting.conf.j2").render(**ctx)
    assert _GUARD_JUMP in text


def test_guard_without_an_address_fails_the_play_first() -> None:
    task = _task("Check the tailnet address guard has an address")
    assert task["when"] == "nftables_tailnet_ip_guard | default(false) | bool"
    names = [t.get("name") for t in _tasks()]
    assert names.index("Check the tailnet address guard has an address") < names.index(_DEPLOY)


@pytest.mark.skipif(not _nft_check_available(), reason="needs nft and an unprivileged netns")
def test_guard_loads_in_a_scratch_netns(tmp_path: Path) -> None:
    conf = tmp_path / "bay-prerouting.conf"
    conf.write_text(_render(
        nftables_tailnet_ip_guard=True, nftables_tailnet_guard_ip="100.64.0.6",
        access_gateway="headscale",
    ))
    result = subprocess.run(
        [
            "unshare", "-rn", "sh", "-c",
            f"nft -c -f {conf} && nft -f {conf} && nft -f {conf}"
            " && nft list table inet bay_prerouting",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "jump tailnet_dst" in result.stdout


# The assert runs under ansible-core's strict conditionals: `a or b` on two
# strings is a string, and core >= 2.19 refuses that ("Conditionals must have
# a boolean result"). A live test-box run caught it; these run the real task.
@pytest.mark.parametrize(
    ("variables", "passes"),
    [
        ({"nftables_tailnet_ip_guard": True, "gateway_bind_ip": "100.64.0.6"}, True),
        ({"nftables_tailnet_ip_guard": True, "nftables_tailnet_guard_ip": "100.64.0.6"}, True),
        ({"nftables_tailnet_ip_guard": True, "headscale_server_tailnet_ip": "100.64.0.6"}, True),
        ({"nftables_tailnet_ip_guard": True}, False),
        ({"nftables_tailnet_ip_guard": True, "gateway_bind_ip": ""}, False),
    ],
)
def test_guard_address_assert_runs_under_strict_conditionals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variables: dict, passes: bool
) -> None:
    monkeypatch.setenv("ANSIBLE_FILTER_PLUGINS", str(_ROOT / "filter_plugins"))
    result = run_role_tasks(
        tmp_path, _TASKS,
        ["Check the tailnet address guard has an address"], variables,
    )
    out = result.stdout + result.stderr
    assert "Conditionals must have a boolean result" not in out
    if passes:
        assert result.returncode == 0, out
    else:
        assert result.returncode != 0
        assert "this host has no tailnet address" in out


_IIF_TASK = "Check the tailnet address guard knows the overlay interface"
_KEEP_TASK = "Refuse to drop or move the guard of a box that serves tailnet hosts"


def _iif_default() -> str:
    return yaml.safe_load(_DEFAULTS.read_text())["nftables_tailnet_guard_iifname"]


@pytest.mark.parametrize(
    ("variables", "passes"),
    [
        ({"access_gateway": "headscale"}, True),
        ({"access_gateway": "wireguard"}, False),
        ({}, False),  # the access_gateway role default is wireguard
        ({"access_gateway": "wireguard", "nftables_tailnet_guard_iifname": "wg0"}, True),
        ({"access_gateway": "headscale", "nftables_tailnet_guard_iifname": "eth0 accept"}, False),
    ],
)
def test_guard_needs_a_known_overlay_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variables: dict, passes: bool
) -> None:
    monkeypatch.setenv("ANSIBLE_FILTER_PLUGINS",
                       str(Path(__file__).resolve().parent.parent / "filter_plugins"))
    result = run_role_tasks(
        tmp_path, _TASKS, [_IIF_TASK],
        {"nftables_tailnet_ip_guard": True,
         "nftables_tailnet_guard_iifname": _iif_default(), **variables},
    )
    out = result.stdout + result.stderr
    assert "Conditionals must have a boolean result" not in out
    assert (result.returncode == 0) is passes, out
    if not passes:
        assert "does not know the" in out


_TAILNET_SVC = {"app": {"tailnet_domains": ["admin.example.com"]}}


@pytest.mark.parametrize(
    ("variables", "passes"),
    [
        # Serves a tailnet host: the guard stays on, for gateway_bind_ip.
        ({"services": _TAILNET_SVC, "nftables_tailnet_ip_guard": True,
          "gateway_bind_ip": "100.64.0.6"}, True),
        ({"services": _TAILNET_SVC, "nftables_tailnet_ip_guard": False,
          "gateway_bind_ip": "100.64.0.6"}, False),
        ({"services": _TAILNET_SVC, "nftables_tailnet_ip_guard": True,
          "gateway_bind_ip": "100.64.0.6", "nftables_tailnet_guard_ip": "100.64.0.9"}, False),
        ({"services": _TAILNET_SVC, "nftables_tailnet_ip_guard": True,
          "gateway_bind_ip": "100.64.0.6", "nftables_tailnet_guard_ip": "100.64.0.6"}, True),
        # A tailnet host of another box does not hold this one.
        ({"services": {"app": {"tailnet_domains": ["a.example.com"], "regions": ["eu2"]}},
          "nftables_tailnet_ip_guard": False}, True),
        ({"services": {}, "nftables_tailnet_ip_guard": False}, True),
    ],
)
def test_guard_cannot_be_dropped_under_a_tailnet_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variables: dict, passes: bool
) -> None:
    monkeypatch.setenv("ANSIBLE_FILTER_PLUGINS", str(_ROOT / "filter_plugins"))
    result = run_role_tasks(tmp_path, _TASKS, [_KEEP_TASK], variables)
    out = result.stdout + result.stderr
    assert "Conditionals must have a boolean result" not in out
    assert (result.returncode == 0) is passes, out
    if not passes:
        assert "so the tailnet address guard must stay on" in out


def test_guard_sudoers_rule_allows_one_read_command() -> None:
    task = _task("Let the app user read the pre-routing table")
    content = task["ansible.builtin.copy"]["content"]
    rule = [line for line in content.splitlines() if not line.startswith("#")]
    assert rule == [
        "{{ app_user }} ALL=(root) NOPASSWD: {{ nftables_nft_path }} list table inet bay_prerouting"
    ]
    assert task["ansible.builtin.copy"]["validate"] == "visudo -cf %s"
    assert task["when"] == "nftables_tailnet_ip_guard | default(false) | bool"
    off = _task("Remove the pre-routing read rule when the guard is off")
    assert off["ansible.builtin.file"]["state"] == "absent"



def test_guard_sudoers_rule_renders_under_ansible(tmp_path: Path) -> None:
    """Run the real copy task (ansible_managed is undefined in a copy task, which
    a live test-box run caught). visudo checks the file when it is installed."""
    out_file = tmp_path / "bay-nft-guard-read"

    def transform(task: dict) -> dict:
        task = {k: v for k, v in task.items() if k not in ("become", "become_user")}
        copy = dict(task["ansible.builtin.copy"])
        copy["dest"] = str(out_file)
        copy.pop("owner")
        copy.pop("group")
        if not shutil.which("visudo"):
            copy.pop("validate")
        task["ansible.builtin.copy"] = copy
        return task

    result = run_role_tasks(
        tmp_path, _TASKS, ["Let the app user read the pre-routing table"],
        {"nftables_tailnet_ip_guard": True, "app_user": "deploy",
         "nftables_nft_path": "/usr/sbin/nft"},
        transform=transform,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert out_file.read_text().splitlines()[-1] == (
        "deploy ALL=(root) NOPASSWD: /usr/sbin/nft list table inet bay_prerouting"
    )


@pytest.mark.parametrize(
    ("hostvars", "want"),
    [
        ({"access_gateway": "headscale"}, "tailscale0"),
        ({"access_gateway": "wireguard"}, ""),
        ({}, ""),
        ({"access_gateway": "none"}, ""),
        ({"access_gateway": "wireguard", "gateway_interface": "wg0"}, "wg0"),
        (None, ""),
    ],
)
def test_guard_interface_follows_the_gateway_adapter(hostvars, want) -> None:
    """The guard lets through tailscale0 for headscale only; any other backend
    has no known interface, and the role then refuses the guard."""
    assert bay_gateway_interface(hostvars) == want


_LIVE_TABLE = """table inet bay_prerouting {
	chain tailnet_dst {
		iifname "tailscale0" return
		counter packets 0 bytes 0 drop
	}

	chain prerouting {
		type filter hook prerouting priority -350; policy accept;
		ip daddr ADDR jump tailnet_dst
	}
}
"""


@pytest.mark.parametrize(
    ("guard", "live_rc", "live_addr", "applies"),
    [
        (True, 0, "100.64.0.6", False),   # live table holds the guard: nothing to do
        (True, 0, "100.64.0.7", True),    # drift (a hand edit): reload the file
        (True, 1, "", True),              # table missing
        (False, 0, "100.64.0.7", False),  # guard off: the live guard is not checked
    ],
)
def test_apply_repairs_a_live_guard_that_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard: bool, live_rc: int,
    live_addr: str, applies: bool,
) -> None:
    """A live test-box run showed `--tags nftables` did not repair a hand-edited
    guard (the file had not changed), while the deploy's refusal told the
    operator to run exactly that."""
    monkeypatch.setenv("ANSIBLE_FILTER_PLUGINS",
                       str(Path(__file__).resolve().parent.parent / "filter_plugins"))
    fake = tmp_path / "bin"
    fake.mkdir()
    marker = tmp_path / "nft-ran"
    (fake / "nft").write_text(f"#!/bin/sh\necho \"$*\" > {marker}\n")
    (fake / "nft").chmod(0o755)

    def transform(task: dict) -> dict:
        return {k: v for k, v in task.items() if k not in ("become", "become_user")}

    stdout = _LIVE_TABLE.replace("ADDR", live_addr) if live_rc == 0 else ""
    result = run_role_tasks(
        tmp_path, _TASKS, [_APPLY],
        {"nftables_tailnet_ip_guard": guard, "gateway_bind_ip": "100.64.0.6",
         "_nftables_prerouting_file": {"changed": False},
         "_nftables_prerouting_live": {"rc": live_rc, "stdout": stdout}},
        path_dir=fake, transform=transform,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert marker.exists() is applies, out
