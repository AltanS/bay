"""Operator surface over the alert system.

Before this, nothing enumerated Bay's alert surface: learning what the
framework can alert about meant grepping nine roles. `alerts/registry.yml` made
that surface explicit; this turns it into a tool.

Design notes:

  * **Effective state, not configured state.** `bay alerts list` resolves the
    registry level against each recipient's `min_level` and the global mute
    list, then shows what will actually arrive. The gap between "what I
    configured" and "what gets delivered" is the whole reason the command
    exists — printing the YAML back would be useless.

  * **Never writes a secret value.** `recipient add` emits a
    `{{ secrets.<key> }}` reference and tells the operator to set the vault
    key. Per Bay's casing convention the key must be lowercase: uppercase
    names in `secrets:` are container env vars, and an uppercase spelling here
    resolves to undefined silently.

  * **`test` is a dry run by default.** A live test fired at a
    `min_level: critical` on-call recipient either pages a human or is filtered
    out and exercises nothing, so `--live` is an explicit gate.

  * **Config writes go through ruamel** (`StackConfig`), so comments in the
    consumer's group_vars survive.

Exit codes:
  0 — success
  1 — validation failure, or a doctor check found a problem
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Any

import typer

from bay_cli import console, paths
from bay_cli.errors import BayError

app = typer.Typer(help="Inspect and configure Bay's alert surface.", no_args_is_help=True)

_LEVELS = ("debug", "info", "warn", "critical")
_ALERTS_FILE = "group_vars/all/alerts.yml"


# ── Registry access ──────────────────────────────────────────────────────


def _framework_root() -> Path:
    """`<framework>/src/bay_cli/commands/alerts.py` -> `<framework>`."""
    return Path(__file__).resolve().parent.parent.parent.parent


def _load_registry() -> dict[str, Any]:
    path = _framework_root() / "alerts" / "registry.yml"
    if not path.is_file():
        raise BayError(f"Alert registry not found: {path}")
    from ruamel.yaml import YAML

    with path.open() as handle:
        data = YAML(typ="safe").load(handle)
    if not isinstance(data, dict) or not data:
        raise BayError(f"Alert registry is empty or malformed: {path}")
    return data


def _alert_module():
    """The shared resolver — the same code the templates route with.

    Loaded by path rather than reimplemented so the CLI cannot disagree with
    what a host actually does.
    """
    path = (
        _framework_root() / "roles" / "alert_channel" / "files" / "bay_alert.py"
    )
    spec = importlib.util.spec_from_file_location("bay_alert_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _level_rank(level: str) -> int:
    return _LEVELS.index(level) if level in _LEVELS else len(_LEVELS)


# ── Consumer config ──────────────────────────────────────────────────────


def _yaml():
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _config_path(root: Path) -> Path:
    return root / _ALERTS_FILE


def _load_config(root: Path) -> dict[str, Any]:
    path = _config_path(root)
    if not path.is_file():
        return {}
    with path.open() as handle:
        data = _yaml().load(handle)
    return data if isinstance(data, dict) else {}


def _save_config(root: Path, data: dict[str, Any]) -> None:
    path = _config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        _yaml().dump(data, handle)


def _recipients(config: dict[str, Any]) -> list[dict[str, Any]]:
    value = config.get("alert_recipients") or []
    return list(value) if isinstance(value, list) else []


def _disabled(config: dict[str, Any]) -> list[str]:
    value = config.get("alerts_disabled") or []
    return list(value) if isinstance(value, list) else []


def _enabled(config: dict[str, Any]) -> list[str]:
    """alerts_enabled — the force-on list that reverses a default-off alert."""
    value = config.get("alerts_enabled") or []
    return list(value) if isinstance(value, list) else []


def _apply_hint() -> None:
    """Every mutating command says exactly how to make the change take effect.

    alert_policy is in provision.yml as well as deploy.yml — the whole point of
    S4 — so both are named. Leaving the provision line out is how GH#33
    happened in the first place.
    """
    console.info("Apply with:")
    console.info("  bin/bay validate")
    console.info("  bin/bay deploy <env> --tags alert_policy")
    console.info("  bin/bay provision <env> --tags alert_policy")


# ── list ─────────────────────────────────────────────────────────────────


@app.command("list")
def list_alerts(
    recipient: str = typer.Option(None, "--recipient", help="Only this recipient."),
    level: str = typer.Option(None, "--level", help="Only alerts at or above this level."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show every alert with its effective per-recipient state."""
    registry = _load_registry()
    root = paths.consumer_root()
    config = _load_config(root)
    recipients = _recipients(config)
    muted = set(_disabled(config))
    forced = set(_enabled(config))
    resolver = _alert_module()

    if level and level not in _LEVELS:
        raise BayError(f"Unknown level {level!r} (expected one of {', '.join(_LEVELS)})")

    selected = [
        r for r in recipients
        if recipient is None or r.get("name") == recipient
    ]
    if recipient and not selected:
        raise BayError(f"No recipient named {recipient!r}")

    rows = []
    for alert_id in sorted(registry):
        entry = registry[alert_id] or {}
        alert_level = entry.get("level", "info")
        if level and _level_rank(alert_level) < _level_rank(level):
            continue
        if alert_id in muted:
            delivered_to: list[str] = []
            state = "muted"
        else:
            delivered_to = [
                r.get("name", "unnamed")
                for r in selected
                if alert_id
                in resolver.bay_recipient_alert_ids(registry, r, muted, forced)
            ]
            if delivered_to:
                state = "delivered"
            elif not entry.get("enabled_by_default", True):
                state = "default off"
            else:
                state = "no recipient"
        rows.append(
            {
                "id": alert_id,
                "level": alert_level,
                "state": state,
                "recipients": delivered_to,
                "summary": entry.get("summary", ""),
            }
        )

    if as_json:
        import json

        typer.echo(json.dumps({"alerts": rows, "recipient_count": len(selected)}, indent=2))
        return

    console.header(f"Alert surface ({len(rows)} of {len(registry)} alerts)")
    if not selected:
        console.warning(
            "No explicit recipients configured — legacy alert_webhook_url / "
            "docker_monitor_telegram_* sinks (if set) still receive everything."
        )
    for row in rows:
        target = ", ".join(row["recipients"]) if row["recipients"] else "—"
        console.info(
            f"{row['id']:<38} {row['level']:<9} {row['state']:<13} {target}"
        )


# ── enable / disable ─────────────────────────────────────────────────────


def _parse_duration(text: str) -> int:
    """'30m', '2h', '7d' -> seconds. Bare digits are seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    raw = text.strip().lower()
    if raw.isdigit():
        return int(raw)
    if len(raw) > 1 and raw[-1] in units and raw[:-1].isdigit():
        return int(raw[:-1]) * units[raw[-1]]
    raise BayError(f"Cannot parse duration {text!r} (expected e.g. 30m, 2h, 7d)")


def _match_ids(registry: dict[str, Any], pattern: str) -> list[str]:
    import fnmatch

    matched = sorted(a for a in registry if fnmatch.fnmatchcase(a, pattern))
    if not matched:
        raise BayError(
            f"No alert matches {pattern!r}. See `bin/bay alerts list` for valid IDs."
        )
    return matched


@app.command("disable")
def disable_alert(
    pattern: str = typer.Argument(..., help="Alert ID or glob, e.g. 'log.*'."),
    for_: str = typer.Option(
        None, "--for", help="Expiry, e.g. 24h. Required unless --permanent."
    ),
    permanent: bool = typer.Option(
        False, "--permanent", help="Mute with no expiry (discouraged)."
    ),
) -> None:
    """Mute one or more alerts."""
    registry = _load_registry()
    root = paths.consumer_root()
    config = _load_config(root)
    matched = _match_ids(registry, pattern)

    if not for_ and not permanent:
        raise BayError(
            "A mute needs an expiry: pass --for 24h, or --permanent to accept "
            "an open-ended one. Alerts are the only observability there is, so "
            "a mute set during an incident and forgotten looks exactly like a "
            "broken emitter."
        )

    disabled = _disabled(config)
    for alert_id in matched:
        if alert_id not in disabled:
            disabled.append(alert_id)
    config["alerts_disabled"] = sorted(disabled)

    if for_:
        config["alert_policy_mute"] = sorted(disabled)
        config["alert_policy_mute_until"] = int(time.time()) + _parse_duration(for_)
    elif permanent:
        console.warning(
            "Permanent mute: this alert will never fire again until re-enabled."
        )

    _save_config(root, config)
    console.success(f"Muted {len(matched)} alert(s): {', '.join(matched)}")
    _apply_hint()


@app.command("enable")
def enable_alert(
    pattern: str = typer.Argument(..., help="Alert ID or glob, e.g. 'log.*'."),
) -> None:
    """Un-mute one or more alerts."""
    registry = _load_registry()
    root = paths.consumer_root()
    config = _load_config(root)
    matched = set(_match_ids(registry, pattern))

    disabled = [a for a in _disabled(config) if a not in matched]
    config["alerts_disabled"] = sorted(disabled)
    if "alert_policy_mute" in config:
        config["alert_policy_mute"] = sorted(
            a for a in config.get("alert_policy_mute") or [] if a not in matched
        )

    _save_config(root, config)
    console.success(f"Un-muted {len(matched)} alert(s): {', '.join(sorted(matched))}")
    _apply_hint()


# ── doctor ───────────────────────────────────────────────────────────────


@app.command("doctor")
def doctor() -> None:
    """Diagnose the failure modes that have actually bitten."""
    registry = _load_registry()
    root = paths.consumer_root()
    config = _load_config(root)
    recipients = _recipients(config)
    resolver = _alert_module()
    problems: list[str] = []

    console.header("Alert configuration")

    # Duplicate delivery — the migration failure mode.
    targets: dict[str, str] = {}
    for entry in recipients:
        target = resolver.bay_recipient_target(entry)
        name = entry.get("name", "unnamed")
        if target in ("telegram:", "webhook:"):
            problems.append(f"{name}: no delivery target resolved (empty url/chat_id)")
            continue
        if target in targets:
            problems.append(
                f"{name} and {targets[target]} deliver to the same target — "
                f"every alert arrives twice"
            )
        targets[target] = name

    # Recipients no alert can reach.
    muted = set(_disabled(config))
    forced = set(_enabled(config))
    for entry in recipients:
        reachable = resolver.bay_recipient_alert_ids(registry, entry, muted, forced)
        if not reachable:
            problems.append(
                f"{entry.get('name', 'unnamed')}: no alert clears its min_level "
                f"({entry.get('min_level', 'info')}) — this recipient is dead config"
            )

    # Active mutes, with age and remaining TTL. A forgotten mute is GH#33 with
    # extra steps, so it must be visible rather than merely stored.
    until = int(config.get("alert_policy_mute_until") or 0)
    mutes = _disabled(config)
    if mutes:
        now = int(time.time())
        if until and until > now:
            remaining = until - now
            console.warning(
                f"{len(mutes)} active mute(s), expiring in {remaining // 3600}h "
                f"{(remaining % 3600) // 60}m: {', '.join(mutes)}"
            )
        elif until and until <= now:
            console.info(
                f"{len(mutes)} mute(s) listed but expired — they are inert. "
                f"Run `bin/bay alerts enable '*'` to clear the list."
            )
        else:
            problems.append(
                f"{len(mutes)} PERMANENT mute(s) with no expiry: {', '.join(mutes)}"
            )

    # Unknown IDs in the mute list.
    for alert_id in mutes:
        if alert_id not in registry:
            problems.append(f"Muted alert {alert_id!r} is not in the registry")

    if problems:
        for problem in problems:
            console.error(problem)
        raise typer.Exit(1)

    console.success(
        f"{len(recipients)} recipient(s), {len(registry)} known alerts, no problems found"
    )


# ── test ─────────────────────────────────────────────────────────────────


@app.command("test")
def test_alert(
    alert_id: str = typer.Argument("alerts.test", help="Alert ID to simulate."),
    recipient: str = typer.Option(None, "--recipient", help="Only this recipient."),
    live: bool = typer.Option(
        False,
        "--live",
        help="Actually deliver. Without this, reports would-be delivery only.",
    ),
) -> None:
    """Show — or with --live, prove — where an alert would be delivered."""
    registry = _load_registry()
    if alert_id not in registry:
        raise BayError(
            f"Unknown alert {alert_id!r}. See `bin/bay alerts list` for valid IDs."
        )

    root = paths.consumer_root()
    config = _load_config(root)
    muted = set(_disabled(config))
    forced = set(_enabled(config))
    resolver = _alert_module()

    selected = [
        r for r in _recipients(config)
        if recipient is None or r.get("name") == recipient
    ]
    would_receive = [
        r.get("name", "unnamed")
        for r in selected
        if alert_id in resolver.bay_recipient_alert_ids(registry, r, muted, forced)
    ]

    entry = registry[alert_id]
    console.header(f"{alert_id} ({entry.get('level')})")
    console.info(entry.get("summary", ""))

    if alert_id in muted:
        console.warning("This alert is currently muted — it would reach nobody.")

    if would_receive:
        console.info(f"Would deliver to: {', '.join(would_receive)}")
    else:
        console.warning("No explicit recipient would receive this alert.")
    console.info(
        "Legacy alert_webhook_url / docker_monitor_telegram_* sinks, if "
        "configured, receive every unmuted alert regardless."
    )

    if not live:
        console.info(
            "Dry run — nothing was sent. Re-run with --live to deliver. "
            "Note that a live test against a critical-only recipient either "
            "pages a human or is filtered out and proves nothing."
        )
        return

    # A control-node send would prove nothing about the nine rendered host
    # scripts, and a stale rendered script is exactly what GH#33 was.
    raise BayError(
        "--live is not implemented yet: it must invoke the RENDERED emitter on "
        "each host, because a control-node send would not exercise the scripts "
        "that actually deliver in production. Use the dry run for now."
    )
