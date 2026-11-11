"""Filters of the host_upgrade role (``bay host upgrade``).

Pure functions over command output, so the decisions are testable without a
box: which packages an upgrade installs, whether the containers are back,
which published port to probe, whether a reboot is needed, and the list of
checks with a name and a verdict each (``tests/test_host_upgrade.py``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

#: ``apt-get -s dist-upgrade`` prints one ``Inst <pkg> [<old>] (<new> ...)`` per package.
_INST = re.compile(r"^Inst (\S+)")
#: A published TCP port on an IPv4 address: ``0.0.0.0:8080->80/tcp``.
_PORT = re.compile(r"(?:^|[ ,])(0\.0\.0\.0|127\.0\.0\.1):(\d+)->\d+/tcp")


def host_upgrade_packages(lines: Iterable[str] | None) -> list[str]:
    """The package names an ``apt-get -s dist-upgrade`` would install or upgrade."""
    out: list[str] = []
    for line in lines or []:
        match = _INST.match(str(line))
        if match and match.group(1) not in out:
            out.append(match.group(1))
    return out


def host_upgrade_containers(lines: Iterable[str] | None, before: Iterable[str] | None) -> dict[str, Any]:
    """The state of the containers that ran before, from ``docker ps`` lines.

    Each line is ``<name>\\t<status>``. A container is back when it is listed
    with a status that starts with ``Up``. ``starting`` holds those whose
    healthcheck has not answered yet, ``unhealthy`` those whose healthcheck
    failed. ``settled`` is True when no container is missing and none is still
    starting: the wait can stop. ``ok`` is True when every container is back
    and none is unhealthy or starting.
    """
    status: dict[str, str] = {}
    for line in lines or []:
        name, _, state = str(line).partition("\t")
        if name.strip():
            status[name.strip()] = state.strip()
    wanted = [str(n) for n in before or [] if str(n).strip()]
    missing, starting, unhealthy = [], [], []
    for name in wanted:
        state = status.get(name)
        if state is None or not state.startswith("Up"):
            missing.append(name)
        elif "(health: starting)" in state:
            starting.append(name)
        elif "(unhealthy)" in state:
            unhealthy.append(name)
    return {
        "running": sorted(status),
        "missing": missing,
        "starting": starting,
        "unhealthy": unhealthy,
        "settled": not missing and not starting,
        "ok": not missing and not starting and not unhealthy,
    }


def host_upgrade_published_port(lines: Iterable[str] | None) -> int:
    """The first TCP port a container publishes on 0.0.0.0 or 127.0.0.1, or 0.

    ``lines`` are ``docker ps --format '{{.Ports}}'`` lines. The lowest port
    wins, so the choice is stable between runs.
    """
    ports = sorted(
        int(m.group(2)) for line in lines or [] for m in _PORT.finditer(str(line))
    )
    return ports[0] if ports else 0


def _restart_table(lines: Iterable[str] | None) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for line in lines or []:
        parts = str(line).split("\t")
        if len(parts) < 3 or not parts[0].strip():
            continue
        try:
            count = int(parts[1].strip())
        except ValueError:
            continue
        out[parts[0].strip().lstrip("/")] = (count, parts[2].strip())
    return out


def host_upgrade_restarts(start: Iterable[str] | None, end: Iterable[str] | None) -> dict[str, Any]:
    """Containers in a restart loop during the wait window.

    ``start`` and ``end`` are ``docker inspect --format
    '{{.Name}}\t{{.RestartCount}}\t{{.State.Status}}'`` lines from the start
    and the end of the window. ``restarting`` holds the containers whose state
    is ``restarting`` at the end; ``grew`` those whose restart count grew in
    the window. A container in a loop can look ``Up`` at one sample, so the
    count is the stronger sign. ``looping`` is both, sorted, without repeats.
    """
    first, last = _restart_table(start), _restart_table(end)
    restarting = sorted(n for n, (_, status) in last.items() if status == "restarting")
    grew = sorted(n for n, (count, _) in last.items() if n in first and count > first[n][0])
    return {
        "restarting": restarting,
        "grew": grew,
        "looping": sorted(set(restarting) | set(grew)),
        "counts": {n: count for n, (count, _) in sorted(last.items())},
    }


def _version_key(text: str) -> list[Any]:
    return [(0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"(\d+)", text) if part]


def host_upgrade_newest_kernel(names: Iterable[str] | None) -> str:
    """The newest kernel release among the folder names of ``/lib/modules``, or ``""``."""
    found = [str(n).strip() for n in names or [] if str(n).strip()]
    return max(found, key=_version_key) if found else ""


def _probe_map(results: Iterable[Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for result in results or []:
        item = result.get("item") or {}
        name = item.get("name") if isinstance(item, Mapping) else None
        if not name:
            continue
        out[str(name)] = {
            "rc": result.get("rc", 1 if result.get("failed") else 0),
            "stdout": str(result.get("stdout") or ""),
            "skipped": bool(result.get("skipped")),
        }
    return out


def _installed(probes: Mapping[str, Mapping[str, Any]], name: str) -> bool:
    """A unit is installed when ``systemctl list-unit-files <unit>`` lists it."""
    probe = probes.get(name) or {}
    return probe.get("rc") == 0 and bool(probe.get("stdout", "").strip())


def _active(probes: Mapping[str, Mapping[str, Any]], name: str) -> bool:
    probe = probes.get(name) or {}
    return probe.get("rc") == 0 and probe.get("stdout", "").strip() == "active"


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def host_upgrade_checks(
    results: Iterable[Mapping[str, Any]] | None,
    containers: Mapping[str, Any] | None,
    want_tables: Iterable[str] | None,
    chains: Iterable[Mapping[str, Any]] | None,
    port: Mapping[str, Any] | None = None,
    restarts: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The post checks, one ``{"name", "ok", "detail"}`` each.

    ``results`` are the ``results`` of the probe loop (each item has a
    ``name``). ``containers`` is :func:`host_upgrade_containers`.
    ``want_tables`` are ``nft list tables`` lines that must be there.
    ``chains`` are the ``host_upgrade_docker_chains`` entries. ``port`` is
    ``{"port": n, "ok": bool}`` from the published-port probe (port 0: none).
    ``restarts`` is :func:`host_upgrade_restarts`: a container that is
    ``restarting`` or whose restart count grew fails ``container_restarts``.

    A check for software that is not installed is left out: crowdsec, the
    bouncer, tailscale and nftables are each checked only where their unit
    exists.
    """
    probes = _probe_map(results)
    checks: list[dict[str, Any]] = []

    docker_ok = _active(probes, "docker_active") and (probes.get("docker_info") or {}).get("rc") == 0
    checks.append(_check("docker", docker_ok, "docker is active and answers" if docker_ok
                         else "docker is not active, or `docker info` failed"))

    c = dict(containers or {})
    if c.get("ok", False):
        detail = "every container that ran before runs again"
    else:
        parts = [f"{key}: {', '.join(c.get(key) or [])}"
                 for key in ("missing", "starting", "unhealthy") if c.get(key)]
        detail = "; ".join(parts) or "the containers were not read"
    checks.append(_check("containers", bool(c.get("ok", False)), detail))

    r = dict(restarts or {})
    looping = list(r.get("looping") or [])
    if looping:
        counts = r.get("counts") or {}
        detail = "restart loop: " + ", ".join(
            f"{n} ({'restarting' if n in (r.get('restarting') or []) else 'restarted'}, count {counts.get(n, '?')})"
            for n in looping
        )
    else:
        detail = "no container restarted during the wait"
    checks.append(_check("container_restarts", not looping, detail))

    if _installed(probes, "nftables_unit"):
        active = _active(probes, "nftables_active")
        checks.append(_check("nftables", active, "nftables.service is active" if active
                             else "nftables.service is not active"))
        listed = {line.strip() for line in (probes.get("nft_tables") or {}).get("stdout", "").splitlines()}
        lost = [t for t in want_tables or [] if t not in listed]
        checks.append(_check("nftables_tables", not lost, "the Bay tables are loaded" if not lost
                             else f"missing: {', '.join(lost)}"))

    lost_chains = []
    for entry in chains or []:
        key = f"chain_{entry.get('table')}_{entry.get('chain')}"
        if (probes.get(key) or {}).get("rc") != 0:
            lost_chains.append(f"{entry.get('table')}/{entry.get('chain')}")
    checks.append(_check("docker_chains", not lost_chains, "Docker's chains are present" if not lost_chains
                         else f"missing: {', '.join(lost_chains)}"))

    if port and int(port.get("port") or 0) > 0:
        n = int(port["port"])
        checks.append(_check("published_port", bool(port.get("ok")),
                             f"127.0.0.1:{n} answers" if port.get("ok") else f"127.0.0.1:{n} does not answer"))

    for check, unit in (("crowdsec", "crowdsec"), ("crowdsec_bouncer", "bouncer"), ("tailscale", "tailscaled")):
        if not _installed(probes, f"{unit}_unit"):
            continue
        active = _active(probes, f"{unit}_active")
        detail = f"{unit} is active" if active else f"{unit} is not active"
        if check == "tailscale" and active:
            try:
                backend = json.loads((probes.get("tailscale_status") or {}).get("stdout") or "{}").get(
                    "BackendState", ""
                )
            except ValueError:
                backend = ""
            active = backend == "Running"
            address = (probes.get("tailscale_ip") or {}).get("stdout", "").strip().splitlines()
            address = address[0].strip() if address else ""
            active = active and bool(address)
            detail = (
                f"tailscaled is active, BackendState {backend or 'unknown'}, "
                f"address {address or 'none'}"
            )
        checks.append(_check(check, active, detail))
    return checks


def host_upgrade_chain_probes(chains: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """One probe per Docker chain: ``iptables -t <table> -n -L <chain>``, named ``chain_<table>_<chain>``."""
    return [
        {
            "name": f"chain_{c.get('table')}_{c.get('chain')}",
            "argv": ["iptables", "-t", str(c.get("table")), "-n", "-L", str(c.get("chain"))],
        }
        for c in chains or []
    ]


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {
            "host_upgrade_packages": host_upgrade_packages,
            "host_upgrade_containers": host_upgrade_containers,
            "host_upgrade_published_port": host_upgrade_published_port,
            "host_upgrade_newest_kernel": host_upgrade_newest_kernel,
            "host_upgrade_checks": host_upgrade_checks,
            "host_upgrade_chain_probes": host_upgrade_chain_probes,
            "host_upgrade_restarts": host_upgrade_restarts,
        }
