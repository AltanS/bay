# Alerting

Bay sends operational alerts — container crashes, build failures, deploy
outcomes, disk pressure, backup failures — to **any number of recipients**.
A recipient is an adapter plus its configuration, with its own severity floor,
so criticals can page an on-call sink while chat keeps taking everything.

Every alert has a stable ID and a severity. `alerts/registry.yml` is the single
source of truth; `bin/bay alerts list` prints it with the effective
per-recipient state.

Alerts are **best-effort by design**: a dead recipient can never fail a deploy,
a backup, or a build.

## Quick start

```bash
bin/bay alerts list                 # every alert, and who currently gets it
bin/bay alerts doctor               # duplicate targets, dead config, live mutes
bin/bay alerts disable 'log.*' --for 24h
bin/bay alerts test deploy.failed   # dry run: where would this land?
```

## Recipients

```yaml
# group_vars/all/alerts.yml
alert_recipients:
  - name: chat
    adapter: webhook
    min_level: info
    config:
      url: "{{ secrets.alert_webhook_url }}"
      format: campfire          # campfire | slack | raw

  - name: oncall
    adapter: webhook
    min_level: critical         # only critical alerts page
    config:
      url: "{{ secrets.pagerduty_url }}"
      format: raw

  - name: ops-telegram
    adapter: telegram
    min_level: warn
    config:
      bot_token: "{{ secrets.TELEGRAM_BOT_TOKEN }}"
      chat_id: "{{ secrets.telegram_chat_id }}"
```

> **Vault key casing is load-bearing.** Bay's convention is that UPPERCASE keys
> in `secrets:` are container env vars and lowercase keys are Ansible role vars.
> `alert_webhook_url` is consumed as a role var, so an UPPERCASE spelling
> resolves to *undefined* silently and the feature simply never fires.

| Key | Default | Meaning |
|---|---|---|
| `name` | — | Required, unique. Used by `bay alerts` and in failure logs. |
| `adapter` | `webhook` | `webhook` or `telegram`. |
| `min_level` | `info` | Severity floor. See the ladder below. |
| `config` | `{}` | Adapter-specific; see below. |

### Severity ladder

```
debug  <  info  <  warn  <  critical
```

Defined once, in
[`build-pipeline-observability-contract.md`](build-pipeline-observability-contract.md),
and shared with the registry — two taxonomies over the same alerts would drift.
There is deliberately **no `error` tier**: nothing needs a distinction that
`warn` and `critical` do not already draw, and an unused tier is drift bait.

Severity answers *how bad*. Whether an alert exists at all is answered by the
presence of a registry entry — a terminal state classified `debug` emits a log
marker and no alert.

Escalation is modelled as a **second alert**, not a mutating severity:
`build.failed` (warn) fires, and `build.circuit_breaker_open` (critical) fires
separately when the breaker trips. Registry levels are static.

### Default-off alerts

Severity says *how bad*. It does not say *do I want to hear about it*, and
those came apart in practice: the `info` tier is success chatter — "deploy
finished", "webhook received", "pull signal handled". One `git push` produced
four of them. A channel where most messages are "it worked" trains you to skim,
which is the expensive failure: the `critical` you needed is in there, and you
scrolled past it.

So the whole `info` tier ships `enabled_by_default: false`. The one exception is
`alerts.test`, which exists purely to prove delivery — a test alert that
defaults to silent would be a trap.

The `Default` column in [What gets sent](#what-gets-sent) is the authority.

Nothing about severity changed, and no alert was deleted. `deploy.failed`
(critical) and `webhook.fanout_failed` (warn) are untouched — only the
success notices went quiet. If you want a "deploy finished" ping, opt in:

```yaml
alerts_enabled:
  - deploy.complete
```

**Recovery notices are default-off too**, and that is the one trade-off worth
naming out loud: `host.disk_recovered`, `host.outbound_restored` and
`container.recovered` are all `info`. Out of the box you get
`host.disk_page` with no "…and it cleared" follow-up. If you page on disk and
want the all-clear, put those three in `alerts_enabled`. They are listed
together in the table for exactly this reason.

### The webhook adapter is declarative

Adding a recipient type that speaks HTTP requires **no framework code**:

```yaml
  - name: ntfy
    adapter: webhook
    min_level: warn
    config:
      url: "{{ secrets.ntfy_url }}"
      method: POST                    # default POST
      content_type: text/plain
      transform: text                 # html | mrkdwn | mrkdwn_json | json | text
      headers:
        Title: "Bay alert"
        Authorization: "Bearer {{ secrets.ntfy_token }}"
```

`format:` is shorthand for a `content_type` + `transform` pair:

| `format` | Content-Type | Transform |
|---|---|---|
| `campfire` | `text/html` | message as-is (Campfire renders our tag subset) |
| `slack` | `application/json` | `{"text": "...mrkdwn..."}` |
| `raw` | `text/plain` | tags stripped, entities decoded |

Consumer-supplied adapter **code** is deliberately out of scope. Any code
extension point would have to ship both a bash and a Python half and pass the
byte-parity test in `tests/test_alert_channel.py` — which is precisely where
that guarantee dies. A declarative adapter that cannot drift beats an extension
point that silently does.

### Credentials supplied at runtime

When a credential arrives from a systemd unit's environment rather than being
baked in, use the `_env` forms:

```yaml
    config:
      url_env: BAY_HOOK_URL          # instead of url:
      token_env: TELEGRAM_BOT_TOKEN   # instead of bot_token:
      chat_id_env: TELEGRAM_CHAT_ID   # instead of chat_id:
```

## Muting alerts

Two mechanisms, deliberately different in cost:

**Global mute list** — config, needs a deploy:

```yaml
alerts_disabled:
  - log.retention_prune
```

**Operator mute with a TTL** — takes effect without re-rendering every emitter:

```bash
bin/bay alerts disable 'host.disk_warn' --for 24h
bin/bay deploy production --tags alert_policy
bin/bay provision production --tags alert_policy
```

**Every mute should have an expiry.** Alerts are the only observability there
is, so a mute set during an incident and forgotten is indistinguishable from a
broken emitter. `bay alerts disable` refuses an open-ended mute unless you pass
`--permanent`, and `bay alerts doctor` surfaces every active mute with its
remaining TTL.

**Global opt-in list** — the counterpart, for turning something back **on**:

```yaml
alerts_enabled:
  - deploy.complete
```

`alerts_enabled` overrides both the registry's `enabled_by_default` **and** the
recipient's `min_level`. The `min_level` override is deliberate: naming an ID
here is unambiguous intent, and an opt-in that silently did nothing on a `warn`
recipient would be the more surprising behaviour. Without this list, default-off
would be a one-way door — no consumer setting could bring `deploy.complete`
back.

Precedence, when the same ID appears in more than one place:

```
alerts_disabled  >  alerts_enabled  >  enabled_by_default + min_level
```

Mute wins. "Silence this" has to mean silenced, including for an alert someone
also opted into — otherwise a mute set during an incident is not a mute.

Per-recipient `include`/`exclude` globs are **reserved and rejected** by
validation. Use `min_level` plus the two global lists. Rejecting rather than
ignoring them means nobody ships config that silently does nothing.

## Migrating from the two-sink model

The legacy two-sink variables still work and need **no changes**:

```yaml
alert_webhook_url: "{{ secrets.alert_webhook_url }}"
alert_webhook_format: campfire
docker_monitor_telegram_bot_token: "{{ secrets.TELEGRAM_BOT_TOKEN }}"
docker_monitor_telegram_chat_id: "{{ secrets.TELEGRAM_CHAT_ID }}"
```

They desugar into implicit recipients with a `debug` floor, so they receive
**every** alert the host emits — including the `info` tier that is default-off
everywhere else.

**This is the reason to migrate.** The two legacy senders fire unconditionally
inside `bay_notify()`; they never consult the registry, so
`enabled_by_default: false` does not reach them. A consumer still on
`alert_webhook_url` keeps getting `webhook.received` on every push no matter
what the registry says. Only mutes apply to them, because a mute has to be
absolute.

Moving the same sink into `alert_recipients` changes nothing about where the
messages land, and everything about which ones are sent.

One asymmetry to know: `deploy.complete` and `deploy.failed` are emitted from
the **control node**, not the host (see [How it works](#how-it-works)), and
those two are routed through the registry for legacy and explicit recipients
alike. So a legacy consumer loses `deploy.complete` but keeps
`webhook.received` — which reads as inconsistent until you know that only the
host-side senders are grandfathered. The legacy pair is scheduled for removal;
migrating settles it.

Two traps, both caught by `bin/bay validate`:

**Duplicate delivery.** If you keep `alert_webhook_url` *and* add an explicit
recipient pointing at the same URL, every alert arrives twice. Either remove the
legacy variable or point the new recipient somewhere else.

**`group_vars` precedence.** `alert_webhook_url` usually lives in
`group_vars/<env>/main.yml`, while `bin/bay alerts` writes
`group_vars/all/alerts.yml`. Ansible precedence puts env **above** all, so the
legacy sink keeps firing alongside the new list. Validation warns; resolve it by
moving both to the same level.

## Which host sent it

Every alert names the machine it came from. Set that name per host:

```yaml
# group_vars/infra/main.yml  (or host_vars/<inventory-name>.yml)
bay_host_label: infra.bay.example.com
```

It is printed with the inventory address beside it, because a responder needs
both — the label to know *which* box, the address to reach it:

```
Host: infra.bay.example.com (203.0.113.42)
```

Leave it unset and Bay falls back to the machine's own hostname, then to the
inventory address:

| `bay_host_label` | `ansible_hostname` | inventory | alert shows |
|---|---|---|---|
| `infra.bay.example.com` | `example-infra` | `203.0.113.42` | `infra.bay.example.com (203.0.113.42)` |
| *(unset)* | `example-infra` | `203.0.113.42` | `example-infra (203.0.113.42)` |
| *(unset)* | *(no facts)* | `203.0.113.42` | `203.0.113.42` |
| *(unset)* | `bay-na` | `bay-na` | `bay-na` |

So an unlabelled host is never anonymous — it just gets a less pretty name.
The label and the address collapse to one field when they are already the
same string, so a consumer that configures nothing sees no change.

### Why this is one variable and not a convention

Bay used to let each emitter answer "which host?" for itself, and they
disagreed. Most printed `inventory_hostname` — a bare IP for any consumer
whose inventory lists addresses. `log_archive` shelled out to `hostname -f`.
`outbound_monitor` printed `region`, which names a region, not a machine: two
boxes in one region produced identical alerts.

Resolution now happens once, in `roles/alert_channel/templates/_host_label.j2`,
symlinked into every role that names a host — the same mechanism `_notify.sh.j2`
uses, and for the same reason (Ansible's template loader has no cross-role
include path). `tests/test_alert_channel.py` fails the build if an emitter
reaches for `inventory_hostname` or `region` directly, so a fourth answer
cannot reappear.

One deliberate exception: restic's `--tag` still uses `inventory_hostname`.
That tag keys the backup history and is data, not a display name — changing it
would orphan every existing snapshot.

## What gets sent

Generated from `alerts/registry.yml` — do not hand-edit. Run `make docs-alerts`.

<!-- BEGIN GENERATED ALERT TABLE -->

| Alert ID | Level | Default | Source | Summary |
|---|---|---|---|---|
| `alerts.test` | `info` | on | `alert_channel/bay alerts test` | A synthetic alert emitted by `bay alerts test` to prove delivery. |
| `backup.failed` | `critical` | on | `backup/backup.sh.j2` | A backup run failed — dump, restic, or a pre-flight check. |
| `backup.integrity_check_failed` | `critical` | on | `backup/maintenance.sh.j2` | A restic integrity check failed — the repository may be damaged. |
| `backup.prune_warning` | `warn` | on | `backup/backup.sh.j2` | Retention prune failed; it will be retried on the next run. |
| `backup.warning` | `warn` | on | `backup/backup.sh.j2` | A backup completed with a non-fatal problem. |
| `build.circuit_breaker_open` | `critical` | on | `git_deploy/rebuild.sh.j2` | Consecutive failures tripped the circuit breaker; auto-builds are off. |
| `build.failed` | `warn` | on | `git_deploy/rebuild.sh.j2` | A build, image pull, or webhook deploy failed; retries remain. |
| `build.fanout_failed` | `warn` | on | `git_deploy/rebuild.sh.j2` | Notifying a peer to pull the new image failed. |
| `build.health_check_failed` | `critical` | on | `git_deploy/rebuild.sh.j2` | The new container failed its health check and no rollback was available. |
| `build.killed` | `warn` | on | `git_deploy/build-alert.sh.j2` | A build was timed out or killed by systemd (timeout, OOM, signal). |
| `build.lock_timeout` | `warn` | on | `git_deploy/rebuild.sh.j2` | A concurrent build held the build lock past the timeout. |
| `build.pipeline_stalled` | `warn` | on | `git_deploy/bay-trigger-watchdog.sh.j2` | Build triggers are older than the stall threshold — the pipeline is stuck. |
| `build.push_blocked` | `warn` | on | `git_deploy/rebuild.sh.j2` | A push was rejected because the circuit breaker is open. |
| `build.remote_complete` | `info` | **off** | `git_deploy/rebuild.sh.j2` | A remote build finished and deployment servers were notified to pull. |
| `build.rollback_failed` | `critical` | on | `git_deploy/rebuild.sh.j2` | Rollback failed — the previous image would not start or was unhealthy. |
| `build.rolled_back` | `warn` | on | `git_deploy/rebuild.sh.j2` | Health check failed; the service was rolled back to the previous image. |
| `build.unknown_service` | `warn` | on | `git_deploy/rebuild.sh.j2` | A build trigger fired for a service not declared in services.yml. |
| `container.crash` | `warn` | on | `docker_monitor/docker-monitor.py.j2` | A container exited with a non-zero status. |
| `container.health_check_failed` | `warn` | on | `docker_monitor/docker-monitor.py.j2` | A container's Docker health check reported unhealthy. |
| `container.recovered` | `info` | **off** | `docker_monitor/docker-monitor.py.j2` | A previously crashed container started again. |
| `container.restart_loop` | `critical` | on | `docker_monitor/docker-monitor.py.j2` | A container restarted repeatedly inside the detection window. |
| `deploy.complete` | `info` | **off** | `deploy_stack/main.yml` | A deploy finished successfully. |
| `deploy.failed` | `critical` | on | `deploy_stack/main.yml` | A deploy failed. |
| `deploy.pull_complete` | `info` | **off** | `git_deploy/rebuild.sh.j2` | A pull-only or remote-pull deploy finished successfully. |
| `deploy.webhook_complete` | `info` | **off** | `git_deploy/rebuild.sh.j2` | A webhook-triggered build and deploy finished successfully. |
| `host.disk_page` | `critical` | on | `outbound_monitor/bay-disk-alert.sh.j2` | Disk usage crossed the page threshold. |
| `host.disk_recovered` | `info` | **off** | `outbound_monitor/bay-disk-alert.sh.j2` | Disk usage fell back below the warn threshold. |
| `host.disk_warn` | `warn` | on | `outbound_monitor/bay-disk-alert.sh.j2` | Disk usage crossed the warn threshold. |
| `host.outbound_lost` | `critical` | on | `outbound_monitor/bay-outbound-check.j2` | The host lost outbound internet while remaining reachable over Tailscale. |
| `host.outbound_restored` | `info` | **off** | `outbound_monitor/bay-outbound-check.j2` | Outbound internet came back. |
| `log.retention_prune` | `info` | **off** | `log_archive/rotate-logs.sh.j2` | Log retention removed archived files (age or size threshold). |
| `webhook.fanout_failed` | `warn` | on | `git_deploy/webhook/app.py` | Forwarding a webhook to a peer region failed. |
| `webhook.image_pull_signal_received` | `info` | **off** | `git_deploy/webhook/app.py` | A registry image-pull signal arrived and triggered pulls. |
| `webhook.pull_signal_received` | `info` | **off** | `git_deploy/webhook/app.py` | A peer signalled that a new image is ready to pull. |
| `webhook.received` | `info` | **off** | `git_deploy/webhook/app.py` | A push webhook arrived and a build was triggered. |

<!-- END GENERATED ALERT TABLE -->

## How it works

There is **one** implementation of "send an alert", in two languages because
bash cannot import Python and the webhook container cannot source bash:

- `roles/alert_channel/templates/_notify.sh.j2` defines `bay_notify()` for
  every shell emitter. It is **symlinked** into each consuming role's
  `templates/` directory, because Ansible's template loader searches only the
  current role's `templates/` — there is no cross-role include path. Git stores
  the links as mode `120000`, and `bin/bay install` is a `git clone` +
  `git checkout`, so they survive installation intact.
- `roles/alert_channel/files/bay_alert.py` does the same for Python: included
  verbatim into `docker-monitor.py.j2`, imported by the webhook receiver, and
  loaded by path in `filter_plugins/bay_filters.py` and by the CLI.

`tests/test_alert_channel.py` asserts the two agree byte for byte on the same
inputs. Divergence is the failure mode this design exists to prevent.

There is a **third** emitter, and it is the odd one out.
`deploy.complete` and `deploy.failed` are sent from the **control node** while
Ansible is mid-play, so they cannot call the host-side `bay_notify()` at all.
They live in `roles/deploy_stack/tasks/send_deploy_alert.yml` as `uri` tasks
that resolve recipients through the same filters
(`bay_alert_recipients` + `bay_alert_ids_for`).

Before that file existed, those two alerts were hard-coded POSTs to the legacy
variables, guarded only by "is the URL set". They were unroutable and
**unmutable**: `_bay_alert_id` was set beside them purely to satisfy the
registry drift test, and nothing read it, so
`bin/bay alerts disable deploy.complete` silently did nothing. If you are
reading this because a deploy alert ignored your config, that was the bug.

### Call sites pass a literal ID

`bay_notify <alert.id> "<message>"`. The ID must be a bare literal — never a
variable — and `tests/test_alert_registry.py` enforces that. The registry drift
check works by scanning for literals, so a variable would make it under-report
and every check would pass on partial data. The single sanctioned exception is a
wrapper forwarding an ID it was handed, marked with an explicit
`# bay-alert-id: forwarder` comment.

### Routing is resolved at render time

Jinja compares each alert's registry level against each recipient's `min_level`
and bakes a **literal ID list** into one bash `case` per recipient. The host
does a single glob match — it never parses a recipient list, never compares
levels, and needs no `jq` (which no framework task installs; it rides on the
base image).

The rejected alternative was shipping the recipient list to the host and
interpreting it in bash. That is ~150 lines of list parsing, level ordering and
glob matching, every line of which would need a byte-identical Python twin — in
a snippet that already carries two documented Jinja/bash landmines. Bash `case`
globs and Python `fnmatch` also disagree on character classes, so the matcher
itself becomes a parity liability.

### The override file is parsed, never sourced

`/etc/bay/alert-overrides` is root-owned and carries **mute state only** — no
credentials, no recipient definitions, no URLs. Readers extract three
whitelisted keys.

Sourcing it would be local root code execution: the scripts that read it run
from privileged cron and backup, and an earlier draft of this design put the
file under `${stack_dir}/state`, which is created mode **0777** so the webhook
container can write it.

**Fail-open has a direction.** Absence or corruption of this file may only
produce *more* alerts, never fewer. Every failure path — missing, empty,
truncated, unknown schema major, malformed epoch, unreadable — yields no mutes.
The inverse would let a failed `mv` silence the entire fleet.

The `alert_policy` role that writes it is included by **both** `provision.yml`
and `deploy.yml`, so a mute is never stranded in the playbook an operator did
not run. That is the structural fix for the GH#33 class of bug.

## Guarantees

- **Fail-open.** Delivery failures are swallowed and, where a failure log is
  configured, recorded to `${stack_dir}/state/telegram-failures.log`. A build
  never fails because chat was down.
- **Off by default, and inert when off.** No recipients means zero extra
  outbound calls and zero log noise.
- **No container churn.** The webhook receiver's `ALERT_WEBHOOK_*` env keys use
  Ansible's `omit` when the feature is off, so the container spec — and its
  `config_hash` — is unchanged for consumers who never enable it.
- **Untrusted text is escaped.** Build-failure alerts embed a 500-byte tail of
  raw build output. It goes through `bay_html_escape` first: unescaped, a `<`
  or `&` produces malformed HTML, Telegram rejects it with a 400, and the
  failure alert is lost exactly when it matters most.

### Secret hygiene, honestly

Rendered emitter scripts in `/usr/local/bin` are mode `0755` with the Telegram
token inlined. That predates this work and is unchanged by it. The override file
carries no credentials, so it does not make this worse — but Bay does not
currently claim host-local secrecy for alert credentials, and this document will
not pretend otherwise.

## Not covered

Deliberately out of scope, and **not** part of the recipient list:

- **Watchtower** container-update notifications use shoutrrr's own URL scheme
  (`WATCHTOWER_NOTIFICATION_URL`, set in
  `roles/container_lifecycle/tasks/build_specs.yml`). shoutrrr accepts multiple
  URLs, so bridging is a matter of appending `slack://` or `generic://`.
- **`bay build` audit notifications** sent from the operator's own machine
  (`src/bay_cli/commands/build.py`) — they fire from a laptop, not a host.

Bay deliberately does *not* standardise on shoutrrr: that would put a Go binary
on every host to replace ~20 lines of curl we fully control, and its `generic://`
templating fights Campfire's `text/html` body.

## Troubleshooting

**Start here:**

```bash
bin/bay alerts doctor
bin/bay alerts list --recipient <name>
bin/bay alerts test <alert.id>
```

**Nothing arrives.** Check the URL is non-empty and the vault key is lowercase
(see above). Then confirm the alert clears the recipient's `min_level` —
`alerts list` shows effective state, which is usually the answer. Delivery
failures land in `${stack_dir}/state/telegram-failures.log`.

**One alert never arrives, others do.** It is probably below that recipient's
`min_level`, or muted. `bin/bay alerts test <id>` says exactly where it would
land.

**Some alerts arrive, disk-pressure and outbound ones do not.** `outbound_monitor`
lives in `provision.yml`, so `bin/bay deploy` never re-renders it (GH#33). The
`alert_policy` role is in **both** playbooks precisely to stop mutes being
stranded this way, but the emitter itself still needs:

```bash
bin/bay provision production --tags outbound_monitor
```

**Alerts arrive twice.** An explicit recipient points at the same target as the
legacy `alert_webhook_url`. `bin/bay validate` and `alerts doctor` both flag it.

**A muted alert is still firing.** Its TTL expired — an expired mute is inert,
by design. Re-mute with a new `--for`.

**Alerts are truncated.** Raise `alert_webhook_max_chars`, but check your sink's
own limit first — clipping exists because a silently rejected alert is worse
than a clipped one.
