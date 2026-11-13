"""Egress networks: one Docker network per ``egress`` container (bay.toml).

Runs on the box, before the reconciler pass (``ensure``) and after it
(``prune``), from ``roles/container_lifecycle/tasks/egress.yml``. Ansible
ships this one file with ``ansible.builtin.script``, so it imports nothing
from its package and nothing outside the standard library except ``docker``,
and that only in :func:`main`.

Each network is named ``services-egress-<container>``, IPv4 only, with a /28
from ``bay_egress_subnet_pool`` and the Linux bridge name
``bayeg<first 8 hex of sha256(container)>``, so the nftables rules match one
stable interface. The egress container sits at ``.2`` (outside the dynamic
range ``.8/29``); its clients get an address from that range.

Subnet choice: the first /28 is picked by the hash of the network name, then
the search walks forward (and wraps) past every subnet a Docker network or a
host route already covers. Once a network exists its subnet is read back from
Docker and never chosen again, so a later change in the pool or in the other
networks never moves a running egress container.

``ensure`` prints one JSON document on stdout:
``{"networks": {<container>: {network, bridge, subnet, gateway, address,
created}}}``. ``--dry-run`` creates nothing and reports what it would pick
(``created`` is then the word ``would``). ``prune`` removes the networks this
stack created that are no longer wanted and that no container uses, and
prints ``{"removed": [...], "kept": [...]}``.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: Label on every network this module creates; the value is the egress container.
LABEL = "bay.egress"
#: Label naming the stack, so two stacks on one box never prune each other's networks.
STACK_LABEL = "bay.egress.stack"
NAME_PREFIX = "services-egress-"
BRIDGE_PREFIX = "bayeg"
BRIDGE_OPTION = "com.docker.network.bridge.name"
PREFIX_LEN = 28


#: IPv4 ranges an egress container may never reach: "this network", private
#: (RFC 1918), the tailnet and carrier-grade NAT (100.64/10, which holds
#: MagicDNS 100.100.100.100), loopback, link-local (cloud metadata),
#: IETF protocol assignments, benchmarking, and multicast plus the reserved
#: rest (224/3). The box firewall adds every box's public address. The
#: bay.toml validator refuses a ``dns`` resolver inside one of them.
BLOCKED_V4 = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/3",
)


def blocked_v4(extra: Iterable[object] = ()) -> list[str]:
    """:data:`BLOCKED_V4` plus every IPv4 address or network in ``extra``, merged.

    Values that are not IPv4 (host names, IPv6, empty strings) are skipped:
    the egress firewall drops all IPv6 anyway, and a name cannot go into an
    nftables set. The result has no overlaps, so it loads into an interval set.
    """
    nets = [ipaddress.IPv4Network(n) for n in BLOCKED_V4]
    for raw in extra:
        try:
            net = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            continue
        if isinstance(net, ipaddress.IPv4Network):
            nets.append(net)
    return [str(n) for n in ipaddress.collapse_addresses(nets)]


def fleet_v4(candidates: Iterable[object], blocked_extra: Iterable[object] = ()) -> list[str]:
    """The box addresses that ``egress_fleet = "web"`` may open, sorted, each once.

    ``candidates`` are the same inventory values that feed :func:`blocked_v4`.
    Only a single IPv4 address counts: a host name, an IPv6 address, a
    network (``a.b.c.d/24``) and an empty value are skipped. So is an address
    inside :data:`BLOCKED_V4` (an inventory that names a box by its tailnet or
    private address must not open it) and an address inside any entry of
    ``blocked_extra``: a block always wins over the fleet opening.
    """
    extra: list[ipaddress.IPv4Network] = []
    for raw in blocked_extra:
        try:
            net = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            continue
        if isinstance(net, ipaddress.IPv4Network):
            extra.append(net)
    out: set[ipaddress.IPv4Address] = set()
    for raw in candidates:
        try:
            ip = ipaddress.ip_address(str(raw).strip())
        except ValueError:
            continue
        if not isinstance(ip, ipaddress.IPv4Address) or is_blocked_v4(str(ip)):
            continue
        if any(ip in net for net in extra):
            continue
        out.add(ip)
    return [str(ip) for ip in sorted(out)]


def is_blocked_v4(address: str) -> bool:
    """True when ``address`` is IPv4 and inside :data:`BLOCKED_V4`."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return isinstance(ip, ipaddress.IPv4Address) and any(
        ip in ipaddress.IPv4Network(n) for n in BLOCKED_V4
    )


class EgressNetError(RuntimeError):
    """A wanted network cannot be made or trusted. The deploy must stop."""


def network_name(container: str) -> str:
    """``services-egress-<container>``: sorts after ``services`` (docker's gateway tie-break)."""
    return f"{NAME_PREFIX}{container}"


def bridge_name(container: str) -> str:
    """``bayeg`` + 8 hex digits: 13 characters, under the 15-character interface limit."""
    return BRIDGE_PREFIX + hashlib.sha256(container.encode()).hexdigest()[:8]


def layout(subnet: ipaddress.IPv4Network) -> dict[str, str]:
    """Gateway ``.1``, egress container ``.2``, dynamic client range ``.8/29``."""
    base = subnet.network_address
    return {
        "subnet": str(subnet),
        "gateway": str(base + 1),
        "address": str(base + 2),
        "ip_range": str(ipaddress.IPv4Network((int(base) + 8, 29))),
    }


def candidates(
    pool: ipaddress.IPv4Network, prefix: int = PREFIX_LEN
) -> list[ipaddress.IPv4Network]:
    if pool.prefixlen > prefix:
        raise EgressNetError(f"the egress pool {pool} is smaller than one /{prefix}")
    return list(pool.subnets(new_prefix=prefix))


def allocate(
    name: str,
    pool: ipaddress.IPv4Network,
    used: Iterable[ipaddress.IPv4Network],
    prefix: int = PREFIX_LEN,
) -> ipaddress.IPv4Network:
    """The first free /28 of ``pool``, starting at the slot the name hashes to.

    Deterministic: the same name, pool and used set give the same subnet. A
    candidate is free when it overlaps none of ``used``.
    """
    slots = candidates(pool, prefix)
    taken = list(used)
    start = int(hashlib.sha256(name.encode()).hexdigest(), 16) % len(slots)
    for i in range(len(slots)):
        cand = slots[(start + i) % len(slots)]
        if not any(cand.overlaps(u) for u in taken):
            return cand
    raise EgressNetError(
        f"no free /{prefix} left in the egress pool {pool}; set bay_egress_subnet_pool "
        "to a larger or another private range"
    )


def host_routes(text: str) -> list[ipaddress.IPv4Network]:
    """The destinations of ``/proc/net/route`` (main table), the default route left out."""
    out: list[ipaddress.IPv4Network] = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8:
            continue
        try:
            dest = int(cols[1], 16).to_bytes(4, "little")
            mask = int(cols[7], 16).to_bytes(4, "little")
            net = ipaddress.IPv4Network(
                (ipaddress.IPv4Address(dest), str(ipaddress.IPv4Address(mask))), strict=False
            )
        except ValueError:
            continue
        if net.prefixlen:
            out.append(net)
    return out


def _subnets(attrs: Mapping[str, Any]) -> list[ipaddress.IPv4Network]:
    out: list[ipaddress.IPv4Network] = []
    for cfg in ((attrs.get("IPAM") or {}).get("Config") or []):
        try:
            net = ipaddress.ip_network(str((cfg or {}).get("Subnet") or ""), strict=False)
        except ValueError:
            continue
        if isinstance(net, ipaddress.IPv4Network):
            out.append(net)
    return out


def check_existing(attrs: Mapping[str, Any], container: str, stack: str) -> ipaddress.IPv4Network:
    """The subnet of a network that already carries the wanted name, if Bay can trust it.

    It must be the network Bay made for this container: the label, the stack,
    the bridge name, IPv4 only and one /28. Anything else stops the deploy:
    the firewall would guard an interface the container is not on.
    """
    name = network_name(container)
    labels = attrs.get("Labels") or {}
    options = attrs.get("Options") or {}
    if labels.get(LABEL) != container or labels.get(STACK_LABEL) != stack:
        raise EgressNetError(
            f"a Docker network named {name} exists that Bay did not create for {container}; "
            f"remove it (docker network rm {name}) and deploy again"
        )
    if options.get(BRIDGE_OPTION) != bridge_name(container):
        raise EgressNetError(
            f"the network {name} does not use the bridge {bridge_name(container)}, so the "
            f"egress firewall would not see it; remove it (docker network rm {name})"
        )
    if attrs.get("EnableIPv6"):
        raise EgressNetError(f"the network {name} has IPv6 on; remove it and deploy again")
    subnets = _subnets(attrs)
    if len(subnets) != 1 or subnets[0].prefixlen != PREFIX_LEN:
        raise EgressNetError(
            f"the network {name} does not have exactly one /{PREFIX_LEN} subnet; remove it "
            "and deploy again"
        )
    return subnets[0]


def plan_networks(
    containers: Sequence[str],
    existing: Sequence[Mapping[str, Any]],
    routes: Sequence[ipaddress.IPv4Network],
    pool: ipaddress.IPv4Network,
    stack: str,
) -> dict[str, dict[str, Any]]:
    """Per egress container: the network, bridge and subnet, read back or newly picked.

    ``existing`` is the attrs of every Docker network on the box. A network
    that exists keeps its subnet (``create`` false). A new one gets the first
    free /28 (``create`` true); subnets picked earlier in the same call count
    as used, so two new networks never collide. Containers are handled in
    sorted order so the result does not depend on the input order.
    """
    by_name = {str(a.get("Name")): a for a in existing}
    used: list[ipaddress.IPv4Network] = list(routes)
    for attrs in existing:
        used.extend(_subnets(attrs))
    out: dict[str, dict[str, Any]] = {}
    for container in sorted(set(containers)):
        name = network_name(container)
        if name in by_name:
            subnet = check_existing(by_name[name], container, stack)
            create = False
        else:
            subnet = allocate(name, pool, used)
            used.append(subnet)
            create = True
        out[container] = {
            "network": name,
            "bridge": bridge_name(container),
            **layout(subnet),
            "create": create,
        }
    return out


def prunable(
    existing: Sequence[Mapping[str, Any]], wanted: Iterable[str], stack: str
) -> list[str]:
    """Names of the networks this stack made that no wanted container needs."""
    keep = {network_name(c) for c in wanted}
    return sorted(
        str(a.get("Name"))
        for a in existing
        if (a.get("Labels") or {}).get(STACK_LABEL) == stack
        and LABEL in (a.get("Labels") or {})
        and str(a.get("Name")) not in keep
    )


# ── Docker side ────────────────────────────────────────────────────────────


def _create(client: Any, container: str, entry: Mapping[str, Any], stack: str) -> None:
    import docker

    pool = docker.types.IPAMPool(
        subnet=entry["subnet"], gateway=entry["gateway"], iprange=entry["ip_range"]
    )
    client.networks.create(
        entry["network"],
        driver="bridge",
        options={BRIDGE_OPTION: entry["bridge"]},
        ipam=docker.types.IPAMConfig(pool_configs=[pool]),
        enable_ipv6=False,
        labels={LABEL: container, STACK_LABEL: stack},
    )


def ensure(
    client: Any,
    containers: Sequence[str],
    pool: ipaddress.IPv4Network,
    stack: str,
    routes: Sequence[ipaddress.IPv4Network],
    *,
    dry_run: bool = False,
) -> dict[str, dict[str, Any]]:
    existing = [n.attrs for n in client.networks.list()]
    planned = plan_networks(containers, existing, routes, pool, stack)
    out: dict[str, dict[str, Any]] = {}
    for container, entry in planned.items():
        create = entry.pop("create")
        if create and not dry_run:
            _create(client, container, entry, stack)
            # Read it back: the subnet Docker holds is the one the firewall uses.
            subnet = check_existing(client.networks.get(entry["network"]).attrs, container, stack)
            if str(subnet) != entry["subnet"]:
                raise EgressNetError(
                    f"Docker gave {entry['network']} the subnet {subnet}, not {entry['subnet']}"
                )
        entry["created"] = ("would" if dry_run else True) if create else False
        out[container] = entry
    return out


def prune(client: Any, wanted: Sequence[str], stack: str) -> dict[str, list[str]]:
    existing = [n.attrs for n in client.networks.list()]
    removed: list[str] = []
    kept: list[str] = []
    for name in prunable(existing, wanted, stack):
        net = client.networks.get(name)
        if (net.attrs.get("Containers") or {}):
            kept.append(name)
            continue
        net.remove()
        removed.append(name)
    return {"removed": removed, "kept": kept}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="egressnet")
    parser.add_argument("action", choices=("ensure", "prune"))
    parser.add_argument(
        "--spec", required=True, help='{"containers": [...], "pool": ..., "stack": ...}'
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    spec = json.loads(args.spec)
    containers = [str(c) for c in spec.get("containers") or []]
    stack = str(spec.get("stack") or "bay")

    import docker

    client = docker.from_env()
    try:
        if args.action == "prune":
            result: dict[str, Any] = prune(client, containers, stack)
        else:
            pool = ipaddress.IPv4Network(str(spec["pool"]))
            with open("/proc/net/route", encoding="ascii") as handle:
                routes = host_routes(handle.read())
            result = {
                "networks": ensure(client, containers, pool, stack, routes, dry_run=args.dry_run)
            }
    except (EgressNetError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        print(f"egressnet: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
