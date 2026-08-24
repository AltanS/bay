"""Canonical alert adapters for bay's Python and control-node emitters.

The shell emitters share roles/alert_channel/templates/_notify.sh.j2. This is
the same contract for everything that cannot source bash:

  * roles/docker_monitor/templates/docker-monitor.py.j2 — included verbatim
    by a Jinja include, via a symlink in that role's templates/ directory.
  * roles/git_deploy/files/webhook/app.py — imported, via a symlink in the
    webhook build context.
  * filter_plugins/bay_filters.py — loaded by path, so the `uri` tasks that
    send "deploy complete"/"deploy failed" from the control node adapt their
    bodies with these exact rules.

One definition, three consumers. Two implementations that drift is the failure
mode this is designed to avoid, so tests/test_alert_channel.py asserts the bash
and Python adapters agree byte for byte on the same inputs.

IMPORTANT — this file is pulled verbatim into a Jinja template, so it must not
contain a Jinja delimiter: no doubled braces, no brace-percent, no brace-hash.
That rules out f-strings with literal braces; use .format() or concatenation.
tests/test_alert_channel.py enforces this — it is not a convention to remember.

Message format: Telegram HTML is the canonical internal representation (M55).
The tag vocabulary is closed — <b>, <code>, <pre> plus the &amp;/&lt;/&gt;
entities — which is what makes plain string substitution safe here.
"""

import json
import urllib.request

BAY_ALERT_MAX_CHARS = 3500
BAY_ALERT_TIMEOUT = 10


def bay_html_escape(text):
    """Escape untrusted text for embedding in a Telegram-HTML message.

    Ampersand first: the other two replacements introduce one. Telegram
    rejects malformed HTML with a 400 and the alert is lost, so anything
    that can contain a literal < or & must go through this.
    """
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def bay_html_unescape(text):
    """Inverse of bay_html_escape. Ampersand last — it is what the others use."""
    return (
        str(text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    )


def bay_to_mrkdwn(message):
    """Telegram HTML -> Slack mrkdwn."""
    out = str(message)
    for tag, repl in (
        ("<pre>", "```"),
        ("</pre>", "```"),
        ("<code>", "`"),
        ("</code>", "`"),
        ("<b>", "*"),
        ("</b>", "*"),
    ):
        out = out.replace(tag, repl)
    return bay_html_unescape(out)


def bay_strip_tags(message):
    """Telegram HTML -> plain text."""
    out = str(message)
    for tag in ("<pre>", "</pre>", "<code>", "</code>", "<b>", "</b>"):
        out = out.replace(tag, "")
    return bay_html_unescape(out)


def bay_clip(message, max_chars=BAY_ALERT_MAX_CHARS):
    """Clip an oversized payload deterministically.

    Campfire and Slack both reject messages past a limit, and a silently
    dropped alert is worse than a clipped one. Any tag fragment left dangling
    by the cut is removed so the result is still well-formed.
    """
    text = str(message)
    if len(text) <= max_chars:
        return text
    text = text[:max_chars]
    head, sep, tail = text.rpartition("<")
    if sep and ">" not in tail:
        text = head
    return text + "\n… [truncated]"


def bay_alert_body(message, fmt="campfire", max_chars=BAY_ALERT_MAX_CHARS):
    """Render the canonical message for one sink. Returns a str."""
    text = bay_clip(message, max_chars)
    if fmt == "campfire":
        return text
    if fmt == "slack":
        return json.dumps({"text": bay_to_mrkdwn(text)})
    return bay_strip_tags(text)


def bay_alert_content_type(fmt="campfire"):
    """The Content-Type that goes with bay_alert_body for this format."""
    if fmt == "campfire":
        return "text/html"
    if fmt == "slack":
        return "application/json"
    return "text/plain"


def bay_send_webhook(
    message,
    url,
    fmt="campfire",
    timeout=BAY_ALERT_TIMEOUT,
    max_chars=BAY_ALERT_MAX_CHARS,
    on_error=None,
):
    """POST one alert to the generic webhook sink. Never raises.

    A dead sink must not fail a deploy, a backup or a build, so every failure
    path returns False instead of propagating. `on_error` receives a short
    reason string when delivery fails.
    """
    if not url:
        return False
    try:
        body = bay_alert_body(message, fmt, max_chars).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": bay_alert_content_type(fmt)},
        )
        urllib.request.urlopen(request, timeout=timeout)
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        if on_error is not None:
            try:
                on_error(str(exc))
            except Exception:  # noqa: BLE001
                pass
        return False


# ── Recipients and routing (M106-S3) ────────────────────────────────────────
#
# The severity ladder is defined in
# docs/build-pipeline-observability-contract.md and shared with the alert
# registry. Ordering is what `min_level` compares against.
#
# IMPORTANT: routing is resolved at RENDER time, not on the host. Jinja calls
# bay_recipient_alert_ids() to turn (registry, recipient) into a flat list of
# IDs, and emits one bash `case` per recipient. The host never parses a
# recipient list, never compares levels and never matches globs — which is what
# keeps this file's bash twin small enough to stay byte-identical.

BAY_LEVELS = ("debug", "info", "warn", "critical")

BAY_ADAPTERS = ("telegram", "webhook")

# Presets for the generic webhook adapter. A consumer can instead supply
# content_type/transform/body_template directly; these three exist because
# they cover the sinks people actually reach for first.
BAY_WEBHOOK_PRESETS = {
    "campfire": {"content_type": "text/html", "transform": "html"},
    "slack": {"content_type": "application/json", "transform": "mrkdwn_json"},
    "raw": {"content_type": "text/plain", "transform": "text"},
}


def bay_level_index(level):
    """Position on the ladder. Unknown levels sort last so they are never
    silently dropped by a min_level comparison."""
    try:
        return BAY_LEVELS.index(level)
    except ValueError:
        return len(BAY_LEVELS)


def bay_level_meets(level, min_level):
    """True when an alert at `level` clears a recipient's `min_level`."""
    return bay_level_index(level) >= bay_level_index(min_level)


def bay_recipient_alert_ids(registry, recipient, disabled=None, enabled=None):
    """The alert IDs one recipient receives, resolved at render time.

    `registry` maps id -> mapping with a `level`. Returns a sorted list so
    generated output is deterministic — a non-deterministic render would churn
    every config_hash on every deploy.

    Three inputs decide whether an ID reaches this recipient, in strict
    precedence order:

      1. `disabled` (alerts_disabled)  force OFF. Always wins, so "mute this"
         means muted even for an alert the operator also opted into.
      2. `enabled` (alerts_enabled)    force ON. Overrides BOTH the registry's
         enabled_by_default AND this recipient's min_level, because naming an
         ID explicitly is unambiguous intent. Without this, opting back into an
         info alert on a warn recipient would silently do nothing.
      3. the default policy               the registry's enabled_by_default
         combined with the recipient's min_level.

    Rule 2 is why the pair is symmetric: alerts_disabled and alerts_enabled are
    both force lists, and the registry plus min_level is the policy in between.
    """
    muted = set(disabled or ())
    forced = set(enabled or ())
    min_level = recipient.get("min_level") or "info"
    out = []
    for alert_id in registry:
        if alert_id in muted:
            continue
        if alert_id in forced:
            out.append(alert_id)
            continue
        entry = registry[alert_id] or {}
        if not entry.get("enabled_by_default", True):
            continue
        if bay_level_meets(entry.get("level"), min_level):
            out.append(alert_id)
    return sorted(out)


def bay_normalize_recipient(recipient):
    """Fill in adapter defaults and resolve a webhook preset.

    Returns a new mapping; never mutates the input (Ansible vars are shared).
    """
    out = dict(recipient or {})
    config = dict(out.get("config") or {})
    adapter = out.get("adapter") or "webhook"
    out["adapter"] = adapter
    out.setdefault("min_level", "info")

    if adapter == "webhook":
        preset_name = config.get("format") or "campfire"
        preset = BAY_WEBHOOK_PRESETS.get(preset_name, BAY_WEBHOOK_PRESETS["campfire"])
        config.setdefault("content_type", preset["content_type"])
        config.setdefault("transform", preset["transform"])
        config.setdefault("method", "POST")
        config.setdefault("headers", {})
    out["config"] = config
    return out


def bay_transform_body(message, transform):
    """Render the canonical Telegram-HTML message for one sink."""
    if transform == "html":
        return message
    if transform == "mrkdwn":
        return bay_to_mrkdwn(message)
    if transform == "mrkdwn_json":
        return json.dumps({"text": bay_to_mrkdwn(message)})
    if transform == "json":
        return json.dumps({"text": bay_strip_tags(message)})
    return bay_strip_tags(message)


def bay_desugar_legacy(
    recipients=None,
    webhook_url="",
    webhook_format="campfire",
    telegram_token="",
    telegram_chat="",
):
    """Fold the pre-M106 variables into the recipient list.

    Existing consumers set `alert_webhook_url` and `docker_monitor_telegram_*`
    and nothing else; they must keep working byte for byte. Legacy entries are
    marked `legacy: true` so `bay alerts` can show them as such and so
    duplicate detection can tell an implicit recipient from an explicit one.

    Order matters: Telegram first, then the webhook, matching the historical
    fan-out order in bay_notify.
    """
    out = []
    if telegram_token and telegram_chat:
        out.append(
            bay_normalize_recipient(
                {
                    "name": "legacy-telegram",
                    "adapter": "telegram",
                    "min_level": "debug",
                    "legacy": True,
                    "config": {"bot_token": telegram_token, "chat_id": telegram_chat},
                }
            )
        )
    if webhook_url:
        out.append(
            bay_normalize_recipient(
                {
                    "name": "legacy-webhook",
                    "adapter": "webhook",
                    "min_level": "debug",
                    "legacy": True,
                    "config": {"url": webhook_url, "format": webhook_format},
                }
            )
        )
    for recipient in recipients or ():
        out.append(bay_normalize_recipient(recipient))
    return out


def bay_recipient_target(recipient):
    """A comparable identity for duplicate-delivery detection.

    The migration failure mode is an operator adding an explicit recipient that
    points at the chat the legacy variables already cover, so every alert fires
    twice. Comparing the delivery target catches that.
    """
    config = (recipient or {}).get("config") or {}
    if (recipient or {}).get("adapter") == "telegram":
        return "telegram:" + str(config.get("chat_id", ""))
    return "webhook:" + str(config.get("url", ""))
