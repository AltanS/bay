"""bay-cert-alert: the certificate expiry probe.

The script is rendered from the real template and run with bash against a temp
dir. Only the shared alert snippet is swapped for a stub that appends each
bay_notify call to a log. The fixture stores hold real self-signed certificates
made with openssl. The clock is moved with BAY_CERT_ALERT_NOW, the test-only
override the script honours, so no test needs a backdated certificate.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLE = _REPO_ROOT / "roles" / "outbound_monitor"
_TEMPLATE = _ROLE / "templates" / "bay-cert-alert.sh.j2"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402

_DAY = 86400
_KEY_MARKER = "PRIVATE-KEY-MUST-NEVER-LEAK"

_STUB_NOTIFY = r"""BAY_HOST="testhost"
bay_html_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
bay_notify() { printf '%s\t%s\n' "$1" "$(printf '%s' "$2" | tr '\n' '|')" >> "${NOTIFY_LOG}"; }
"""

# A fake `docker` on PATH: `ps -q` lists one id per container of
# $FAKE_DOCKER_LABELS (a JSON list of label maps), `inspect` prints the labels
# of each id as one JSON line. $FAKE_DOCKER_FAIL makes every call fail.
_FAKE_DOCKER = r"""#!/usr/bin/env bash
[[ -z "${FAKE_DOCKER_FAIL:-}" ]] || { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
echo "$*" >> "${FAKE_DOCKER_CALLS}"
case "$1" in
  ps) python3 -c 'import json,sys; [print("c%d" % i) for i, _ in enumerate(json.load(open(sys.argv[1])))]' "${FAKE_DOCKER_LABELS}" ;;
  inspect) python3 -c 'import json,sys; d = json.load(open(sys.argv[1])); [print(json.dumps(d[int(a[1:])])) for a in sys.argv[2:]]' "${FAKE_DOCKER_LABELS}" "${@:4}" ;;
  *) exit 2 ;;
esac
"""

#: Every name the tests use, served by one router, unless a test sets its own.
_SERVED = ["a.example.com", "b.example.com", "bad.example.com", "d.example.com",
           "ok.example.com", "p1.example.com", "p2.example.com", "w1.example.com",
           "w2.example.com", "www.example.com"]


def make_cert(workdir: Path, name: str, days: int, cn: str) -> bytes:
    key = workdir / f"{name}.key"
    pem = workdir / f"{name}.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(pem), "-days", str(days),
            "-subj", f"/CN={cn}",
        ],
        check=True,
        capture_output=True,
    )
    return pem.read_bytes()


def entry(pem: bytes, main: str, sans: list[str] | None = None) -> dict:
    return {
        "domain": {"main": main, "sans": sans or []},
        "certificate": base64.b64encode(pem).decode(),
        "key": base64.b64encode(_KEY_MARKER.encode()).decode(),
        "Store": "default",
    }


class Probe:
    def __init__(self, root: Path):
        self.root = root
        self.certs = root / "certs"
        self.certs.mkdir()
        self.state_dir = root / "state"
        self.cert_state = self.state_dir / "cert-alert"
        self.acme = root / "acme.json"
        self.acme_dns = root / "acme-dns.json"
        self.log = root / "notify.log"
        templates = root / "templates"
        templates.mkdir()
        shutil.copy(_TEMPLATE, templates / _TEMPLATE.name)
        (templates / "_notify.sh.j2").write_text(_STUB_NOTIFY)
        self.script = root / "bay-cert-alert"
        self.dynamic = root / "dynamic"
        self.dynamic.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(_FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.docker_labels = root / "docker-labels.json"
        self.docker_calls = root / "docker-calls.log"
        self.docker_fail = False
        self.routers([{"traefik.http.routers.all.rule":
                       " || ".join(f"Host(`{n}`)" for n in _SERVED)}])
        self.render()
        self.now = int(time.time())

    def routers(self, labels: list[dict]) -> None:
        """The label maps of the running containers the fake docker reports."""
        self.docker_labels.write_text(json.dumps(labels))

    def render(self, warn: int = 21, page: int = 7, expired: int = 7) -> None:
        env = make_ansible_env(self.root / "templates")
        text = env.get_template(_TEMPLATE.name).render(
            outbound_check_state_dir=str(self.state_dir),
            cert_alert_acme_files=[str(self.acme), str(self.acme_dns)],
            cert_alert_warn_days=warn,
            cert_alert_page_days=page,
            cert_alert_expired_days=expired,
            cert_alert_dynamic_dir=str(self.dynamic),
        )
        self.script.write_text(text)

    def cert(self, name: str, days: int, main: str = "a.example.com",
             sans: list[str] | None = None) -> dict:
        return entry(make_cert(self.certs, name, days, main), main, sans)

    def write_store(self, path: Path, *entries: dict, resolver: str = "le") -> None:
        path.write_text(json.dumps({resolver: {"Account": {}, "Certificates": list(entries)}}))

    def run(self, now: int | None = None, expect: int = 0) -> subprocess.CompletedProcess:
        env = dict(os.environ, NOTIFY_LOG=str(self.log),
                   BAY_CERT_ALERT_NOW=str(now if now is not None else self.now),
                   PATH=f"{self.bin}:{os.environ['PATH']}",
                   FAKE_DOCKER_LABELS=str(self.docker_labels),
                   FAKE_DOCKER_CALLS=str(self.docker_calls))
        if self.docker_fail:
            env["FAKE_DOCKER_FAIL"] = "1"
        proc = subprocess.run(
            ["bash", str(self.script)], capture_output=True, text=True,
            check=False, env=env,
        )
        assert proc.returncode == expect, proc.stderr
        return proc

    def alerts(self) -> list[tuple[str, str]]:
        if not self.log.exists():
            return []
        return [tuple(line.split("\t", 1)) for line in self.log.read_text().splitlines()]

    def states(self) -> dict[str, str]:
        if not self.cert_state.exists():
            return {}
        return {p.name: p.read_text() for p in sorted(self.cert_state.iterdir())}


@pytest.fixture
def probe(tmp_path) -> Probe:
    return Probe(tmp_path)


def test_rendered_script_parses(probe):
    proc = subprocess.run(["bash", "-n", str(probe.script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_no_files_is_silent_and_writes_nothing(probe):
    proc = probe.run()
    assert proc.stdout == "" and proc.stderr == ""
    assert probe.alerts() == []
    assert not probe.state_dir.exists()


def test_empty_store_is_silent(probe):
    probe.acme.write_text("")
    probe.acme_dns.write_text("{}")
    proc = probe.run()
    assert proc.stderr == ""
    assert probe.alerts() == []


def test_null_certificates_is_handled(probe):
    probe.acme.write_text(json.dumps({"le": {"Account": {}, "Certificates": None}}))
    proc = probe.run()
    assert proc.stderr == ""
    assert probe.alerts() == []


def test_healthy_certificate_sends_nothing(probe):
    probe.write_store(probe.acme, probe.cert("ok", 60))
    probe.run()
    assert probe.alerts() == []
    assert probe.states() == {}


def test_twenty_days_left_sends_one_warn_with_details(probe):
    probe.write_store(
        probe.acme, probe.cert("c", 20, "a.example.com", ["www.example.com", "<x>&y"])
    )
    probe.run()
    (alert_id, message), = probe.alerts()
    assert alert_id == "host.cert_expiry_warn"
    assert "testhost" in message
    assert "<code>a.example.com</code>" in message
    assert "www.example.com" in message
    assert "&lt;x&gt;&amp;y" in message and "<x>&y" not in message
    assert "20 days left" in message
    assert str(probe.acme) in message
    (state,) = probe.states().values()
    assert re.fullmatch(r"last_alert_day=\d{4}-\d\d-\d\d\n", state)


def test_five_days_left_sends_one_page_not_a_warn(probe):
    probe.write_store(probe.acme, probe.cert("c", 5))
    probe.run()
    (alert_id, message), = probe.alerts()
    assert alert_id == "host.cert_expiry_page"
    assert "5 days left" in message


def test_one_warn_and_one_page_message_per_run_listing_all_certs(probe):
    probe.write_store(
        probe.acme,
        probe.cert("w1", 20, "w1.example.com"),
        probe.cert("w2", 15, "w2.example.com"),
        probe.cert("p1", 3, "p1.example.com"),
        probe.cert("p2", 2, "p2.example.com"),
        probe.cert("ok", 80, "ok.example.com"),
    )
    probe.write_store(probe.acme_dns, probe.cert("d", 10, "d.example.com"), resolver="dns")
    probe.run()
    alerts = dict(probe.alerts())
    assert len(probe.alerts()) == 2
    warn, page = alerts["host.cert_expiry_warn"], alerts["host.cert_expiry_page"]
    for name in ("w1", "w2", "d"):
        assert f"{name}.example.com" in warn
    for name in ("p1", "p2"):
        assert f"{name}.example.com" in page
    assert "ok.example.com" not in warn + page
    assert str(probe.acme_dns) in warn
    assert len(probe.states()) == 5


def test_same_certificate_in_two_stores_counts_once(probe):
    shared = probe.cert("c", 5)
    probe.write_store(probe.acme, shared)
    probe.write_store(probe.acme_dns, shared, resolver="dns")
    probe.run()
    (_, message), = probe.alerts()
    assert message.count("a.example.com") == 1


def test_two_runs_on_the_same_day_send_one_alert(probe):
    probe.write_store(probe.acme, probe.cert("c", 5))
    probe.run()
    probe.run(now=probe.now + 3600)
    assert len(probe.alerts()) == 1


def test_next_day_alerts_again(probe):
    probe.write_store(probe.acme, probe.cert("c", 10))
    # Start at noon UTC so that +1 day is surely the next UTC date.
    noon = probe.now - probe.now % _DAY + _DAY // 2
    probe.run(now=noon)
    probe.run(now=noon + _DAY)
    assert len(probe.alerts()) == 2


def test_expired_over_the_limit_is_muted_and_never_alerts_again(probe):
    probe.write_store(probe.acme, probe.cert("c", 1))
    end = probe.now + _DAY
    probe.run(now=end + 3 * _DAY)
    (_, message), = probe.alerts()
    assert "expired 3 days ago" in message
    assert "orphan" in message and "restart traefik" in message
    probe.run(now=end + 8 * _DAY)
    (state,) = probe.states().values()
    assert "muted=1" in state
    probe.run(now=end + 9 * _DAY)
    probe.run(now=end + 40 * _DAY)
    assert len(probe.alerts()) == 1


def test_muted_state_wins_even_if_the_cert_looks_due(probe):
    probe.write_store(probe.acme, probe.cert("c", 5))
    probe.run()
    (name,) = probe.states()
    (probe.cert_state / name).write_text("muted=1\n")
    probe.run(now=probe.now + 2 * _DAY)
    assert len(probe.alerts()) == 1


def test_renewed_certificate_prunes_the_old_state_and_sends_nothing(probe):
    probe.write_store(probe.acme, probe.cert("old", 5))
    probe.run()
    (old_name,) = probe.states()
    probe.write_store(probe.acme, probe.cert("new", 80))
    probe.run(now=probe.now + 3600)
    assert probe.states() == {}
    assert old_name not in probe.states()
    assert len(probe.alerts()) == 1


def test_prune_leaves_unrelated_files_alone(probe):
    probe.write_store(probe.acme, probe.cert("ok", 80))
    probe.cert_state.mkdir(parents=True)
    (probe.cert_state / "notes.txt").write_text("keep")
    (probe.cert_state / "short.state").write_text("keep")
    probe.run()
    assert set(probe.states()) == {"notes.txt", "short.state"}


def test_unparseable_store_fails_the_run_with_no_alert(probe):
    probe.acme.write_text("{ this is not json")
    proc = probe.run(expect=1)
    assert "cannot parse" in proc.stderr
    assert probe.alerts() == []


def test_unparseable_store_does_not_prune_and_does_not_hide_the_other_store(probe):
    probe.write_store(probe.acme, probe.cert("c", 5))
    probe.run()
    state_before = probe.states()
    probe.acme.write_text("{ broken")
    probe.write_store(probe.acme_dns, probe.cert("d", 5, "d.example.com"), resolver="dns")
    probe.run(now=probe.now + _DAY, expect=1)
    assert set(state_before) <= set(probe.states())
    assert any("d.example.com" in m for _, m in probe.alerts())


def test_bad_certificate_entry_fails_the_run_but_others_still_alert(probe):
    broken = {"domain": {"main": "bad.example.com"}, "certificate": "bm90IGEgY2VydA==", "key": ""}
    probe.write_store(probe.acme, broken, probe.cert("c", 5))
    proc = probe.run(expect=1)
    assert "skipped a certificate entry" in proc.stderr
    assert len(probe.alerts()) == 1


def test_private_key_never_appears_anywhere(probe):
    probe.write_store(probe.acme, probe.cert("a", 5), probe.cert("b", 15, "b.example.com"))
    proc = probe.run()
    probe.run(now=probe.now + _DAY)
    key_b64 = base64.b64encode(_KEY_MARKER.encode()).decode()
    haystack = proc.stdout + proc.stderr + probe.log.read_text()
    haystack += "".join(probe.states().values())
    assert _KEY_MARKER not in haystack and key_b64 not in haystack
    assert "PRIVATE KEY" not in haystack


def test_state_file_is_parsed_never_sourced(probe, tmp_path):
    probe.write_store(probe.acme, probe.cert("c", 5))
    probe.run()
    (name,) = probe.states()
    marker = tmp_path / "pwned"
    (probe.cert_state / name).write_text(
        f"last_alert_day=$(touch {marker})\nmuted=1; touch {marker}\n"
    )
    probe.run(now=probe.now + 3600)
    assert not marker.exists()
    assert len(probe.alerts()) == 2, "garbage counts as no state"


def test_thresholds_come_from_the_variables(probe):
    probe.write_store(probe.acme, probe.cert("c", 30))
    probe.run()
    assert probe.alerts() == []
    probe.render(warn=40)
    probe.run()
    assert [i for i, _ in probe.alerts()] == ["host.cert_expiry_warn"]
    probe.render(warn=40, page=35)
    probe.run(now=probe.now + _DAY)
    assert [i for i, _ in probe.alerts()][-1] == "host.cert_expiry_page"


def test_bad_clock_override_fails_closed(probe):
    probe.write_store(probe.acme, probe.cert("c", 5))
    probe.run(now="x; touch /tmp/never", expect=1)  # type: ignore[arg-type]
    assert probe.alerts() == []


# ── Wiring ───────────────────────────────────────────────────────────────


# ── Only certificates a live router serves alert (2.23.0 review fix) ─────────


def test_orphan_certificate_is_logged_not_alerted(probe):
    """Traefik keeps a certificate that no router uses and never renews it."""
    probe.routers([{"traefik.http.routers.web.rule": "Host(`a.example.com`)"}])
    probe.write_store(probe.acme, probe.cert("o", 5, "gone.example.com"))
    proc = probe.run()
    assert probe.alerts() == []
    assert probe.states() == {}
    assert "gone.example.com" in proc.stdout
    assert "served by no router: no alert" in proc.stdout


def test_orphan_does_not_hide_a_served_certificate(probe):
    probe.routers([{"traefik.http.routers.web.rule":
                    "Host(`a.example.com`) && PathPrefix(`/x`)"}])
    probe.write_store(probe.acme, probe.cert("o", 5, "gone.example.com"),
                      probe.cert("a", 5, "a.example.com"))
    probe.run()
    (alert_id, message), = probe.alerts()
    assert alert_id == "host.cert_expiry_page"
    assert "a.example.com" in message and "gone.example.com" not in message


def test_a_san_served_by_a_router_counts(probe):
    probe.routers([{"traefik.http.routers.web.rule": "Host(`x.example.com`, `www.example.com`)"}])
    probe.write_store(probe.acme, probe.cert("c", 20, "a.example.com", ["www.example.com"]))
    probe.run()
    assert [a for a, _ in probe.alerts()] == ["host.cert_expiry_warn"]


def test_a_tls_domains_label_counts(probe):
    probe.routers([{
        "traefik.http.routers.ts.rule": "HostRegexp(`^.+\\.ts\\.example\\.com$`)",
        "traefik.http.routers.ts.tls.domains[0].main": "*.ts.example.com",
        "traefik.http.routers.ts.tls.domains[0].sans": "ts.example.com",
    }])
    probe.write_store(probe.acme_dns, probe.cert("w", 5, "*.ts.example.com", ["ts.example.com"]))
    probe.run()
    assert [a for a, _ in probe.alerts()] == ["host.cert_expiry_page"]


def test_a_host_rule_in_a_dynamic_file_counts(probe):
    probe.routers([])
    (probe.dynamic / "tailnet-proxies.yml").write_text(
        "http:\n  routers:\n    vault:\n      rule: \"Host(`Vault.TS.example.com`)\"\n"
    )
    probe.write_store(probe.acme_dns, probe.cert("w", 5, "*.ts.example.com"))
    probe.run()
    assert [a for a, _ in probe.alerts()] == ["host.cert_expiry_page"]


@pytest.mark.parametrize(("host", "served"), [
    ("vault.ts.example.com", True),
    ("ts.example.com", False),
    ("a.b.ts.example.com", False),
    ("vault.ts.example.com.evil.test", False),
])
def test_a_wildcard_covers_one_label(probe, host, served):
    probe.routers([{"traefik.http.routers.r.rule": f"Host(`{host}`)"}])
    probe.write_store(probe.acme_dns, probe.cert("w", 5, "*.ts.example.com"))
    proc = probe.run()
    assert bool(probe.alerts()) is served, proc.stdout


def test_hostsni_and_other_entrypoints_do_not_count(probe):
    probe.routers([{"traefik.tcp.routers.db.rule": "HostSNI(`a.example.com`)",
                    "traefik.http.services.a.loadbalancer.server.port": "80"}])
    probe.write_store(probe.acme, probe.cert("a", 5, "a.example.com"))
    proc = probe.run()
    assert probe.alerts() == []
    assert "served by no router" in proc.stdout


def test_docker_unreadable_alerts_every_due_certificate(probe):
    probe.docker_fail = True
    probe.write_store(probe.acme, probe.cert("o", 5, "gone.example.com"))
    proc = probe.run()
    assert [a for a, _ in probe.alerts()] == ["host.cert_expiry_page"]
    assert "cannot read the routers" in proc.stdout


def test_the_routers_are_read_only_when_a_certificate_is_due(probe):
    probe.write_store(probe.acme, probe.cert("ok", 60))
    probe.run()
    assert not probe.docker_calls.exists()
    probe.write_store(probe.acme, probe.cert("ok", 60), probe.cert("a", 5),
                      probe.cert("b", 6, "b.example.com"))
    probe.run()
    # One ps and one inspect for the whole run, however many certificates are due.
    assert probe.docker_calls.read_text().splitlines() == [
        "ps -q", "inspect --format {{json .Config.Labels}} c0",
    ]


def test_dynamic_dir_default_follows_traefik():
    defaults = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    assert defaults["cert_alert_dynamic_dir"] == "{{ stack_dir }}/dynamic"
    tasks = (_REPO_ROOT / "roles" / "traefik" / "tasks" / "main.yml").read_text()
    assert 'dest: "{{ stack_dir }}/dynamic/tailnet-proxies.yml"' in tasks


def test_call_sites_pass_literal_registered_ids():
    registry = yaml.safe_load((_REPO_ROOT / "alerts" / "registry.yml").read_text())
    assert registry["host.cert_expiry_warn"]["level"] == "warn"
    assert registry["host.cert_expiry_page"]["level"] == "critical"
    text = _TEMPLATE.read_text()
    for alert_id in ("host.cert_expiry_warn", "host.cert_expiry_page"):
        assert registry[alert_id]["enabled_by_default"] is True
        assert re.search(rf'^\s*bay_notify {re.escape(alert_id)} "\$\{{message\}}"', text, re.M)


def test_defaults_are_declared():
    defaults = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    assert defaults["cert_alert_enabled"] is True
    assert defaults["cert_alert_warn_days"] == 21
    assert defaults["cert_alert_page_days"] == 7
    assert defaults["cert_alert_expired_days"] == 7
    assert defaults["cert_alert_acme_files"] == [
        "{{ stack_dir }}/acme.json",
        "{{ stack_dir }}/acme-dns.json",
    ]


def test_units_and_tasks_are_wired():
    service = (_ROLE / "templates" / "bay-cert-alert.service.j2").read_text()
    assert "Type=oneshot" in service and "User=root" in service
    assert "ExecStart=/usr/local/bin/bay-cert-alert" in service
    timer = (_ROLE / "templates" / "bay-cert-alert.timer.j2").read_text()
    assert "OnCalendar=*-*-* " in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=30min" in timer
    tasks = (_ROLE / "tasks" / "main.yml").read_text()
    for needle in ("bay-cert-alert.sh.j2", "bay-cert-alert.timer", "cert_alert_enabled"):
        assert needle in tasks
