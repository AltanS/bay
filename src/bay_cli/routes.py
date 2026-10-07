"""Tailnet routes: the ``[tailnet.routes.<name>]`` table of ``bay.fleet.toml``.

A route gives a machine on the tailnet that Bay does not run (a laptop, a NAS)
a name with a trusted certificate. The ingress box terminates TLS with the
wildcard ``cert_domain`` and forwards to ``upstream`` over the tailnet::

    [tailnet]
    ingress_box = "infra"
    cert_domain = "*.ts.example.com"

    [tailnet.routes.notes]
    domain = "notes.ts.example.com"
    upstream = "http://laptop.acme.tailnet.internal:8080"
    host = "upstream"     # upstream | client (default client)
    identity = true       # inject X-Tailnet-Device
    aliases = []          # more names for the same route
    # entrypoint = "..."  # optional Traefik entrypoint

* **Compiled.** :func:`compile_routes` checks the table and returns the
  ``tailnet_proxies`` map that ``bay compile`` writes into ``services.yml``,
  with the keys the Traefik, Headscale and CrowdSec templates read
  (``domains``, ``upstream``, ``pass_host_header``, ``identity_inject``,
  ``entrypoint``). A key at its default is left out, so a route renders
  exactly as the same entry did in ``group_vars/all/tailnet_proxies.yml``.
* **Planned.** :func:`route_steps` diffs two compiled files: one step per
  route, kind ``route``, action ``route_added`` / ``route_changed`` /
  ``route_removed``, risk ``shared``. :func:`deploy_tags` adds the
  ``headscale`` and ``traefik`` tags to ``bay up`` when a route step is
  present.
* **Edited.** :func:`add_route_text`, :func:`remove_route_text` and
  :func:`set_tailnet_keys` change the fleet file with small text edits, so
  comments and order stay. Every edit is parsed back and compared with the
  intended document; a mismatch refuses instead of writing.
* **Imported.** :func:`from_proxies` turns the old YAML map into the table.
  The first entry of ``domains`` is ``domain``, the rest are ``aliases``.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bay_cli.errors import BayError, ErrorCode

#: The old hand-written file that the table replaces.
OLD_FILE = Path("group_vars") / "all" / "tailnet_proxies.yml"
#: The tags ``bay up`` runs when the plan has a route step. Headscale renders
#: the split-DNS records, Traefik the route file.
ROUTE_TAGS = ("headscale", "traefik")
HOST_CLIENT = "client"
HOST_UPSTREAM = "upstream"

#: Keys of one old ``tailnet_proxies`` entry.
_PROXY_KEYS = ("domains", "upstream", "pass_host_header", "identity_inject", "entrypoint")
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
#: MagicDNS names: Headscale's default base domain and Tailscale's.
_TAILNET_SUFFIXES = (".tailnet.internal", ".ts.net")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_HEADER_RE = re.compile(r"^[ \t]*\[(?!\[)(.*)\][ \t]*(#.*)?$")
_ARRAY_HEADER_RE = re.compile(r"^[ \t]*\[\[(.*)\]\][ \t]*(#.*)?$")


# ── reading the table ───────────────────────────────────────────────────────


def tailnet_of(fleet: Mapping[str, Any]) -> dict[str, Any]:
    raw = fleet.get("tailnet")
    return dict(raw) if isinstance(raw, Mapping) else {}


def table(fleet: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``[tailnet.routes]`` of a parsed fleet file, name -> route."""
    raw = tailnet_of(fleet).get("routes")
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, Mapping)}


def ingress_env(fleet: Mapping[str, Any]) -> str | None:
    """The env of the ingress box: the box env a route change deploys to."""
    box = tailnet_of(fleet).get("ingress_box")
    boxes = fleet.get("boxes") or {}
    entry = boxes.get(box) if isinstance(box, str) else None
    env = entry.get("env") if isinstance(entry, Mapping) else None
    return env if isinstance(env, str) else None


def under_cert(domain: str, cert_domain: str) -> bool:
    """True when the certificate for ``cert_domain`` covers ``domain``.

    A wildcard covers exactly one label below its base: ``*.ts.example.com``
    covers ``notes.ts.example.com``, not ``a.notes.ts.example.com``.
    """
    if not cert_domain.startswith("*."):
        return domain == cert_domain
    base = cert_domain[2:]
    label, dot, rest = domain.partition(".")
    return bool(dot) and rest == base and bool(_LABEL_RE.match(label))


def is_tailnet_host(host: str) -> bool:
    """A tailnet address (100.64.0.0/10, fd7a:115c:a1e0::/48) or a MagicDNS name.

    A MagicDNS name is a single label (``laptop``) or a name under
    ``.tailnet.internal`` (Headscale's default base domain) or ``.ts.net``.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return ip in _TAILNET_V4 if ip.version == 4 else ip in _TAILNET_V6
    labels = host.split(".")
    if not all(_LABEL_RE.match(label) for label in labels):
        return False
    return len(labels) == 1 or host.endswith(_TAILNET_SUFFIXES)


def parse_upstream(url: str) -> tuple[str, int] | str:
    """``(host, port)`` of an upstream URL, or a message that says what is wrong."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        return f"{url} is not a URL ({exc})"
    if parts.scheme not in ("http", "https"):
        return f"{url} must start with http:// or https://"
    if parts.username or parts.password or parts.query or parts.fragment:
        return f"{url} must be scheme, name and port only, such as http://laptop:8080"
    if parts.path not in ("", "/"):
        return f"{url} must have no path, such as http://laptop:8080"
    host = parts.hostname
    if not host:
        return f"{url} names no machine"
    if port is None:
        return f"{url} names no port; write it out, such as http://{host}:8080"
    if not is_tailnet_host(host):
        return (
            f"{url}: {host} is not a tailnet address or MagicDNS name "
            "(100.64.0.0/10, a single name such as laptop, or a name under "
            ".tailnet.internal or .ts.net)"
        )
    return host, port


# ── compile ─────────────────────────────────────────────────────────────────


def proxy_entry(route: Mapping[str, Any]) -> dict[str, Any]:
    """One route as a ``tailnet_proxies`` entry. Keys at their default are left out."""
    entry: dict[str, Any] = {
        "domains": [route["domain"], *route.get("aliases", [])],
        "upstream": route["upstream"],
    }
    if route.get("host", HOST_CLIENT) == HOST_UPSTREAM:
        entry["pass_host_header"] = False
    if route.get("identity", False):
        entry["identity_inject"] = True
    if route.get("entrypoint"):
        entry["entrypoint"] = route["entrypoint"]
    return entry


def compile_routes(
    fleet: Mapping[str, Any], claimed: dict[str, str] | None = None
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Check ``[tailnet.routes]`` and return ``(tailnet_proxies, errors)``.

    ``claimed`` maps a domain the fleet already serves to its owner (the
    compiler passes the project domains). Route domains are added to it, so
    a later check (the webhook) sees them too.
    """
    routes = table(fleet)
    if not routes:
        return {}, []
    seen = claimed if claimed is not None else {}
    errors: list[str] = []
    tailnet = tailnet_of(fleet)
    ingress = tailnet.get("ingress_box")
    cert = tailnet.get("cert_domain")
    boxes = fleet.get("boxes") or {}
    if not ingress:
        errors.append(
            "tailnet.ingress_box: routes need the box that serves them; set ingress_box"
        )
    elif ingress not in boxes:
        errors.append(f"tailnet.ingress_box: there is no [boxes.{ingress}]")
    if not cert:
        errors.append(
            'tailnet.cert_domain: routes need the certificate domain, such as '
            '"*.ts.example.com"; set cert_domain'
        )
    out: dict[str, dict[str, Any]] = {}
    for name in routes:  # file order: the route file renders in this order
        route = routes[name]
        where = f"tailnet.routes.{name}"
        owner = f"route {name}"
        domains = [str(route.get("domain", "")), *(str(a) for a in route.get("aliases", []))]
        for d in sorted({d for d in domains if domains.count(d) > 1}):
            errors.append(f"{where}: domain {d} is listed twice")
        for d in dict.fromkeys(domains):
            if isinstance(cert, str) and cert and not under_cert(d, cert):
                errors.append(
                    f"{where}: domain {d} is not under cert_domain {cert}; the "
                    "certificate covers one name below it"
                )
            first = seen.setdefault(d, owner)
            if first != owner:
                errors.append(f"domain {d} is used by both {first} and {owner}")
        parsed = parse_upstream(str(route.get("upstream", "")))
        if isinstance(parsed, str):
            errors.append(f"{where}.upstream: {parsed}")
        if "domain" in route and "upstream" in route:
            out[name] = proxy_entry(route)
    return out, errors


# ── import from the old YAML map ────────────────────────────────────────────


def from_proxies(proxies: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The old ``tailnet_proxies`` map as ``[tailnet.routes]``: ``(routes, problems)``.

    Names stay. ``domains[0]`` is ``domain``, the rest are ``aliases``.
    ``pass_host_header: false`` is ``host = "upstream"``;
    ``identity_inject: true`` is ``identity = true``. A key the table cannot
    hold, or a templated value, is a problem: the caller refuses.
    """
    routes: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for raw_name, proxy in proxies.items():  # file order, kept
        name = str(raw_name)
        where = f"tailnet_proxies.{name}"
        if not isinstance(proxy, Mapping):
            problems.append(f"{where}: is not a mapping")
            continue
        unknown = sorted(str(k) for k in proxy if k not in _PROXY_KEYS)
        if unknown:
            problems.append(f"{where}: {', '.join(unknown)} has no place in [tailnet.routes]")
        if "{{" in json.dumps(proxy, default=str):
            problems.append(f"{where}: holds a template; write the plain value first")
        domains = proxy.get("domains")
        if (
            not isinstance(domains, list)
            or not domains
            or not all(isinstance(d, str) for d in domains)
        ):
            problems.append(f"{where}.domains: must be a list of at least one name")
            continue
        upstream = proxy.get("upstream")
        if not isinstance(upstream, str):
            problems.append(f"{where}.upstream: is missing")
            continue
        route: dict[str, Any] = {"domain": domains[0], "upstream": upstream}
        for key, want in (("pass_host_header", bool), ("identity_inject", bool)):
            if key in proxy and not isinstance(proxy[key], want):
                problems.append(f"{where}.{key}: must be true or false")
        if proxy.get("pass_host_header") is False:
            route["host"] = HOST_UPSTREAM
        if proxy.get("identity_inject") is True:
            route["identity"] = True
        if domains[1:]:
            route["aliases"] = list(domains[1:])
        if proxy.get("entrypoint"):
            route["entrypoint"] = str(proxy["entrypoint"])
        routes[name] = route
    return routes, problems


def guess_ingress(
    group_vars: Mapping[str, Mapping[str, Any]], boxes: Mapping[str, Mapping[str, Any]]
) -> tuple[str | None, str | None, str]:
    """``(ingress_box, cert_domain, why)`` from today's group variables.

    The ingress box is the box of the one group that sets
    ``tailnet_ingress_cert_domain`` (the Traefik role reads it there). When
    only ``all`` sets it, ``headscale_control_region`` names the box. ``why``
    says what was found or what is missing.
    """
    found = {
        g: v["tailnet_ingress_cert_domain"]
        for g, v in group_vars.items()
        if isinstance(v.get("tailnet_ingress_cert_domain"), str)
        and "{{" not in v["tailnet_ingress_cert_domain"]
    }
    specific = {g: c for g, c in found.items() if g != "all"}

    def box_of(group: str) -> str | None:
        hits = [
            name
            for name, box in boxes.items()
            if group == name
            or group == box.get("group")
            or group in (box.get("groups") or [])
        ]
        return hits[0] if len(hits) == 1 else None

    if len(specific) == 1:
        group, cert = next(iter(specific.items()))
        box = box_of(group)
        if box is None:
            return None, cert, f"group {group} sets tailnet_ingress_cert_domain but is not one box"
        return box, cert, f"tailnet_ingress_cert_domain is set for {group}"
    if len(specific) > 1:
        return None, None, "more than one group sets tailnet_ingress_cert_domain"
    if "all" in found:
        region = (group_vars.get("all") or {}).get("headscale_control_region")
        box = box_of(region) if isinstance(region, str) else None
        if box is None:
            return None, found["all"], "no headscale_control_region names the ingress box"
        return box, found["all"], f"headscale_control_region is {region}"
    return None, None, "no group sets tailnet_ingress_cert_domain"


# ── plan ────────────────────────────────────────────────────────────────────


def _norm(entry: Any) -> dict[str, Any] | None:
    """A compiled entry with every default spelled out, for comparison."""
    if not isinstance(entry, Mapping):
        return None
    return {
        "domains": [str(d) for d in entry.get("domains") or []],
        "upstream": entry.get("upstream"),
        "pass_host_header": entry.get("pass_host_header", True) is not False,
        "identity_inject": entry.get("identity_inject", False) is True,
        "entrypoint": entry.get("entrypoint"),
    }


def _proxies(compiled: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = compiled.get("tailnet_proxies")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in raw.items():
        norm = _norm(v)
        if norm is not None:
            out[str(k)] = norm
    return out


def _route_step(action: str, name: str, reason: str) -> dict[str, Any]:
    # The shape of plan._step; built here so plan.py needs only one call site.
    return {
        "id": "",
        "kind": "route",
        "container": None,
        "resource": name,
        "project": None,
        "action": action,
        "risk": "shared",
        "reason": reason,
        "source": "compile",
    }


def route_steps(
    old_compiled: Mapping[str, Any], new_compiled: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """One step per route that differs between two compiled services files."""
    old, new = _proxies(old_compiled), _proxies(new_compiled)
    steps: list[dict[str, Any]] = []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        if before is None:
            assert after is not None
            steps.append(
                _route_step(
                    "route_added",
                    name,
                    f"route {name}: new, {', '.join(after['domains'])} to {after['upstream']}; "
                    "Headscale restarts (new DNS record)",
                )
            )
        elif after is None:
            steps.append(
                _route_step(
                    "route_removed",
                    name,
                    f"route {name}: removed ({', '.join(before['domains'])}); "
                    "Headscale restarts (DNS record removed)",
                )
            )
        else:
            changed = sorted(k for k in after if before.get(k) != after.get(k))
            dns = set(before["domains"]) != set(after["domains"])
            tail = (
                "Headscale restarts (DNS records change)"
                if dns
                else "Traefik reloads the route file"
            )
            steps.append(
                _route_step(
                    "route_changed", name, f"route {name}: changed: {', '.join(changed)}; {tail}"
                )
            )
    return steps


def plan_routes(
    current: Mapping[str, Any],
    wanted: Mapping[str, Any],
    fleet: Mapping[str, Any],
    box_env: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The route steps of a plan, and a blocker when the plan deploys elsewhere.

    A route is served by the ingress box. A plan for a project on another box
    env would commit the route into the services file and never deploy it.
    """
    steps = route_steps(current, wanted)
    blockers: list[str] = []
    env = ingress_env(fleet)
    if steps and env is not None and box_env is not None and env != box_env:
        box = tailnet_of(fleet).get("ingress_box")
        blockers.append(
            f"{len(steps)} route change(s) deploy to the ingress box {box} (env {env}), "
            f"not to {box_env}; plan a project on {env} first"
        )
    return steps, blockers


def deploy_tags(steps: Sequence[Mapping[str, Any]], base: str) -> str:
    """The tags ``bay up`` runs: ``base``, plus headscale and traefik on a route step."""
    tags = [t for t in base.split(",") if t]
    if any(s.get("kind") == "route" for s in steps):
        tags += [t for t in ROUTE_TAGS if t not in tags]
    return ",".join(tags)


# ── bay show --routes ───────────────────────────────────────────────────────


def _running_routes(entries: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]] | None:
    """Routes the receipts list, by name; None when no receipt lists routes.

    Every box writes a receipt; only the ingress box renders routes, so the
    receipt with a non-empty list wins.
    """
    found: dict[str, dict[str, Any]] | None = None
    for entry in entries:
        receipt = entry.get("receipt")
        listed = receipt.get("routes") if isinstance(receipt, Mapping) else None
        if not isinstance(listed, list):
            continue
        rows = {
            str(r["name"]): n
            for r in listed
            if isinstance(r, Mapping) and "name" in r and (n := _norm(r)) is not None
        }
        if rows or found is None:
            found = rows
    return found


def _serves(pinned: Mapping[str, Any] | None, running: Mapping[str, Any] | None) -> bool:
    """True when the box serves the pinned route.

    The rendered file always names an entrypoint; a pinned entry without one
    takes the role's default, so only a pinned entrypoint is compared.
    """
    if pinned is None or running is None:
        return pinned is running
    keys = [k for k in pinned if k != "entrypoint" or pinned[k] is not None]
    return all(pinned[k] == running.get(k) for k in keys)


def show_routes(
    fleet: Mapping[str, Any],
    pinned: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """WANTED (the fleet table) against PINNED (services.yml) and RUNNING (the receipt).

    ``entries`` are the receipts of the ingress box env, or None when they
    were not read. Status per route: ``ok`` (all three agree), ``pending``
    (the fleet table differs from the compiled file: plan and up), ``drift``
    (the box serves something else than the compiled file) or ``unknown``
    (no receipt lists routes).
    """
    wanted_raw, errors = compile_routes(fleet)
    wanted = {k: _norm(v) for k, v in wanted_raw.items()}
    pin = _proxies(pinned)
    running = _running_routes(entries) if entries is not None else None
    rows: list[dict[str, Any]] = []
    for name in sorted(set(wanted) | set(pin) | set(running or {})):
        w, p = wanted.get(name), pin.get(name)
        r = (running or {}).get(name) if running is not None else None
        if w != p:
            status = "pending"
        elif running is None:
            status = "unknown"
        elif not _serves(p, r):
            status = "drift"
        else:
            status = "ok"
        rows.append({"name": name, "wanted": w, "pinned": p, "running": r, "status": status})
    tailnet = tailnet_of(fleet)
    return {
        "ingress_box": tailnet.get("ingress_box"),
        "env": ingress_env(fleet),
        "running_checked": running is not None,
        "routes": rows,
        "errors": errors,
    }


def render_show(doc: Mapping[str, Any]) -> str:
    def cell(entry: Any) -> str:
        if entry is None:
            return "-"
        return f"{', '.join(entry['domains'])} -> {entry['upstream']}"

    lines = [f"tailnet routes, ingress box {doc.get('ingress_box') or 'not set'}"]
    if not doc["routes"]:
        lines.append("  no routes")
    for row in doc["routes"]:
        lines.append(f"  {row['name']}  {row['status']}")
        lines.append(f"    WANTED   {cell(row['wanted'])}")
        lines.append(f"    PINNED   {cell(row['pinned'])}")
        running = cell(row["running"]) if doc["running_checked"] else "unknown"
        lines.append(f"    RUNNING  {running}")
    if not doc["running_checked"]:
        lines.append("note: no receipt lists routes, so RUNNING is unknown")
    for e in doc.get("errors") or []:
        lines.append(f"error: {e}")
    return "\n".join(lines)


# ── ACL check (bay validate) ────────────────────────────────────────────────


def _ports(spec: str) -> list[tuple[int, int]] | None:
    """``"*"`` -> None (every port); ``"80,8000-8100"`` -> ranges."""
    if spec == "*":
        return None
    out: list[tuple[int, int]] = []
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        try:
            out.append((int(lo), int(hi or lo)))
        except ValueError:
            continue
    return out


def _covers(spec: str, port: int) -> bool:
    ranges = _ports(spec)
    return ranges is None or any(lo <= port <= hi for lo, hi in ranges)


def _alias_ips(hosts: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for alias, cidr in hosts.items():
        try:
            out[str(alias)] = ipaddress.ip_network(str(cidr), strict=False)
        except ValueError:
            continue
    return out


def _names_for_host(host: str, nets: Mapping[str, Any]) -> set[str]:
    """Every way an ACL rule can name the machine ``host``."""
    names = {host}
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None:
        label = host.split(".", 1)[0]
        names.add(label)
        if label in nets:
            net = nets[label]
            if net.num_addresses == 1:
                names.add(str(net.network_address))
    else:
        names |= {alias for alias, net in nets.items() if ip in net}
    return names


def _names_for_box(box: str, ip: str | None, nets: Mapping[str, Any]) -> set[str]:
    names = {box}
    if ip:
        names.add(ip)
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            addr = None
        if addr is not None:
            names |= {
                alias for alias, net in nets.items() if net.num_addresses == 1 and addr in net
            }
    return names


def acl_warnings(fleet: Mapping[str, Any], policy: Any) -> list[str]:
    """Routes whose upstream port no ACL rule grants to the ingress box alone.

    ``policy`` is ``headscale_acl_policy``. ``None`` means no policy: the
    tailnet allows everything, which is legal, so there is no warning.
    """
    routes = table(fleet)
    if not routes or not isinstance(policy, Mapping):
        return []
    tailnet = tailnet_of(fleet)
    ingress = tailnet.get("ingress_box")
    if not isinstance(ingress, str):
        return []
    box = (fleet.get("boxes") or {}).get(ingress) or {}
    nets = _alias_ips(policy.get("hosts") or {})
    me = _names_for_box(ingress, box.get("tailnet_ip"), nets)
    rules = [
        r
        for r in policy.get("acls") or []
        if isinstance(r, Mapping) and r.get("action", "accept") == "accept"
    ]
    warnings: list[str] = []
    for name in sorted(routes):
        parsed = parse_upstream(str(routes[name].get("upstream", "")))
        if isinstance(parsed, str):
            continue
        host, port = parsed
        targets = _names_for_host(host, nets) | {"*"}
        sole = False
        reached = False
        others: set[str] = set()
        for rule in rules:
            src = [str(s) for s in rule.get("src") or []]
            grants = False
            for dst in rule.get("dst") or []:
                target, _, spec = str(dst).rpartition(":")
                if target in targets and _covers(spec, port):
                    grants = True
                    break
            if not grants:
                continue
            if "*" in src or me & set(src):
                reached = True
            if src and all(s in me for s in src):
                sole = True
            others |= {s for s in src if s not in me}
        if sole:
            continue
        where = f"route {name}: {host}:{port}"
        if not reached:
            warnings.append(
                f"{where} has no ACL rule that lets the ingress box {ingress} reach it; "
                "the route answers 502. Add a rule with src [\"" + ingress + "\"]."
            )
        else:
            warnings.append(
                f"{where} has no ACL rule with the ingress box {ingress} as its only src; "
                f"also reachable from {', '.join(sorted(others))}, which can bypass the "
                "ingress (and set X-Tailnet-Device)"
            )
    return warnings


def acl_policy_of(parsed_files: Mapping[str, Any]) -> Any:
    """``headscale_acl_policy`` from parsed ``group_vars/all`` files, or None."""
    for rel, data in parsed_files.items():
        if not str(rel).startswith("group_vars/all/") or not isinstance(data, Mapping):
            continue
        if data.get("headscale_acl_policy"):
            return data["headscale_acl_policy"]
    return None


# ── text edits of bay.fleet.toml ────────────────────────────────────────────


def _table_path(line: str) -> tuple[str, ...] | None:
    """The key path of a ``[table]`` header line, or None."""
    m = _HEADER_RE.match(line) or _ARRAY_HEADER_RE.match(line)
    if not m:
        return None
    try:
        node: Any = tomllib.loads(f"{m.group(1)} = 0")
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    while isinstance(node, dict) and len(node) == 1:
        key = next(iter(node))
        path.append(key)
        node = node[key]
    return tuple(path)


def _headers(lines: list[str]) -> list[tuple[int, tuple[str, ...]]]:
    out: list[tuple[int, tuple[str, ...]]] = []
    for i, line in enumerate(lines):
        path = _table_path(line)
        if path is not None:
            out.append((i, path))
    return out


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__}")


_ROUTE_ORDER = ("domain", "upstream", "host", "identity", "aliases", "entrypoint")


def route_block(name: str, route: Mapping[str, Any]) -> list[str]:
    lines = [f"[tailnet.routes.{name}]"]
    for key in _ROUTE_ORDER:
        if key in route:
            lines.append(f"{key} = {_value(route[key])}")
    return lines


def _end_of_body(lines: list[str], start: int, stop: int) -> int:
    """The index after the last key line of a table body (trailing comments stay out)."""
    end = stop
    while end > start + 1 and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1
    return end


def _pruned(doc: Mapping[str, Any]) -> dict[str, Any]:
    """``doc`` without an empty ``[tailnet.routes]`` or ``[tailnet]``.

    Whether TOML keeps an empty table depends on how the file wrote it; the
    two forms mean the same fleet.
    """
    out = copy.deepcopy(dict(doc))
    tailnet = out.get("tailnet")
    if isinstance(tailnet, dict):
        if tailnet.get("routes") == {}:
            del tailnet["routes"]
        if not tailnet:
            del out["tailnet"]
    return out


def _checked(text: str, expected: Mapping[str, Any]) -> str:
    try:
        got = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BayError(
            f"the edit of bay.fleet.toml would not parse ({exc})",
            hint="Edit bay.fleet.toml by hand.",
            code=ErrorCode.CONFLICT,
        ) from None
    if _pruned(got) != _pruned(expected):
        raise BayError(
            "the edit of bay.fleet.toml would change more than the route",
            hint="Edit bay.fleet.toml by hand.",
            code=ErrorCode.CONFLICT,
        )
    return text


def _join(lines: list[str], trailing_newline: bool) -> str:
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).rstrip("\n")
    return text + ("\n" if trailing_newline else "")


def set_tailnet_keys(text: str, values: Mapping[str, Any]) -> str:
    """Set top keys of ``[tailnet]`` (``ingress_box``, ``cert_domain``). Comments stay."""
    doc = tomllib.loads(text)
    expected = copy.deepcopy(doc)
    expected.setdefault("tailnet", {}).update(values)
    lines = text.splitlines()
    heads = _headers(lines)
    own = next((i for i, p in heads if p == ("tailnet",)), None)
    if own is None:
        first_sub = next((i for i, p in heads if p[:1] == ("tailnet",)), None)
        block = ["[tailnet]", *(f"{k} = {_value(v)}" for k, v in values.items())]
        if first_sub is None:
            lines += ["", *block]
        else:
            lines[first_sub:first_sub] = [*block, ""]
        return _checked(_join(lines, True), expected)
    stop = next((i for i, _p in heads if i > own), len(lines))
    insert: list[str] = []
    for key, value in values.items():
        new = f"{key} = {_value(value)}"
        pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=")
        hit = next((i for i in range(own + 1, stop) if pattern.match(lines[i])), None)
        if hit is None:
            insert.append(new)
        else:
            lines[hit] = new
    lines[own + 1 : own + 1] = insert
    return _checked(_join(lines, text.endswith("\n")), expected)


def add_route_text(text: str, name: str, route: Mapping[str, Any]) -> str:
    """``text`` with ``[tailnet.routes.<name>]`` added after the last tailnet table."""
    doc = tomllib.loads(text)
    if name in table(doc):
        raise BayError(f"route {name} exists", hint=f"Remove it first: bay route rm {name}")
    expected = copy.deepcopy(doc)
    expected.setdefault("tailnet", {}).setdefault("routes", {})[name] = dict(route)
    lines = text.splitlines()
    heads = _headers(lines)
    block = route_block(name, route)
    last = [i for i, p in heads if p[:1] == ("tailnet",)]
    if not last:
        lines += ["", *block]
    else:
        start = last[-1]
        stop = next((i for i, _p in heads if i > start), len(lines))
        end = _end_of_body(lines, start, stop)
        lines[end:end] = ["", *block]
        if end < len(lines) - len(block) - 1 and lines[end + len(block) + 1].strip():
            lines.insert(end + len(block) + 1, "")
    return _checked(_join(lines, True), expected)


def remove_route_text(text: str, name: str) -> str:
    """``text`` without ``[tailnet.routes.<name>]`` and its keys. Other comments stay."""
    doc = tomllib.loads(text)
    if name not in table(doc):
        raise BayError(f"there is no route {name}", hint="List them: bay route ls")
    expected = copy.deepcopy(doc)
    del expected["tailnet"]["routes"][name]
    lines = text.splitlines()
    heads = _headers(lines)
    own = ("tailnet", "routes", name)
    start = next((i for i, p in heads if p == own), None)
    if start is None:
        raise BayError(
            f"route {name} is not written as a [tailnet.routes.{name}] table",
            hint="Edit bay.fleet.toml by hand.",
            code=ErrorCode.CONFLICT,
        )
    stop = next((i for i, p in heads if i > start and p[: len(own)] != own), len(lines))
    end = _end_of_body(lines, start, stop)
    del lines[start:end]
    return _checked(_join(lines, text.endswith("\n")), expected)
