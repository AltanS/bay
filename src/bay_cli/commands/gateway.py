"""Access gateway management commands: status, nodes, users, routes, and device enrollment."""

import ipaddress
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.table import Table

from bay_cli import ansible, paths, runner
from bay_cli import console as con
from bay_cli.commands.gateway_backend import (
    GatewayBackend,
    LocalHeadscaleBackend,
    NullGatewayBackend,
)
from bay_cli.errors import BayError
from bay_cli.utils.ephemeral import show_ephemeral

app = typer.Typer(help="Manage the access gateway (headscale tailnet / wireguard).")
acl_app = typer.Typer(help="Inspect the tailnet ACL policy.")
app.add_typer(acl_app, name="acl")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# \Z, not $: `$` also matches just before a trailing newline, so "alice\n" would
# pass. Every validated value here is f-string-interpolated into a shell command
# (see gateway_backend), where today a trailing newline merely ends the command
# harmlessly — but only because nothing is appended after it. \Z keeps these
# charsets actually closed rather than closed-by-luck.
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+\Z")
# Mirrors Prometheus model.ParseDuration, which is what headscale parses
# --expiration with: units ms/s/m/h/d/w/y, largest first, compound allowed
# ("1h30m", "2w3d"), no decimals. Verified against a live headscale v0.28.
# Deliberately no stricter than the server — a validator that rejects what
# headscale accepts (this used to reject "2w") is its own bug. Bare "0" is
# ParseDuration's own special case and is accepted for the same reason.
_EXPIRATION_RE = re.compile(
    r"^(?:0|(?=.)(\d+y)?(\d+w)?(\d+d)?(\d+h)?(\d+m)?(\d+s)?(\d+ms)?)\Z"
)
# ACL tags as headscale spells them: the literal `tag:` prefix, then lowercase
# alphanumerics and hyphens. headscale is strict about the prefix (a bare "agent"
# is not a tag, it's a parse error) and the value ends up comma-joined into a
# root shell on the control host, so keep this closed with \Z like the others.
_TAG_RE = re.compile(r"^tag:[a-z0-9][a-z0-9-]*\Z")


# headscale's JSON is protobuf-derived and therefore snake_case throughout
# (ip_addresses, last_seen, approved_routes, ...). Reading camelCase keys silently
# yields nothing rather than raising, which is how the IP and LAST SEEN columns
# came to render blank and `routes` came to report "no routes found" unconditionally.
# Go through these helpers rather than reaching into the dicts by hand.

# protobuf's zero Timestamp — headscale's "never", not a real date.
_PROTO_ZERO_TIME_S = -62135596800


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _node_ip(record: dict) -> str:
    """Return a node's primary tailnet IP, or '' if it has none."""
    ips = record.get("ip_addresses") or []
    return str(ips[0]) if ips else ""


def _format_timestamp(value: object) -> str:
    """Render a headscale timestamp as local 'YYYY-MM-DD HH:MM:SS'.

    headscale marshals protobuf Timestamps to {"seconds": ..., "nanos": ...} rather
    than the RFC3339 string protojson would emit, so accept both shapes. Rendered in
    local time on purpose: headscale's own table prints UTC, which makes a node seen
    seconds ago look hours stale to an operator reading a "LAST SEEN" column.
    """
    if isinstance(value, dict):
        seconds = value.get("seconds")
        if not isinstance(seconds, int) or seconds <= _PROTO_ZERO_TIME_S:
            return ""
        return (
            datetime.fromtimestamp(seconds, tz=UTC)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    if isinstance(value, str):
        return value[:19].replace("T", " ")
    return ""


def _validate_username(name: str) -> None:
    """Validate headscale username to prevent shell injection."""
    if not _USERNAME_RE.match(name):
        con.error(f"Invalid username '{name}': only alphanumeric, hyphens, and underscores allowed.")
        raise typer.Exit(1)


def _validate_node_name(name: str) -> None:
    """Validate a node name we are about to SET. Same contract as usernames.

    `nodes rename` interpolates this straight into a root shell (see
    gateway_backend.rename_node), and rename-node was the one name-taking
    command that skipped validation. Same charset as `enroll`, which already
    gates hostnames here — so this only rejects names the CLI could not have
    created anyway. Applied to the new name only: the old name is a JSON lookup
    key, never reaches a shell, and may predate this CLI's charset.
    """
    if not _USERNAME_RE.match(name):
        con.error(
            f"Invalid node name '{name}': only alphanumeric, hyphens, and underscores allowed."
        )
        raise typer.Exit(1)


def _validate_tags(tags: list[str] | None) -> list[str]:
    """Validate ACL tags we are about to stamp onto a pre-auth key.

    Rejects before any headscale call — a bad tag is a typo the operator wants
    back immediately, not an opaque error from the control host after a user has
    already been created. Names the offending value: with several --tag flags,
    "invalid tag" alone doesn't say which.

    Deduplicates, first occurrence wins: the tags are comma-joined into a single
    --tags value, and `--tag tag:agent --tag tag:agent` otherwise stamps the key
    with "tag:agent,tag:agent". Validation runs first so a repeated typo is still
    reported rather than collapsed away.
    """
    if not tags:
        return []
    for tag in tags:
        if not tag.startswith("tag:"):
            con.error(
                f"Invalid tag '{tag}': ACL tags must start with 'tag:' (e.g. tag:agent)."
            )
            raise typer.Exit(1)
        if not _TAG_RE.match(tag):
            con.error(
                f"Invalid tag '{tag}': after 'tag:' use lowercase letters, digits and "
                "hyphens only (e.g. tag:ci-runner)."
            )
            raise typer.Exit(1)
    return list(dict.fromkeys(tags))


def _get_control_host(bay_dir: Path, region: str | None = None) -> str | None:
    """Return the control host for gateway commands, or None for single-server.

    Uses headscale_control_region from access_gateway.yml when available,
    falling back to the first child group convention.
    With --region, targets that specific region's host instead.
    Returns None for single-server setups (no --limit needed).
    """
    consumer_root = bay_dir.parent
    inventory = consumer_root / "hosts" / "production"
    if not inventory.exists():
        return None

    text = inventory.read_text()

    # Single-server: no children groups, no limit needed
    if "[production:children]" not in text:
        return None

    # Two-pass parse: [production:children] may appear before or after group defs
    # Pass 1: collect children group names
    children: list[str] = []
    in_children = False
    for line in text.splitlines():
        line = line.strip()
        if line == "[production:children]":
            in_children = True
            continue
        if in_children:
            if line.startswith("["):
                break
            if line and not line.startswith("#"):
                children.append(line)

    # Pass 2: map each child group to its first host
    groups: dict[str, str] = {}  # group_name -> first host IP
    current_group: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1].split(":")[0]
            continue
        if current_group and current_group in children and line and not line.startswith("#"):
            if current_group not in groups:
                groups[current_group] = line.split()[0]

    if region:
        if region in groups:
            return groups[region]
        # Unknown region — return None, let ansible fail naturally
        return None

    # Prefer explicit headscale_control_region from config
    config = _get_gateway_config(bay_dir)
    control_region = config.get("headscale_control_region")
    if control_region and control_region in groups:
        return groups[control_region]

    # Fallback: first child group (wizard convention)
    if children and children[0] in groups:
        return groups[children[0]]

    return None


def _run_on_host(
    env: str,
    cmd: str,
    *,
    bay_dir: Path,
    capture: bool = True,
    message: str = "",
    check: bool = True,
    limit: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command on the target host via ansible ad-hoc."""
    uv_cmd = ansible._uv_run_cmd(bay_dir)
    full_cmd = [
        *uv_cmd,
        "ansible", env,
        "-m", "ansible.builtin.shell",
        "-a", cmd,
        "--become",
    ]
    if limit:
        full_cmd.extend(["--limit", limit])
    return runner.run(
        full_cmd,
        capture=capture,
        message=message,
        env=ansible._collections_env(bay_dir),
        check=check,
    )


def _extract_output_from_ansible(stdout: str) -> str:
    """Extract command output from ansible ad-hoc response.

    Ansible output format: 'hostname | SUCCESS | rc=0 >>\\n<output>'
    Handles ANSI color codes in ansible output.
    """
    clean = _strip_ansi(stdout)
    marker = ">>"
    idx = clean.find(marker)
    if idx == -1:
        return clean
    return clean[idx + len(marker):].strip()


def _get_gateway_config(bay_dir: Path) -> dict:
    """Read access gateway configuration from consumer group_vars."""
    import yaml

    consumer_root = bay_dir.parent
    config: dict = {
        "access_gateway": "wireguard",
        "vpn_allowed_ips": [],
        "headscale_domain": "",
        "headscale_tailnet_cidr": ["100.64.0.0/10"],
        "headscale_control_region": "",
    }

    # Check candidate files for gateway configuration
    candidates = [
        consumer_root / "group_vars" / "all" / "access_gateway.yml",
        consumer_root / "group_vars" / "all" / "main.yml",
    ]
    for candidate in candidates:
        if candidate.exists():
            data = yaml.safe_load(candidate.read_text()) or {}
            for key in config:
                if key in data:
                    config[key] = data[key]

    # Also check vpn_access.yml for vpn_allowed_ips if not already found
    vpn_file = consumer_root / "group_vars" / "all" / "vpn_access.yml"
    if vpn_file.exists():
        data = yaml.safe_load(vpn_file.read_text()) or {}
        if "vpn_allowed_ips" in data:
            config["vpn_allowed_ips"] = data["vpn_allowed_ips"]

    return config


def _find_acl_policy_file(bay_dir: Path) -> Path | None:
    """Return the consumer group_vars file defining headscale_acl_policy, if any.

    Its presence means the tailnet is DEFAULT-DENY, which is load-bearing for
    enrollment: a node that no accept rule names is not merely unreachable, it is
    never distributed to the other nodes as a peer at all (it won't show up in
    their ``tailscale status``), so a perfectly successful enrollment still reads
    as a broken one. The policy is a plain group_vars var and may live in any file,
    so search rather than guess a path.
    """
    import yaml

    group_vars = bay_dir.parent / "group_vars"
    if not group_vars.is_dir():
        return None

    for path in sorted(group_vars.rglob("*.yml")):
        try:
            text = path.read_text()
        except OSError:
            continue
        if text.lstrip().startswith("$ANSIBLE_VAULT"):
            continue
        # Cheap pre-filter — parsing every group_vars file to find one key is waste.
        if "headscale_acl_policy" not in text:
            continue
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        # `in`, not truthiness: Ansible keys the policy off `is defined`, so even an
        # empty value flips the tailnet to default-deny.
        if isinstance(data, dict) and "headscale_acl_policy" in data:
            return path
    return None


def _warn_default_deny(acl_file: Path, bay_dir: Path, node_name: str | None, env: str) -> None:
    """Warn that an enrolled node stays unreachable until the ACL names it."""
    try:
        rel = acl_file.relative_to(bay_dir.parent)
    except ValueError:
        rel = acl_file
    alias = node_name or "<device>"

    con.warning(f"Tailnet is DEFAULT-DENY ([bold]{rel}[/bold]) — this device is not reachable yet.")
    con.console.print(
        f"\n  Enrollment succeeded, but nothing can reach [bold]{alias}[/bold] until an"
        f"\n  accept rule names it — it won't even appear in [dim]tailscale status[/dim] on your"
        f"\n  other nodes. Grant it explicitly:"
        f"\n"
        f"\n  [bold]1.[/bold] Get its tailnet IP once the device has joined:"
        f"\n"
        f"\n       [dim]bin/bay gateway nodes[/dim]"
        f"\n"
        f"\n  [bold]2.[/bold] Add to [bold]headscale_acl_policy[/bold] in [bold]{rel}[/bold]:"
        f"\n"
        f"\n       [dim]hosts:[/dim]"
        f"\n       [dim]  {alias}: 100.64.0.X/32[/dim]"
        f"\n"
        f"\n       [dim]acls:[/dim]"
        f"\n       [dim]  - action: accept[/dim]"
        f"\n       [dim]    src: [\"<your-laptop>\"]        # who may reach it[/dim]"
        f"\n       [dim]    dst: [\"{alias}:*\"]            # which ports[/dim]"
        f"\n"
        f"\n     Rules are one-way: the rule above only grants traffic [bold]to[/bold] {alias}."
        f"\n     For anything {alias} must reach itself, also add rules with"
        f"\n     [bold]{alias}[/bold] in [dim]src[/dim] — there is no deny to spot in a log, ungranted"
        f"\n     traffic just goes nowhere."
        f"\n"
        f"\n  [bold]3.[/bold] Deploy the policy to the control host, then verify:"
        f"\n"
        f"\n       [dim]bin/bay deploy {env} --tags headscale[/dim]"
        f"\n       [dim]bin/bay gateway acl audit[/dim]"
        f"\n"
    )


def _report_tagged_enrollment(
    tags: list[str], acl_file: Path | None, bay_dir: Path, node_name: str | None, env: str
) -> None:
    """Explain what a key-stamped tag does — and the three ways it can still be inert."""
    alias = node_name or "<device>"
    tag_list = ", ".join(tags)
    first_tag = tags[0]
    rel: Path | str = "your headscale ACL policy"
    if acl_file:
        try:
            rel = acl_file.relative_to(bay_dir.parent)
        except ValueError:
            rel = acl_file

    con.info(f"Key is stamped with [bold]{tag_list}[/bold] — the device joins already tagged.")
    con.console.print(
        f"\n  Every ACL rule matching {'these tags' if len(tags) > 1 else 'this tag'} applies"
        f"\n  from the moment [bold]{alias}[/bold] joins — there is no window where it is online"
        f"\n  but ungranted. If class rules for [bold]{first_tag}[/bold] already exist, you are"
        f"\n  DONE: no hosts: alias, no per-device rule, no deploy."
        f"\n"
        f"\n  [bold]But if no rule names {first_tag} yet[/bold], this device is exactly as"
        f"\n  dead on arrival as an untagged one. The tag has to appear in BOTH"
        f"\n  [bold]tagOwners[/bold] and in accept rules in [bold]{rel}[/bold] first — as"
        f"\n  [dim]dst[/dim] for what may reach it, and as [dim]src[/dim] for what it must reach"
        f"\n  itself. Then [dim]bin/bay deploy {env} --tags headscale[/dim]."
        f"\n"
        f"\n  [bold]Ownership:[/bold] on headscale v0.29.x a key-stamped node registers"
        f"\n  under the synthetic [bold]tagged-devices[/bold] user, NOT '{alias}' — verified"
        f"\n  against v0.29.2. The tag still shows in the Tags column of"
        f"\n  [dim]bin/bay gateway nodes[/dim]; only the owner moves. Anything keyed on the"
        f"\n  user (per-user ACL rules) will not match this node — key off the tag."
        f"\n"
        f"\n  [bold]Verifying:[/bold] [dim]bin/bay gateway acl audit[/dim] resolves"
        f"\n  [dim]tag:[/dim] targets against live headscale state, so once the device has"
        f"\n  joined it will show as reachable via the class rule — and it names any tag"
        f"\n  no node carries. It still only checks the inbound (dst) side. For the"
        f"\n  outbound side, probe FROM A PEER, never from the node serving the port:"
        f"\n"
        f"\n       [dim]nc -z -w5 <ip> <port>[/dim]   denied → TIMES OUT (exit 124)"
        f"\n                                  allowed, port closed → REFUSED (exit 1)"
        f"\n"
    )


# Where a node's ACL tags actually live in `headscale nodes list -o json`.
#
# Verified against headscale v0.29.2: the flat top-level `tags` array is the one
# that is populated, for BOTH force-tagged nodes (`nodes tag -t`) and nodes that
# joined on a tag-stamped pre-auth key. The fields you would reach for first —
# `forced_tags` and `valid_tags` — are absent from the JSON entirely (they read
# back as null) even for a node whose Tags column shows tag:agent, so keying off
# either one alone silently sees an untagged tailnet. `pre_auth_key.acl_tags`
# holds the stamp on the KEY (it is populated in `preauthkeys list -o json`) but
# is not echoed into the node record. Union everything plausible: the cost of an
# extra empty field is nothing, the cost of missing the live one is an audit that
# reports every tagged node as NOT IN POLICY.
_NODE_TAG_FIELDS = ("tags", "forced_tags", "valid_tags", "applied_tags", "invalid_tags")


def _node_tags(node: dict) -> list[str]:
    """Return every ACL tag headscale reports for a node, from any known field."""
    found: list[str] = []
    sources: list[object] = [node.get(f) for f in _NODE_TAG_FIELDS]
    pak = node.get("pre_auth_key")
    if isinstance(pak, dict):
        sources.append(pak.get("acl_tags"))
    for source in sources:
        if not isinstance(source, list):
            continue
        for tag in source:
            text = str(tag)
            if text.startswith("tag:") and text not in found:
                found.append(text)
    return found


def _build_tag_index(nodes: list[dict]) -> dict[str, list[ipaddress._BaseNetwork]]:
    """Map each ACL tag present on the tailnet to the host networks carrying it."""
    index: dict[str, list[ipaddress._BaseNetwork]] = {}
    for node in nodes:
        tags = _node_tags(node)
        if not tags:
            continue
        nets: list[ipaddress._BaseNetwork] = []
        for raw_ip in node.get("ip_addresses") or []:
            try:
                nets.append(ipaddress.ip_network(str(raw_ip), strict=False))
            except ValueError:
                continue
        for tag in tags:
            index.setdefault(tag, []).extend(nets)
    return index


def _resolve_acl_target(
    alias: str,
    hosts: dict,
    tag_index: dict[str, list[ipaddress._BaseNetwork]] | None = None,
) -> list[ipaddress._BaseNetwork] | None:
    """Resolve an ACL dst target to networks, or None if it isn't IP-resolvable.

    None means "can't tell" — group:, autogroup: and user@ targets have no IP
    answer here. That is deliberately distinct from "resolves to nothing": callers
    must not read an unresolvable target as proof a node is unreachable.

    `tag:` targets DO resolve, but only against live headscale state, so they need
    `tag_index` (built from the node list the audit already fetches). With no
    index they stay None, as before. With one, a tag returns the networks of the
    nodes currently carrying it — and an EMPTY list when no node carries it, which
    is a real answer: rules granting that tag are inert. Callers must tell that
    empty list apart from None and surface it.
    """
    if alias.startswith("tag:"):
        if tag_index is None:
            return None
        return list(tag_index.get(alias, []))
    if alias == "*":
        return [ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")]
    if alias in hosts:
        try:
            return [ipaddress.ip_network(str(hosts[alias]), strict=False)]
        except ValueError:
            return None
    try:
        return [ipaddress.ip_network(alias, strict=False)]
    except ValueError:
        return None


def _ip_hits(nets: list[ipaddress._BaseNetwork], addrs: list[ipaddress._BaseAddress]) -> int:
    """Count how many of `nets` contain any of `addrs`."""
    return sum(1 for n in nets if any(a in n for a in addrs if a.version == n.version))


def _audit_acl_reachability(
    policy: dict, nodes: list[dict]
) -> tuple[list[dict], list[str], list[str]]:
    """Classify each live node against the ACL policy.

    Zero inbound rules alone is NOT a fault — a phone or laptop that only ever
    initiates is legitimately client-only, and flagging it would make this cry wolf
    on every run. What's actually diagnostic is whether the policy mentions the node
    at all. A node named nowhere is the dead-on-arrival case this exists to catch:
    freshly enrolled, online, and silently inert, because under default-deny an
    ungranted node isn't distributed to other nodes as a peer at all.

    The `*` wildcard doesn't count as being named — it matches every node, so
    treating it as intent would mask exactly the case we're hunting.

    Each row gets a status:
      reachable   — some accept rule names it as dst
      client-only — named as src by a specific target, but nothing reaches it
      unknown     — the policy never names it; almost certainly an oversight

    `tag:` targets are resolved against the live node list, so a class rule
    (`dst: ["tag:agent:*"]`) counts as naming every node currently carrying that
    tag — without that, a tag-based policy makes this audit blind and every
    tagged node reads as NOT IN POLICY.

    Returns (rows, unresolvable, inert_tags).
      unresolvable — group:/autogroup:/user@ targets with no IP answer here, so
                     reachability may be understated.
      inert_tags   — tags a rule names that NO live node carries. The tag-flavored
                     dead-on-arrival case: the rule is syntactically fine and
                     grants nothing. Never let this resolve silently to nothing.
    """
    hosts = policy.get("hosts") or {}
    unresolvable: list[str] = []
    inert_tags: list[str] = []
    tag_index = _build_tag_index(nodes)

    def _resolve(target: str) -> list[ipaddress._BaseNetwork] | None:
        """Resolve one target, recording the unresolvable and the inert."""
        resolved = _resolve_acl_target(target, hosts, tag_index)
        if resolved is None:
            if target not in unresolvable:
                unresolvable.append(target)
            return None
        if not resolved and target.startswith("tag:") and target not in inert_tags:
            inert_tags.append(target)
        return resolved

    def _nets_for(key: str, *, strip_port: bool) -> list[ipaddress._BaseNetwork]:
        nets: list[ipaddress._BaseNetwork] = []
        for rule in policy.get("acls") or []:
            if not isinstance(rule, dict) or rule.get("action") != "accept":
                continue
            for entry in rule.get(key) or []:
                raw = str(entry)
                # dst is "target:ports"; rsplit keeps IPv6 targets intact. src has
                # no port suffix, so splitting it would corrupt an IPv6 CIDR.
                target = raw.rsplit(":", 1)[0] if (strip_port and ":" in raw) else raw
                if target == "*":
                    continue  # wildcard: never evidence of intent about a node
                resolved = _resolve(target)
                if resolved is None:
                    continue
                nets.extend(resolved)
        return nets

    # dst keeps the wildcard: `dst: ["*:*"]` genuinely does make every node reachable.
    dst_nets: list[ipaddress._BaseNetwork] = []
    for rule in policy.get("acls") or []:
        if not isinstance(rule, dict) or rule.get("action") != "accept":
            continue
        for entry in rule.get("dst") or []:
            raw = str(entry)
            target = raw.rsplit(":", 1)[0] if ":" in raw else raw
            resolved = _resolve(target)
            if resolved is None:
                continue
            dst_nets.extend(resolved)

    named_src_nets = _nets_for("src", strip_port=False)

    rows: list[dict] = []
    for node in nodes:
        addrs: list[ipaddress._BaseAddress] = []
        for raw_ip in node.get("ip_addresses") or []:
            try:
                addrs.append(ipaddress.ip_address(str(raw_ip)))
            except ValueError:
                continue

        inbound = _ip_hits(dst_nets, addrs)
        named_src = _ip_hits(named_src_nets, addrs)
        if inbound:
            status = "reachable"
        elif named_src:
            status = "client-only"
        else:
            status = "unknown"

        rows.append(
            {
                "name": node.get("given_name") or node.get("name", ""),
                "ip": _node_ip(node),
                "inbound": inbound,
                "status": status,
            }
        )
    return rows, unresolvable, inert_tags


def _require_headscale(config: dict) -> None:
    """Exit with error if gateway type is not headscale."""
    if config.get("access_gateway") != "headscale":
        con.error("Node management is only available with access_gateway: headscale")
        raise typer.Exit(1)


def _make_backend(
    bay_dir: Path, env: str, region: str | None = None, config: dict | None = None
) -> GatewayBackend:
    """Create the backend for the configured access gateway.

    Dispatches on `access_gateway`. Anything that is not headscale gets a
    NullGatewayBackend, so an unsupported command raises one uniform
    capability error instead of a traceback from a headscale call that was
    never going to reach a control server. Callers that already validated the
    gateway type may omit `config`.
    """
    gateway_type = (config or {}).get("access_gateway", "headscale")
    if gateway_type != "headscale":
        return NullGatewayBackend(gateway_type)
    control_host = _get_control_host(bay_dir, region)
    return LocalHeadscaleBackend(env, bay_dir, limit=control_host)


def _get_node_by_name(
    name: str, backend: LocalHeadscaleBackend,
) -> dict:
    """Resolve a node given_name to its record, erroring on ambiguity."""
    try:
        nodes_data = backend.list_nodes()
    except (json.JSONDecodeError, BayError):
        con.error("Failed to parse node list from headscale.")
        raise typer.Exit(1)

    matches = [
        n for n in nodes_data
        if (n.get("given_name") or n.get("name", "")) == name
    ]
    if not matches:
        con.error(f"Node '{name}' not found.")
        raise typer.Exit(1)
    if len(matches) > 1:
        con.warning(f"Multiple nodes named '{name}':")
        for m in matches:
            user = (
                m.get("user", {}).get("name", "")
                if isinstance(m.get("user"), dict)
                else str(m.get("user", ""))
            )
            ip = _node_ip(m)
            con.console.print(f"  ID {m['id']}: user={user}, ip={ip}")
        con.error("Ambiguous node name. Remove duplicates or rename nodes first.")
        raise typer.Exit(1)
    return matches[0]


@app.command()
def status(
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Show access gateway status.

    For headscale: coordination server domain, tailnet CIDR, and connected
    node count. For wireguard: the configured allowed IPs.

    Examples:

        bin/bay gateway status
        bin/bay gateway status --region eu
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    gateway_type = config.get("access_gateway", "wireguard")

    con.header("Access Gateway")

    if gateway_type == "headscale":
        domain = config.get("headscale_domain", "")
        cidr = config.get("headscale_tailnet_cidr", ["100.64.0.0/10"])
        cidr_display = ", ".join(cidr) if isinstance(cidr, list) else str(cidr)

        con.console.print(f"  Type:         [bold]headscale[/bold]")
        con.console.print(f"  Server:       {domain}")
        con.console.print(f"  Tailnet CIDR: {cidr_display}")

        # Get node count from remote
        try:
            backend = _make_backend(bay_dir, env, region)
            nodes = backend.list_nodes()
            online = sum(1 for n in nodes if n.get("online", False))
            con.console.print(f"  Nodes:        {online} connected ({len(nodes)} total)")
        except (json.JSONDecodeError, BayError):
            con.console.print(f"  Nodes:        [dim]unavailable[/dim]")

    elif gateway_type == "wireguard":
        allowed_ips = config.get("vpn_allowed_ips", [])
        con.console.print(f"  Type:        [bold]wireguard[/bold]")
        if allowed_ips:
            con.console.print(f"  Allowed IPs: {', '.join(str(ip) for ip in allowed_ips)}")
        else:
            con.console.print(f"  Allowed IPs: [dim]none configured[/dim]")

    else:
        con.console.print(f"  Type: [bold]{gateway_type}[/bold]")
        con.warning(f"Unknown gateway type: {gateway_type}")


@app.command()
def nodes(
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """List tailnet nodes with user, IP, and last-seen time (headscale only).

    A node missing from THIS list never joined. But a node missing from
    another machine's `tailscale status` may still be enrolled fine: under a
    default-deny ACL policy an ungranted node is not distributed as a peer
    at all. Run `bin/bay gateway acl audit` before re-enrolling anything.

    Examples:

        bin/bay gateway nodes
        bin/bay gateway nodes --region eu
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)

    con.header("Gateway Nodes")

    backend = _make_backend(bay_dir, env, region)
    try:
        nodes_data = backend.list_nodes()
    except json.JSONDecodeError:
        con.error("Failed to parse node list from headscale.")
        raise typer.Exit(1)

    # Build user ID → name lookup from live users list (node responses cache stale names)
    user_id_map: dict[str, str] = {}
    try:
        users_data = backend.list_users()
        for u in users_data:
            user_id_map[str(u.get("id", ""))] = u.get("name", "")
    except (json.JSONDecodeError, BayError):
        pass  # Fall back to embedded user name if users list fails

    if not nodes_data:
        if con.is_json_mode():
            con.emit_result({"nodes": []}, command="gateway.nodes")
            return
        con.info("No nodes registered.")
        return

    # Resolve every node once, then render — the same rows feed both the table
    # and --json, so machine-readable output can't drift from what an operator
    # sees. Printing the Rich table in JSON mode made `--json gateway nodes`
    # unparseable and forced a `docker exec headscale ...` workaround (GH bay#29).
    rows = []
    for node in nodes_data:
        name = node.get("given_name") or node.get("name", "")
        # Resolve user name from live users list, fall back to embedded name
        if isinstance(node.get("user"), dict):
            user_id = str(node["user"].get("id", ""))
            user = user_id_map.get(user_id, node["user"].get("name", ""))
        else:
            user = str(node.get("user", ""))
        rows.append(
            {
                "name": name,
                "user": user,
                "ip": _node_ip(node),
                "last_seen": _format_timestamp(node.get("last_seen")),
                "online": bool(node.get("online", False)),
            }
        )

    if con.is_json_mode():
        con.emit_result({"nodes": rows}, command="gateway.nodes")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("NODE", width=20)
    table.add_column("USER", width=15)
    table.add_column("IP", width=18)
    table.add_column("LAST SEEN", width=22)

    for row in rows:
        status_marker = "[green]*[/green] " if row["online"] else "  "
        table.add_row(
            f"{status_marker}{row['name']}", row["user"], row["ip"], row["last_seen"]
        )

    con.console.print(table)


@acl_app.command("audit")
def acl_audit(
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Flag tailnet nodes that no accept rule can reach.

    Only meaningful when headscale_acl_policy is defined (without it the
    tailnet is allow-all). Cross-checks live nodes against the policy:

        reachable      some accept rule names the node as dst
        client-only    named as src only — fine for a phone/laptop that
                       only initiates connections
        NOT IN POLICY  never named; exits 1. The dead-on-arrival case:
                       enrolled, online, and silently unreachable.

    ACL rules are accept-only and DIRECTIONAL. This audit checks the inbound
    (dst) side; a node that is reachable may still be unable to initiate
    toward peers no rule grants it as src. The `*` wildcard does not count
    as naming a node.

    tag: targets ARE resolved — against live headscale state, not the hosts:
    map — so a class rule like dst: ["tag:agent:*"] counts as naming every
    node currently carrying that tag. A tag no live node carries is called
    out separately: the rule is valid and grants nothing. group:/autogroup:/
    user@ targets have no IP answer here and are still reported as
    unresolvable rather than guessed.

    Examples:

        bin/bay gateway acl audit
        bin/bay gateway acl audit --region eu
    """
    import yaml

    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)

    acl_file = _find_acl_policy_file(bay_dir)
    if acl_file is None:
        con.info("No headscale_acl_policy defined — the tailnet is allow-all, nothing to audit.")
        return

    data = yaml.safe_load(acl_file.read_text()) or {}
    policy = data.get("headscale_acl_policy") or {}

    con.header("ACL Audit")

    backend = _make_backend(bay_dir, env, region)
    try:
        nodes_data = backend.list_nodes()
    except json.JSONDecodeError:
        con.error("Failed to parse node list from headscale.")
        raise typer.Exit(1) from None

    rows, unresolvable, inert_tags = _audit_acl_reachability(policy, nodes_data)
    unknown = [r for r in rows if r["status"] == "unknown"]

    if con.is_json_mode():
        con.emit_result(
            {
                "policy_file": str(acl_file),
                "nodes": rows,
                "unknown": [r["name"] for r in unknown],
                "unresolvable_targets": unresolvable,
                "inert_tags": inert_tags,
            },
            command="gateway.acl.audit",
        )
        return

    styles = {
        "reachable": "[green]reachable[/green]",
        "client-only": "[dim]client-only[/dim]",
        "unknown": "[red]NOT IN POLICY[/red]",
    }

    table = Table(show_header=True, header_style="bold")
    table.add_column("NODE", width=20)
    table.add_column("IP", width=18)
    table.add_column("INBOUND RULES", width=14, justify="right")
    table.add_column("STATUS", width=16)

    for r in rows:
        table.add_row(r["name"], r["ip"], str(r["inbound"]), styles[r["status"]])
    con.console.print(table)

    for tag in inert_tags:
        con.warning(
            f"{tag} matches no node — rules granting it are inert. "
            f"The policy looks correct and grants nothing: either no node carries "
            f"the tag yet, or it is a typo. Check the Tags column of "
            f"`bin/bay gateway nodes`."
        )

    if unresolvable:
        con.warning(
            f"Could not resolve {len(unresolvable)} target(s) to IPs: "
            f"{', '.join(unresolvable)} — group:/autogroup:/user@ targets have no IP "
            f"answer here, so reachability may be understated above."
        )

    con.info("client-only = nothing reaches it, but the policy names it as a src — deliberate.")

    if not unknown:
        con.success(f"All {len(rows)} node(s) are accounted for in the policy.")
        return

    names = ", ".join(r["name"] for r in unknown)
    con.error(f"{len(unknown)} node(s) the policy never names: {names}")
    try:
        rel = acl_file.relative_to(bay_dir.parent)
    except ValueError:
        rel = acl_file
    con.console.print(
        f"\n  These are enrolled and online but inert. Under default-deny an ungranted"
        f"\n  node isn't just unreachable — it isn't distributed as a peer at all, so it's"
        f"\n  missing from other nodes' [dim]tailscale status[/dim] and looks like a failed"
        f"\n  enrollment. Add a [bold]hosts:[/bold] alias and a rule naming it in"
        f"\n  [bold]{rel}[/bold], then:"
        f"\n"
        f"\n       [dim]bin/bay deploy {env} --tags headscale[/dim]"
        f"\n"
    )
    raise typer.Exit(1)


@app.command("add-user")
def add_user(
    name: str = typer.Argument(..., help="Username to create."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Create a new headscale user (headscale only).

    Users own devices; pre-auth keys are minted per user. For the common
    enroll-a-device flow, `bin/bay gateway enroll` does this step for you.

    Examples:

        bin/bay gateway add-user alice
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)
    _validate_username(name)

    con.header(f"Creating user: {name}")

    backend = _make_backend(bay_dir, env, region)
    try:
        backend.create_user(name)
        con.success(f"User '{name}' created.")
    except BayError:
        con.error(f"Failed to create user '{name}'.")
        raise typer.Exit(1)


@app.command()
def key(
    name: str = typer.Argument(..., help="Username to generate key for."),
    tag: list[str] | None = typer.Option(
        None,
        "--tag",
        help="ACL tag to stamp on the key (repeatable, e.g. --tag tag:agent).",
    ),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Generate a pre-auth key for a user (headscale only).

    The key is single-use and expires on headscale's own default lifetime
    (1h as of headscale 0.28) — use `bin/bay gateway enroll --expiry` for
    a longer window. An unused key needs no revoking — it just expires.

    With --tag the node joins ALREADY tagged, so tag-granted ACL rules apply
    from its first packet instead of after a manual `headscale nodes tag`.

    The key prints on one logical line whatever the terminal width, so a
    selection carries the whole token; press `c` to copy it instead.

    Examples:

        bin/bay gateway key alice
        bin/bay gateway key ci-runner --tag tag:agent
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)
    _validate_username(name)
    tags = _validate_tags(tag)

    con.header(f"Generating pre-auth key for: {name}")

    backend = _make_backend(bay_dir, env, region)
    try:
        user_id = backend.get_user_id(name)
    except BayError:
        con.error(f"User '{name}' not found in headscale.")
        raise typer.Exit(1)

    output = backend.generate_preauth_key(user_id, tags=tags)

    if output:
        show_ephemeral(
            f"\n  [bold]Pre-auth key for {name}:[/bold]\n\n  {output}\n",
            clipboard=output,
        )
    else:
        con.error("No key returned from headscale.")
        raise typer.Exit(1)


@app.command()
def apikey(
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    expiration: str = typer.Option("1y", "--expiration", help="Key lifetime (e.g. 90d, 24h, 1y)."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Generate a Headscale API key (headscale only).

    For remote-region registration (store as headscale_api_key in the vault
    `secrets:` dict) and other Headscale API consumers (e.g. the
    tailnet-identity sidecar). If the key expires, deploys to non-control
    regions fail at the tailscale_register step — re-mint and update the
    vault. Keys belong to the control host's Headscale state: rebuilding or
    relocating the control host invalidates every previously minted key
    (they 401), so re-mint there too.

    Examples:

        bin/bay gateway apikey
        bin/bay gateway apikey --expiration 90d
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)
    control_host = _get_control_host(bay_dir, region)

    if not _EXPIRATION_RE.match(expiration):
        con.error(
            f"Invalid expiration '{expiration}': use durations like 90d, 24h, 2w "
            "(units ms/s/m/h/d/w/y, largest first, no decimals)."
        )
        raise typer.Exit(1)

    con.header("Generating Headscale API key")

    backend = LocalHeadscaleBackend(env, bay_dir, limit=control_host)
    output = backend.create_api_key(expiration)

    if output:
        key = output
        content = (
            f"\n  [bold]API key:[/bold] {key}"
            f"\n"
            f"\n  [bold]Multi-region deployment:[/bold]"
            f"\n     Add to vault so remote regions can register via API:"
            f"\n"
            f"\n       bin/bay vault edit {env}"
            f"\n"
            f'\n     Then set: [bold]headscale_api_key: "{key}"[/bold]'
            f"\n"
        )
        show_ephemeral(content, clipboard=key)
    else:
        con.error("No API key returned from headscale.")
        raise typer.Exit(1)


@app.command()
def enroll(
    user: str | None = typer.Option(None, "--user", "-u", help="Username for enrollment."),
    hostname: str | None = typer.Option(
        None,
        "--hostname",
        help="Tailnet name for the device. Defaults to the enrollment user name.",
    ),
    no_hostname: bool = typer.Option(
        False,
        "--no-hostname",
        help="Don't pin a tailnet name; the device registers under its own hostname.",
    ),
    tag: list[str] | None = typer.Option(
        None,
        "--tag",
        help="ACL tag to stamp on the key, so the node joins pre-tagged (repeatable).",
    ),
    reusable: bool = typer.Option(False, "--reusable", help="Generate a reusable pre-auth key."),
    expiry: str = typer.Option(
        "24h",
        "--expiry",
        help="Key expiry duration (e.g. 24h, 7d). The key is single-use unless --reusable.",
    ),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Enroll a device: create user, generate key, print the join command.

    For devices OUTSIDE the Ansible inventory (laptops, phones, externally
    managed boxes). Inventory servers join the tailnet automatically during
    provision/deploy — don't enroll them here.

    What enroll does NOT do: it never touches the ACL policy. On a
    default-deny tailnet (headscale_acl_policy defined) the device comes
    online but is DEAD ON ARRIVAL until the policy names it — it isn't even
    distributed to other nodes as a peer, so it's absent from their
    `tailscale status` and looks like a failed enrollment. ACL rules are
    accept-only and DIRECTIONAL: add a hosts: alias for the device, a rule
    naming it as dst (so you can reach it), AND rules naming it as src for
    anything it must reach itself. Then apply and verify:

        bin/bay deploy production --tags headscale
        bin/bay gateway acl audit

    --tag is the exception, and the pattern for agent boxes: the tag is
    stamped on the pre-auth key, so the node joins ALREADY tagged and any
    class rules for that tag apply from its first packet — no per-device ACL
    edit, no manual `headscale nodes tag` afterwards. It only helps if rules
    for the tag already exist; a tag no rule names is as dead on arrival as
    no grant at all.

    --user names the OWNER in headscale; the device's tailnet name defaults
    to the same value (override with --hostname, or --no-hostname to keep
    the device's own). Keys are single-use unless --reusable.

    Examples:

        bin/bay gateway enroll --user laptop
        bin/bay gateway enroll --user alice --hostname alice-phone --expiry 7d
        bin/bay gateway enroll --user ci-runner --tag tag:agent --reusable --expiry 30d
    """
    from rich.prompt import Prompt

    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)

    if config.get("access_gateway") != "headscale":
        raise BayError.config(
            "Gateway enrollment requires access_gateway: headscale",
            hint="Configure headscale in group_vars/all/access_gateway.yml",
        )

    # Resolve username
    if not user:
        if con.is_json_mode() or con.is_yes_mode():
            raise BayError.config("--user is required in non-interactive mode")
        user = Prompt.ask("  Username for new device")
    _validate_username(user)

    if hostname and no_hostname:
        raise BayError.config("--hostname and --no-hostname are mutually exclusive")
    if hostname:
        _validate_username(hostname)

    # Guard before it reaches a --become shell on the control host, and so junk
    # fails crisply here instead of as an opaque headscale error over ansible.
    if not _EXPIRATION_RE.match(expiry):
        con.error(
            f"Invalid expiry '{expiry}': use durations like 24h, 7d, 2w "
            "(units ms/s/m/h/d/w/y, largest first, no decimals)."
        )
        raise typer.Exit(1)

    tags = _validate_tags(tag)

    domain = config.get("headscale_domain", "")
    backend = _make_backend(bay_dir, env, region)
    user_created = False

    # -- Step 1: Create user (idempotent) --
    if not con.is_json_mode():
        con.header(f"Enrolling: {user}")

    # Check if user already exists
    existing_users: list[str] = []
    user_id: str | None = None
    try:
        users_data = backend.list_users()
        for u in users_data:
            existing_users.append(u.get("name", ""))
            if u.get("name") == user:
                user_id = str(u["id"])
    except (json.JSONDecodeError, KeyError):
        pass

    if user in existing_users:
        con.info(f"User '{user}' already exists, skipping creation")
    else:
        try:
            backend.create_user(user)
            con.success(f"User '{user}' created")
            user_created = True
        except BayError:
            raise BayError.remote(
                f"Failed to create user '{user}'",
                hint="Check that headscale container is running on the target server",
            )
        # Look up the new user's ID
        try:
            user_id = backend.get_user_id(user)
        except BayError:
            con.error(f"User '{user}' not found in headscale.")
            raise typer.Exit(1)

    if user_id is None:
        try:
            user_id = backend.get_user_id(user)
        except BayError:
            con.error(f"User '{user}' not found in headscale.")
            raise typer.Exit(1)

    # -- Step 2: Generate pre-auth key (always fresh) --
    try:
        auth_key = backend.generate_preauth_key(
            user_id, expiry=expiry, reusable=reusable, tags=tags
        )
    except BayError:
        hint = f"User created but key generation failed. Run `bin/bay gateway key {user}` to retry."
        raise BayError.remote("Failed to generate pre-auth key", hint=hint)

    if not auth_key:
        hint = f"User created but key generation failed. Run `bin/bay gateway key {user}` to retry."
        raise BayError.remote("No key returned from headscale", hint=hint)

    # -- Step 3: Build enrollment command --
    # --user names the OWNER in headscale; without --hostname the device registers
    # under whatever hostname it happens to have, so `ssh <user>` resolves nothing.
    # Default them to the same name — enroll is one-user-per-device, so that's the
    # least surprising outcome and it keeps ACL aliases predictable.
    node_name = None if no_hostname else (hostname or user)
    join_cmd = f"tailscale up --login-server=https://{domain} --authkey={auth_key}"
    if node_name:
        join_cmd += f" --hostname={node_name}"

    acl_file = _find_acl_policy_file(bay_dir)

    if con.is_json_mode():
        con.emit_result(
            {
                "user": user,
                "created": user_created,
                "key": auth_key,
                "command": join_cmd,
                "hostname": node_name,
                "tags": tags,
                "default_deny": acl_file is not None,
                "acl_policy_file": str(acl_file) if acl_file else None,
            },
            command="gateway.enroll",
        )
        return

    con.success(f"Pre-auth key generated for '{user}'")
    if node_name:
        con.info(f"User '{user}' owns the device; its tailnet name will be '{node_name}'.")

    content = (
        f"\n  [bold]Run this on the device to enroll:[/bold]"
        f"\n"
        f"\n  {join_cmd}"
        f"\n"
    )
    show_ephemeral(content, clipboard=join_cmd)

    # After the ephemeral screen, never inside it: the alternate screen is torn down
    # on dismiss, and this guidance is not a secret — it needs to survive in scrollback.
    # A tagged key changes the whole ACL story, so it gets its own epilogue rather
    # than the per-device "add a hosts: alias" walkthrough, which would be wrong advice.
    if tags:
        _report_tagged_enrollment(tags, acl_file, bay_dir, node_name, env)
    elif acl_file:
        _warn_default_deny(acl_file, bay_dir, node_name, env)


# -- S1: User management --


@app.command()
def users(
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """List all headscale users with node counts.

    Examples:

        bin/bay gateway users
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)

    con.header("Gateway Users")

    backend = _make_backend(bay_dir, env, region)
    try:
        users_data = backend.list_users()
    except json.JSONDecodeError:
        con.error("Failed to parse user list from headscale.")
        raise typer.Exit(1)

    try:
        nodes_data = backend.list_nodes()
    except json.JSONDecodeError:
        con.error("Failed to parse node list from headscale.")
        raise typer.Exit(1)

    if not users_data:
        con.info("No users found.")
        return

    # Build per-user node counts
    user_nodes: dict[str, list[dict]] = {}
    for node in nodes_data:
        uname = (
            node.get("user", {}).get("name", "")
            if isinstance(node.get("user"), dict)
            else str(node.get("user", ""))
        )
        user_nodes.setdefault(uname, []).append(node)

    table = Table(show_header=True, header_style="bold")
    table.add_column("USER", width=20)
    table.add_column("NODES", width=12, justify="right")
    table.add_column("ONLINE", width=12, justify="right")
    table.add_column("CREATED", width=22)

    for u in users_data:
        name = u.get("name", "")
        created = _format_timestamp(u.get("created_at"))
        nodes_for_user = user_nodes.get(name, [])
        total = len(nodes_for_user)
        online = sum(1 for n in nodes_for_user if n.get("online", False))
        online_display = f"[green]{online}[/green]/{total}" if online else f"0/{total}"
        table.add_row(name, str(total), online_display, created)

    con.console.print(table)


@app.command("user-info")
def user_info(
    name: str = typer.Argument(..., help="Username to inspect."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Show details for a user and their nodes.

    Examples:

        bin/bay gateway user-info alice
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)
    _validate_username(name)

    backend = _make_backend(bay_dir, env, region)
    try:
        users_data = backend.list_users()
    except json.JSONDecodeError:
        con.error("Failed to parse user list from headscale.")
        raise typer.Exit(1)

    user_record = next((u for u in users_data if u.get("name") == name), None)
    if not user_record:
        con.error(f"User '{name}' not found.")
        raise typer.Exit(1)

    con.header(f"User: {name}")
    con.console.print(f"  ID:      {user_record.get('id', '')}")
    con.console.print(f"  Created: {_format_timestamp(user_record.get('created_at'))}")
    con.console.print()

    # Fetch nodes for this user
    try:
        nodes_data = backend.list_nodes()
    except json.JSONDecodeError:
        con.error("Failed to parse node list from headscale.")
        raise typer.Exit(1)

    user_nodes = [
        n for n in nodes_data
        if (
            n.get("user", {}).get("name", "")
            if isinstance(n.get("user"), dict)
            else str(n.get("user", ""))
        ) == name
    ]

    if not user_nodes:
        con.info("No nodes for this user.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("NODE", width=20)
    table.add_column("IP", width=18)
    table.add_column("STATUS", width=10)
    table.add_column("LAST SEEN", width=22)

    for node in user_nodes:
        node_name = node.get("given_name") or node.get("name", "")
        ip = _node_ip(node)
        online = node.get("online", False)
        status = "[green]online[/green]" if online else "[dim]offline[/dim]"
        last_seen = _format_timestamp(node.get("last_seen"))
        table.add_row(node_name, ip, status, last_seen)

    con.console.print(table)


@app.command("rename-user")
def rename_user(
    old_name: str = typer.Argument(..., help="Current username."),
    new_name: str = typer.Argument(..., help="New username."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Rename a headscale user.

    The user's devices keep their tailnet names — rename those separately
    with `bin/bay gateway rename-node`.

    Examples:

        bin/bay gateway rename-user alice bob
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)
    _validate_username(old_name)
    _validate_username(new_name)

    backend = _make_backend(bay_dir, env, region)
    # Existence pre-check, for a precise error. `rename_user` resolves the name
    # itself — passing an id here makes it look up a user literally named "3".
    try:
        backend.get_user_id(old_name)
    except BayError:
        con.error(f"User '{old_name}' not found in headscale.")
        raise typer.Exit(1)

    try:
        backend.rename_user(old_name, new_name)
        con.success(f"User '{old_name}' renamed to '{new_name}'.")
    except BayError:
        con.error(f"Failed to rename user '{old_name}'.")
        raise typer.Exit(1)


@app.command("delete-user")
def delete_user(
    name: str = typer.Argument(..., help="Username to delete."),
    force: bool = typer.Option(False, "--force", "-f", help="Delete even if user has active nodes."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Delete a headscale user.

    Refuses when the user still has nodes unless --force (which deletes the
    nodes too).

    Examples:

        bin/bay gateway delete-user alice
        bin/bay gateway delete-user alice --force -y
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)
    _validate_username(name)

    backend = _make_backend(bay_dir, env, region)

    # Check for existing nodes
    try:
        nodes_data = backend.list_nodes()
    except json.JSONDecodeError:
        con.error("Failed to parse node list from headscale.")
        raise typer.Exit(1)

    user_nodes = [
        n for n in nodes_data
        if (
            n.get("user", {}).get("name", "")
            if isinstance(n.get("user"), dict)
            else str(n.get("user", ""))
        ) == name
    ]

    if user_nodes and not force:
        con.error(f"User '{name}' has {len(user_nodes)} active node(s):")
        for n in user_nodes:
            node_name = n.get("given_name") or n.get("name", "")
            con.console.print(f"  - {node_name}")
        con.console.print()
        con.console.print("Remove nodes first, or use [bold]--force[/bold] to delete anyway.")
        raise typer.Exit(1)

    if not yes:
        msg = f"Delete user '{name}'"
        if user_nodes:
            msg += f" and their {len(user_nodes)} node(s)"
        typer.confirm(f"{msg}?", abort=True)

    # If force-deleting with nodes, remove nodes first
    if user_nodes and force:
        for n in user_nodes:
            node_id = n.get("id")
            node_name = n.get("given_name") or n.get("name", "")
            try:
                backend.delete_node(node_id)
            except BayError:
                con.error(f"Failed to remove node '{node_name}' (id={node_id}).")
                raise typer.Exit(1)

    try:
        user_id = backend.get_user_id(name)
    except BayError:
        con.error(f"User '{name}' not found in headscale.")
        raise typer.Exit(1)

    try:
        backend.delete_user(user_id)
        con.success(f"User '{name}' deleted.")
    except BayError:
        con.error(f"Failed to delete user '{name}'.")
        raise typer.Exit(1)


# -- S2: Node management --


@app.command("delete-node")
def delete_node(
    name: str = typer.Argument(..., help="Node name (given_name) to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Delete a node from the tailnet.

    Shows the node's details and prompts before deleting. If the policy in
    headscale_acl_policy names the node, clean up its hosts: alias and
    rules too.

    Examples:

        bin/bay gateway delete-node myphone
        bin/bay gateway delete-node myphone -y
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)

    backend = _make_backend(bay_dir, env, region)
    node = _get_node_by_name(name, backend)
    node_id = node.get("id")
    user = (
        node.get("user", {}).get("name", "")
        if isinstance(node.get("user"), dict)
        else str(node.get("user", ""))
    )
    ip = _node_ip(node)
    last_seen = _format_timestamp(node.get("last_seen"))

    if not yes:
        con.console.print(f"  Node:      [bold]{name}[/bold]")
        con.console.print(f"  User:      {user}")
        con.console.print(f"  IP:        {ip}")
        con.console.print(f"  Last seen: {last_seen}")
        con.console.print()
        typer.confirm(f"Delete this node?", abort=True)

    try:
        backend.delete_node(node_id)
        con.success(f"Node '{name}' deleted.")
    except BayError:
        con.error(f"Failed to delete node '{name}'.")
        raise typer.Exit(1)


@app.command("rename-node")
def rename_node(
    old_name: str = typer.Argument(..., help="Current node name (given_name)."),
    new_name: str = typer.Argument(..., help="New node name."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Rename a node in the tailnet.

    ACL hosts: aliases and split-DNS records key off the node name — if the
    node is referenced in headscale_acl_policy, update the policy and
    redeploy the headscale tag after renaming.

    ALSO: the node name IS the injected identity. tailnet_identity sends
    given_name as X-Tailnet-Device, so renaming changes what every
    identity_inject route reports. Any downstream allowlist keyed on the old
    name must be updated in the SAME operation — including configs Bay does
    not manage (user-owned .env files, other repos), which no deploy will
    fix for you. The usual failure is silent: the app keeps serving the
    device with reduced privileges rather than erroring, so a 200 response
    does not prove the rename was clean. See docs/tailnet-ingress.md.

    Examples:

        bin/bay gateway rename-node myphone phone
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)
    _validate_node_name(new_name)

    backend = _make_backend(bay_dir, env, region)
    node = _get_node_by_name(old_name, backend)
    node_id = node.get("id")

    try:
        backend.rename_node(node_id, new_name)
        con.success(f"Node '{old_name}' renamed to '{new_name}'.")
    except BayError:
        con.error(f"Failed to rename node '{old_name}'.")
        raise typer.Exit(1)


# -- S3: Route management --


@app.command()
def routes(
    node_name: str | None = typer.Option(None, "--node", "-n", help="Filter routes by node name."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """List all advertised routes across the tailnet.

    Shows approved, pending, and actively served subnet routes per node.

    Examples:

        bin/bay gateway routes
        bin/bay gateway routes --node myserver
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)

    con.header("Gateway Routes")

    backend = _make_backend(bay_dir, env, region)
    node_id = None
    if node_name:
        node_record = _get_node_by_name(node_name, backend)
        node_id = node_record.get("id")

    try:
        nodes_data = backend.list_node_routes(node_id=node_id)
    except json.JSONDecodeError:
        con.error("Failed to parse route list from headscale.")
        raise typer.Exit(1)

    # Flatten node routes into per-route rows
    rows: list[tuple[str, str, str, str, str, str]] = []
    for node in nodes_data:
        nname = node.get("given_name") or node.get("name", "")
        user = (
            node.get("user", {}).get("name", "")
            if isinstance(node.get("user"), dict)
            else str(node.get("user", ""))
        )
        nid = str(node.get("id", ""))
        approved = set(node.get("approved_routes") or [])
        available = set(node.get("available_routes") or [])
        serving = set(node.get("subnet_routes") or [])

        # Show all unique routes (union of all three sets)
        all_routes = sorted(approved | available | serving)
        for route in all_routes:
            is_approved = route in approved
            is_serving = route in serving
            status = "[green]approved[/green]" if is_approved else "[yellow]pending[/yellow]"
            primary = "[green]yes[/green]" if is_serving else ""
            rows.append((nid, route, nname, user, status, primary))

    if not rows:
        con.info("No routes found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("NODE ID", width=8, justify="right")
    table.add_column("PREFIX", width=20)
    table.add_column("NODE", width=18)
    table.add_column("USER", width=15)
    table.add_column("STATUS", width=12)
    table.add_column("SERVING", width=10)

    for row in rows:
        table.add_row(*row)

    con.console.print(table)


@app.command("route-approve")
def route_approve(
    node_name: str = typer.Argument(..., help="Node name (given_name) to manage routes for."),
    route: str = typer.Argument(..., help="Route prefix to approve (e.g. 10.0.0.0/8)."),
    revoke: bool = typer.Option(False, "--revoke", help="Revoke (remove) the route instead of approving it."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Approve or revoke an advertised route for a node.

    The route must already be advertised by the node (it appears as
    'pending' in `bin/bay gateway routes`) before it can be approved.

    Examples:

        bin/bay gateway route-approve mynode 10.0.0.0/8
        bin/bay gateway route-approve mynode 10.0.0.0/8 --revoke
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)

    backend = _make_backend(bay_dir, env, region)
    node_record = _get_node_by_name(node_name, backend)
    node_id = node_record.get("id")

    # Fetch current route state for this node
    try:
        nodes_data = backend.list_node_routes(node_id=node_id)
    except json.JSONDecodeError:
        con.error("Failed to parse route list from headscale.")
        raise typer.Exit(1)

    node_routes = nodes_data[0] if nodes_data else {}
    approved = set(node_routes.get("approved_routes") or [])
    available = set(node_routes.get("available_routes") or [])

    if revoke:
        if route not in approved:
            con.warning(f"Route {route} is not currently approved for {node_name}.")
            return
        approved.discard(route)
        action = "Revoking"
    else:
        if route not in available:
            con.error(f"Route {route} is not available on {node_name}.")
            con.console.print(f"  Available routes: {', '.join(sorted(available)) or 'none'}")
            raise typer.Exit(1)
        if route in approved:
            con.info(f"Route {route} is already approved for {node_name}.")
            return
        approved.add(route)
        action = "Approving"

    routes_csv = ",".join(sorted(approved))
    try:
        backend.approve_routes(node_id, routes_csv)
    except BayError:
        con.error(f"Failed to update routes for {node_name}.")
        raise typer.Exit(1)

    action_past = "revoked" if revoke else "approved"
    con.success(f"Route {route} {action_past} for {node_name}.")


# -- S4: Namespace migration --


def _read_stack_name(bay_dir: Path) -> str:
    """Read stack_name from consumer group_vars/all/main.yml."""
    import yaml

    consumer_root = bay_dir.parent
    main_file = consumer_root / "group_vars" / "all" / "main.yml"
    if not main_file.exists():
        con.error("group_vars/all/main.yml not found. Cannot determine stack_name.")
        raise typer.Exit(1)
    data = yaml.safe_load(main_file.read_text()) or {}
    stack_name = data.get("stack_name", "")
    if not stack_name:
        con.error("stack_name is not set in group_vars/all/main.yml.")
        raise typer.Exit(1)
    return stack_name


def _read_region(bay_dir: Path, target_region: str | None) -> str:
    """Read region from consumer group_vars if available."""
    import yaml

    if target_region:
        return target_region

    consumer_root = bay_dir.parent
    # Check for region-specific group_vars
    group_vars_dir = consumer_root / "group_vars"
    if not group_vars_dir.exists():
        return ""

    # Look for headscale_control_region in access_gateway config
    config = _get_gateway_config(bay_dir)
    control_region = config.get("headscale_control_region", "")
    if control_region:
        return control_region

    return ""


@app.command("migrate-namespace")
def migrate_namespace(
    from_user: str | None = typer.Option(None, "--from", help="Current Headscale user to rename (default: auto-detect old 'server' user)."),
    to_user: str | None = typer.Option(None, "--to", help="Target Headscale user name (default: stack_name from group_vars)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print plan without executing."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
    region: str | None = typer.Option(None, "--region", "-r", help="Target a specific region."),
) -> None:
    """Rename Headscale user and node hostnames to match stack_name.

    By default, migrates from the legacy "server" user to the current
    stack_name. Use --from/--to for custom renames (e.g. after changing
    stack_name). Safe to run multiple times -- skips resources that
    already match the target.

    Examples:

        bin/bay gateway migrate-namespace                    # server -> stack_name
        bin/bay gateway migrate-namespace --from old --to new
        bin/bay gateway migrate-namespace --dry-run
    """
    bay_dir = paths.find_bay_dir()
    config = _get_gateway_config(bay_dir)
    _require_headscale(config)

    stack_name = _read_stack_name(bay_dir)
    resolved_region = _read_region(bay_dir, region)
    old_user = from_user or "server"
    new_user = to_user or stack_name

    if old_user == new_user:
        con.error(f"Source and target user are the same: '{old_user}'")
        raise typer.Exit(1)

    backend = _make_backend(bay_dir, env, region)

    # Fetch current state
    try:
        users_data = backend.list_users()
    except (json.JSONDecodeError, BayError):
        con.error("Failed to fetch users from headscale.")
        raise typer.Exit(1)

    try:
        nodes_data = backend.list_nodes()
    except (json.JSONDecodeError, BayError):
        con.error("Failed to fetch nodes from headscale.")
        raise typer.Exit(1)

    user_names = {u.get("name", "") for u in users_data}
    old_user_exists = old_user in user_names
    new_user_exists = new_user in user_names

    # Determine user rename action
    user_action = ""
    if old_user_exists and not new_user_exists:
        user_action = "rename"
    elif old_user_exists and new_user_exists:
        user_action = "skip_both_exist"
    elif not old_user_exists and new_user_exists:
        user_action = "skip_already_done"
    else:
        user_action = "skip_neither"

    # Find VPS node(s) under old or new user.
    # After a user rename, Headscale may cache the old name in node responses,
    # so match against both old and new user names.
    match_user_names = {old_user, new_user}
    vps_nodes = [
        n for n in nodes_data
        if (
            n.get("user", {}).get("name", "")
            if isinstance(n.get("user"), dict)
            else str(n.get("user", ""))
        ) in match_user_names
    ]

    # Determine node rename actions
    node_actions: list[tuple[dict, str, str]] = []  # (node, action, target_name)
    for node in vps_nodes:
        current_name = node.get("given_name") or node.get("name", "")
        # Extract region from current name (format: {prefix}-{region})
        if "-" in current_name:
            node_region = current_name.rsplit("-", 1)[1]
        else:
            node_region = resolved_region or ""
        target_name = f"{new_user}-{node_region}" if node_region else new_user
        # Same gate as `nodes rename`: a name we are about to SET is
        # interpolated into a root shell by gateway_backend.rename_node. The
        # region half comes from an existing node's given_name, so it is not
        # operator-typed at this point. Consistency, not a known hole.
        _validate_node_name(target_name)
        if current_name != target_name:
            node_actions.append((node, "rename", target_name))
        else:
            node_actions.append((node, "skip", target_name))

    # Print plan
    con.header("Gateway Namespace Migration" + (" (dry-run)" if dry_run else ""))
    con.console.print()

    nothing_to_do = True

    # User section
    if user_action == "rename":
        con.console.print(f"  User:  [bold]{old_user}[/bold] -> [bold]{new_user}[/bold]")
        nothing_to_do = False
    elif user_action == "skip_both_exist":
        con.console.print(f"  User:  [dim]both '{old_user}' and '{new_user}' exist -- manual resolution needed[/dim]")
        con.warning(f"Both '{old_user}' and '{new_user}' users exist. Rename or delete one manually.")
    elif user_action == "skip_already_done":
        con.console.print(f"  User:  [dim]'{new_user}' already exists, no '{old_user}' user found -- nothing to do[/dim]")
    else:
        con.console.print(f"  User:  [dim]no '{old_user}' user found and no '{new_user}' user -- nothing to do[/dim]")

    # Node section
    for node, action, target_name in node_actions:
        current_name = node.get("given_name") or node.get("name", "")
        if action == "rename":
            con.console.print(f"  Node:  [bold]{current_name}[/bold] -> [bold]{target_name}[/bold]")
            nothing_to_do = False
        else:
            con.console.print(f"  Node:  [dim]'{current_name}' already matches target -- nothing to do[/dim]")

    if not vps_nodes:
        con.console.print(f"  Node:  [dim]no nodes found under '{old_user}' or '{new_user}'[/dim]")

    con.console.print()

    if nothing_to_do:
        con.info("Nothing to migrate -- resources are already namespaced.")
        return

    if dry_run:
        con.info("Run without --dry-run to apply.")
        return

    if not yes:
        typer.confirm("Apply migration?", abort=True)

    # Execute user rename
    if user_action == "rename":
        try:
            backend.rename_user(old_user, new_user)
            con.success(f"User '{old_user}' renamed to '{new_user}'.")
        except BayError as e:
            con.error(f"Failed to rename user: {e}")
            raise typer.Exit(1)

    # Execute node renames
    for node, action, target_name in node_actions:
        if action != "rename":
            continue
        current_name = node.get("given_name") or node.get("name", "")
        node_id = node.get("id")
        try:
            backend.rename_node(node_id, target_name)
            con.success(f"Node '{current_name}' renamed to '{target_name}'.")
        except BayError as e:
            con.error(f"Failed to rename node '{current_name}': {e}")
            raise typer.Exit(1)

    con.console.print()
    con.info("Migration complete. Re-deploy to confirm new names are persisted.")
