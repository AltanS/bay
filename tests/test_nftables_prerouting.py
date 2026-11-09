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

from helpers import make_ansible_env

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
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    ctx = {**defaults, "ansible_managed": "Ansible managed", **variables}
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
