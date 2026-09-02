# Build Pipeline Observability Contract

## Invariant

Every push that enters the build pipeline MUST terminate in a state that is
observable to the operator without tailing `journald` — either a Telegram
message or a `log-only-debug` stdout marker that is known to be benign.

"Silent failure" — a terminal state that emits no Telegram and is not
pre-classified as `log-only-debug` — is a framework defect. This contract
is the authoritative reference that Phase 2–6 work must conform to.

This document was written in response to the 2026-04-16 `blog` /
`argo-na` incident,  <!-- legacy-argo: historical host name -->
in which a tripped circuit breaker silently swallowed
pushes for 2+ hours because the CB-OPEN `exit 0` path emitted only a
journald log line, and the webhook container logs looked identical to a
successful pull from the outside.

## Severity levels

Every terminal state in `rebuild.sh` is classified into exactly one of:

| Severity               | When to use                                                                                   | Required emission                                                        |
| ---------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `log-only-debug`       | Benign/expected skips that an operator doesn't need to see in real time                       | Exactly one prefix-categorized `[rebuild] ...` stdout line               |
| `Telegram-info`        | Successful terminal state — deploy/build completed, or orderly rollback                       | ✅ / ⏪ Telegram message + categorized stdout line                        |
| `Telegram-warn`        | Something failed OR a push was rejected, but the service state is recoverable without urgency | ⚠️ / 🚨 Telegram message including the recovery command + stdout line    |
| `Telegram-critical`    | Manual intervention required, or the service is in a degraded/unsafe state                    | 🛑 Telegram message including the next action for the operator          |

The names above are historical and transport-flavoured — they were coined when
Telegram was the only sink. `alerts/registry.yml` classifies alerts from *every*
emitter, not just `rebuild.sh`, and Telegram is one adapter among several. Both
therefore speak the transport-neutral ladder below, and the four names above are
retained as aliases so no row in the exit-path map changes meaning.

### Severity ladder

The authoritative ordering, used for every `min_level` comparison in the alert
recipient config (see `docs/alerting.md`):

```
debug  <  info  <  warn  <  critical
```

| Level      | Alias in this document | Meaning                                                                    |
| ---------- | ---------------------- | -------------------------------------------------------------------------- |
| `debug`    | `log-only-debug`       | Benign/expected. Observable in logs; below every recipient's default floor. |
| `info`     | `Telegram-info`        | Successful terminal state.                                                  |
| `warn`     | `Telegram-warn`        | Something failed or was rejected; recoverable without urgency.              |
| `critical` | `Telegram-critical`    | Manual intervention required, or the service is degraded/unsafe.            |

**Four tiers, not five.** An `error` tier between `warn` and `critical` was
considered and rejected: nothing in this contract or any other emitter needs a
distinction that `warn` and `critical` do not already draw, and a ladder tier no
alert uses is exactly the kind of unused vocabulary that invites drift. Adding a
tier later is a backwards-compatible change; removing a used one is not.

**Severity and alertability are different questions.** `log-only-debug` has
historically answered both at once — "not urgent" *and* "emits no alert". They
are now separate: the ladder answers *how bad*, and the presence of a registry
entry answers *whether an alert exists at all*. A terminal state classified
`debug` emits its stdout marker and has **no** registry entry, which is why the
`alert_id` column below reads `—` for those rows. `debug` remains the floor of
the ladder so that `min_level: debug` unambiguously means "everything".

**Escalation is a second alert, not a mutating severity.** Several rows below are
classified `Telegram-warn` → `-critical` at trip. That is not one alert changing
level — it is `build.failed` (warn) firing, and then `build.circuit_breaker_open`
(critical) firing as a distinct alert when `_record_failure` hits the threshold.
Each has its own registry entry and its own level. A registry level is static.

Categorized stdout markers (one of):

- `[rebuild] Pulling ...`
- `[rebuild] Building ...`
- `[rebuild] Pushing ...`
- `[rebuild] Pushed ...`
- `[rebuild] Completed ...`
- `[rebuild] Circuit breaker OPEN ...`
- `[rebuild] ... pull-only but no pull signal ...`
- `[rebuild] Unknown service ...`
- `[rebuild] Container healthy`
- `[rebuild] Container unhealthy`
- `[rebuild] Rolled back ...`

Absence of one of these on an invocation is a framework bug (see Phase 1
CI enforcement task).

## Exit-path map (rebuild.sh.j2)

Line numbers are authoritative as of the current working tree (HEAD after
the path-#5 Telegram fix and the explicit trailing `exit 0`). The
`test_observability_contract.py` harness enforces this map — any drift
between the template and this table fails CI.

| # | Line  | Exit | Terminal state                                  | Current log marker                                    | Current Telegram                                       | Required severity            | Conforms? | Notes                                                                                                                  | alert_id                     |
| - | ----- | ---- | ----------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------ | ---------------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------- |
| 1 | 382   | 1    | Rollback: no previous image available           | `_record_failure` stdout                              | 🚨 Health check FAILED — no rollback available          | `Telegram-critical`          | ✅        | Correct                                                                                                                | build.health_check_failed    |
| 2 | 402   | 1    | Rollback: previous image wouldn't start         | `_record_failure` stdout                              | 🛑 ROLLBACK FAILED — container would not start          | `Telegram-critical`          | ✅        | Correct                                                                                                                | build.rollback_failed        |
| 3 | 417   | 1    | Rollback: previous image also unhealthy         | `_record_failure` stdout                              | 🛑 ROLLBACK FAILED — previous image also unhealthy      | `Telegram-critical`          | ✅        | Correct                                                                                                                | build.rollback_failed        |
| 4 | 435   | 1    | Rollback succeeded                              | `[rebuild] Container healthy`                         | ⏪ Rolled back to previous (+ ⚠ CB preview when N<MAX)  | `Telegram-warn`              | ✅        | Phase 3 appended a `⚠ Circuit breaker at N/MAX — next failure will block pushes. Reset: bay build reset <svc>` line to the Telegram body when below threshold.  | build.rolled_back            |
| 5 | 633   | 1    | Unknown service name passed to rebuild.sh       | `[rebuild] Unknown service: ${SERVICE}`               | ⚠️ Unknown service in build trigger — with triggers/ + systemd list commands | `Telegram-warn`              | ✅        | Was a silent `exit 1` until the path-#5 fix landed. Now alerts on stale triggers, misconfigured webhooks, etc.         | build.unknown_service        |
| 6 | 682   | 0    | CB-OPEN — push blocked                          | `[rebuild] Circuit breaker OPEN ...` + reset command  | ⚠️ Push blocked — CB OPEN, rate-limited 1/hour          | `Telegram-warn` rate-limited | ✅        | Phase 3 replaced the silent `exit 0` with a Telegram alert rate-limited via `alerts.last_blocked_alert_at` (epoch seconds; `CB_BLOCKED_ALERT_INTERVAL_SEC=3600`). First blocked push fires; subsequent pushes within 3600s are suppressed and log a `[rebuild] Suppressed CB blocked-push alert ...` marker. | build.push_blocked           |
| 7 | 702   | 1    | Build lock file unopenable                      | `[rebuild] Cannot open build lock <path> — aborting (check directory ownership)` | NONE                                                | `log-only-debug`             | ✅        | Issue #9 follow-up. The lock path defaults to `{{ stack_dir }}/build.lock` (bay-owned). On the first deploy after upgrade (v0.88.0 attempted `/run/argo-build.lock` and failed because /run is root-owned), this guard catches a stack with broken `stack_dir` ownership and aborts before flock — surfacing the cause, not a cryptic redirection error from set -e. <!-- legacy-argo: historical pre-rename path --> | —                            |
| 8 | 711   | 1    | Build lock wait timed out                       | `[rebuild] Lock wait timed out after Ns — aborting`   | ⏱️ Build lock timeout — points at `journalctl -u bay-build@*` for hung-build investigation | `Telegram-warn`              | ✅        | Issue #9. Cross-service `flock` on `git_deploy_build_lock_path` serializes concurrent path-unit firings. Bounds deadlock on a hung peer build to `git_deploy_build_lock_timeout` seconds (default 3600). Trap is cleared before exit so the ERR-trap row 16 doesn't double-fire. | build.lock_timeout           |
| 9 | 724   | 1    | `docker pull` failed                            | `_record_failure("","Image pull")` stdout             | 🚨 "Image pull failed (N/MAX)" via `_record_failure` dedup; 🛑 CB OPEN at trip | `Telegram-warn` → `-critical` at trip | ✅        | `_record_failure` already fires first-SHA and CB-trip correctly. No change needed if Phase 3 tests confirm.            | build.failed                 |
| 10| 812   | 0    | Pull deploy completed (pull-only or remote-pull)| `[rebuild] Pull deploy complete for ${SERVICE}`       | ✅ Pull deploy complete                                 | `Telegram-info`              | ✅        | Correct                                                                                                                | deploy.pull_complete         |
| 11| 820   | 0    | Pull-only guard, no pull signal present         | `[rebuild] Service ${SERVICE} is pull-only but no pull signal detected — skipping` | NONE                    | `log-only-debug`             | ✅        | Benign skip — pull-only services should not receive plain push webhooks; the guard exists for safety.                  | —                            |
| 12| 916   | 1    | Remote build failed                             | `_record_failure("${SHA}","Remote build",TAIL)`        | 🚨 Remote build failed (N/MAX) with 500-char tail      | `Telegram-warn` → `-critical` at trip | ✅        | Correct                                                                                                                | build.failed                 |
| 13| 1037   | 0    | Remote build succeeded (+ fan-out)              | `[rebuild] Completed remote build`                    | ✅ Remote build complete (+ per-peer ⚠ if fan-out fails) | `Telegram-info` (success) / `Telegram-warn` (per-peer fan-out fail) | ✅        | Correct. A follow-up change inserted a post-push local-image prune block (~44 lines) above this row, shifting it from 846 → 890. Issue #9 added the cross-service flock block above, shifting again from 890 → 921. The registry-cache change (remote strategy builds with `buildx --push`, optional `--cache-to`/`--cache-from` behind `git_deploy_registry_cache`) shifted 1010 → 1013. | build.remote_complete        |
| 14| 1099  | 1    | Local build failed                              | `_record_failure("${SHA}","Build",TAIL)`               | 🚨 Build failed (N/MAX) with 500-char tail             | `Telegram-warn` → `-critical` at trip | ✅        | Correct                                                                                                                | build.failed                 |
| 15| 1214  | 0    | Local build succeeded                           | `[rebuild] Completed rebuild for ${SERVICE}`          | ✅ Webhook deploy complete                              | `Telegram-info`              | ✅        | Trailing `exit 0` added so this terminal state is captured by the CI contract test. v0.82.6 shifted 1053 → 1066; issue #9 shifted 1066 → 1093; the lock-open guard then shifted 1093 → 1097. | deploy.webhook_complete      |
| 16| ERR trap | 1 | Unhandled shell error (via `set -e` trap)       | `_deploy_failed` → `_record_failure("${SHA}","Webhook deploy")` | 🚨 Webhook deploy failed (N/MAX)                      | `Telegram-warn` → `-critical` at trip | ✅        | Catches unexpected failures. Remote and local build paths explicitly `trap - ERR` before their own `exit 1` to avoid duplicate notification. | build.failed                 |

## External failure channel: `bay-build-alert@.service`

Fires via `OnFailure=` on the per-service build unit. Covers systemd-level
kills only: `timeout`, `oom-kill`, `signal`. Explicitly skips `Result=exit-code`
and `Result=success` because `rebuild.sh` handles those itself.

**Historical gap (closed):** Path #5 (`exit 1` on unknown service) results
in `Result=exit-code`, which `bay-build-alert` skips. Previously no other
channel fired either, making this silent. `rebuild.sh` now calls
`notify_build` before the exit.

**Note on naming:** the notifier was called `send_telegram` until alerts
gained a second sink. It is now `notify_build` — a thin wrapper that appends
the build correlation ID and delegates to `bay_notify`, the shared fan-out
defined in `roles/alert_channel/templates/_notify.sh.j2`. Telegram behaviour
is unchanged; see `docs/alerting.md`.

Since the alert routing rework both take the alert's registry ID as their first argument:
`notify_build <alert_id> <message>`. The ID must be a bare literal at every
call site — `tests/test_alert_registry.py` enforces it, because the registry
drift check scans for literals and a variable would make it under-report in
silence. The `alert_id` column above is the join key between this map and
`alerts/registry.yml`.

## External failure channel: `bay-trigger-watchdog.service`

A second, *proactive* OnFailure-like channel. Unlike `bay-build-alert@`,
which is event-driven (fires when a build unit fails), the stall watchdog
is **timer-driven**: a 5-minute systemd timer scans
`${STACK_DIR}/triggers/` for `*.trigger` files older than
`git_deploy_stall_watchdog_threshold` (default 600 s) and emits a single
Telegram alert listing the stale services, their age, and the recovery
commands (`bay build reset <svc>` and the manual `rm` path).

This exists to catch the "invisible" class of failures where
`rebuild.sh` never runs at all: the `.path` unit stalls, the
`bay-build@<svc>.service` fails *before* its `ExecStartPost=rm -f`
cleanup could run, or an operator writes a trigger by hand that never
gets picked up.

The watchdog does **not** route through `bay-build-alert@.service`. Its
service unit deliberately has no `OnFailure=` directive, and the script
is hardened to always `exit 0` — every internal error path is swallowed
and recorded to `${STACK_DIR}/state/stall-watchdog.log` so a watchdog bug
cannot cascade into a misclassified build alert. The script is *not*
subject to the exit-path severity classification above (that table is
scoped to `rebuild.sh.j2`); its single terminal state is always
`Telegram-warn` — a stall alert — with rate-limiting keyed on the set of
stale services plus `git_deploy_stall_watchdog_repeat_sec` (default
1800 s).

## `_record_failure()` semantics

Defined in `rebuild.sh.j2:182-276`. Every failure path calls this, and it is
the sole owner of:

1. State file writes (`${STACK_DIR}/state/<svc>.json`)
2. Trigger file cleanup (`${STACK_DIR}/triggers/<svc>.trigger`)
3. Three-way Telegram dispatch:
   - **At CB-trip threshold** (`new_count == CB_MAX_FAILURES`), gated by
     `alerts.opened_sent`: fires 🛑 "Circuit breaker OPEN" with the reset
     command exactly once per open event, then sets `opened_sent=true`
     so re-trips during the same open period are suppressed (a
     `[rebuild] Circuit breaker re-trip ... suppressing` marker is emitted
     instead). The bit is cleared back to `false` by `_reset_cb` /
     `_clean_state_json`, so the next distinct open event will re-alert.
   - **Duplicate SHA below threshold**: suppresses the Telegram and emits
     `[rebuild] Failure #N — notification suppressed (same commit SHA)`.
     Prevents spam when force-pushes hit the same broken commit.
   - **First failure for a new SHA below threshold**: fires 🚨
     "<context> failed (N/MAX)" with optional tail output.

Phase 2 replaced the `{"failures":N}` state with a versioned
schema v1 that adds `opened_at`, structured `last_failure` (sha/stage/
reason/at), and `alerts.opened_sent` + `alerts.last_blocked_alert_at`.
Legacy-shape files are migrated on first read. Phase 3 wired in the
fourth dispatch arm — the rate-limited "Push blocked — circuit breaker
OPEN" alert at the top-of-script CB guard (row 6 above), keyed on
`alerts.last_blocked_alert_at` (epoch seconds, default interval 3600 s)
— and made the threshold-trip alert in `_record_failure` once-only via
`alerts.opened_sent`. Both fields are now load-bearing; Phase 4 (CLI)
will read them for `bay build status`.

## Required contract enforcement (Phase 1 tasks)

- [x] CI test: `tests/test_observability_contract.py` parses the template
      and this contract together. Any new/moved exit path without a
      corresponding row in the exit-path map fails the build, and every
      exit must be preceded by a categorized stdout marker, `notify_build`
      call, or `_record_failure` call.
- [x] On every alert delivery failure, append to
      `${STACK_DIR}/state/telegram-failures.log` so delivery outages are
      visible. Implemented in both `rebuild.sh.j2` (curl HTTP-code branch)
      and the webhook `app.py` (`_record_telegram_failure` helper). The
      webhook container mounts `${STACK_DIR}/state` at `/state` and reads
      `TELEGRAM_FAILURES_LOG` from the compose env.
- [x] Path #5 (unknown service): added a `send_telegram` call before the
      `exit 1` — `Telegram-warn` severity with host, service name, and
      investigation commands (landed in `rebuild.sh.j2:356`).

## Deferred to later phases

- **Surface failed Telegram sends in `bay status` output**: deferred
  to Phase 4 (`bay build status` subcommand), which reads remote
  state files per service. The current `bay status` is a local-only
  command that doesn't SSH to hosts, so extending it for per-host log
  reads would blur its scope. `bay build status` is the natural home.
- **Telegram heartbeat** ("I'm alive" periodic message): deferred.
  Low priority and tangential to the incident pattern the contract is
  addressing. Will revisit if an operator needs it.

## Correlating a push end-to-end

Every GitHub push handled by the webhook receiver gets a UUID4 correlation ID
(`corr_id`). The ID travels from the webhook container through the trigger file
into rebuild.sh and into every Telegram message sent during that build. This
makes post-incident log correlation trivial.

### Trigger file format (v2)

```
<UUID4-corr_id>\n
<signal>\n
```

Line 1 is the correlation ID. Line 2 is the signal: `pull` for image-pull
triggers, or empty for normal build triggers. Rebuild.sh reads line 1 as
`CORR_ID` and line 2 to detect pull signals. Legacy single-line files
(format v1) are handled gracefully: `CORR_ID` is set to `unknown`.

Manual invocations (no trigger file on disk when rebuild.sh starts) receive
a `manual-<epoch>` ID so they are distinguishable in logs but not confused
with webhook-originated builds.

### Grep commands

```bash
# Find all journal entries for a specific push on a single host:
journalctl -u 'bay-build@*' | grep <corr_id>

# Find the webhook log entry that originated the push:
docker logs bay-webhook | grep <corr_id>

# Cross-region: same grep on both EU and NA hosts
ssh debugbot@203.0.113.11  "docker logs bay-webhook 2>&1 | grep <corr_id>"
ssh debugbot@203.0.113.12 "docker logs bay-webhook 2>&1 | grep <corr_id>"

# Find the Telegram message text (corr_id appears in the footer):
journalctl -u 'bay-build@*' | grep -A5 <corr_id>
```

### Known limitation: cross-region correlation

The correlation ID is generated **per-region**. When the webhook receiver on
region EU fans out a push to NA (for a service whose `regions` includes both),
NA's webhook receiver generates a **new** UUID for its own trigger file. The
two IDs are different for the same upstream GitHub push.

Cross-region correlation currently requires matching on the GitHub delivery ID
(`X-GitHub-Delivery` header), which is logged by the webhook in the fanout
path. The `corr_id` is useful for tracing a build within a single region only.

Fixing this would require forwarding the originator's `corr_id` in the fanout
request body — planned as a follow-up when the cross-region fanout path is
refactored.

## Rollback target lost (state-integrity failure)

### Detection

After a double-failure scenario (new image fails health check AND rollback image also fails health check), check whether `:latest` and `:previous` point at the same digest:

```bash
docker inspect --format '{{.Id}}' <registry>/<stack>/<svc>:latest
docker inspect --format '{{.Id}}' <registry>/<stack>/<svc>:previous
```

If both commands return the **same digest**, the rollback target has been corrupted. Before v0.81.0 (`_handle_rollback` reversible-retag fix), this could happen any time a double-failure occurred.

**Reference incident:** `blog` on demo NA, 2026-04-16 — `sha256:461469b50b867dc26e994655a5e58760dea87bc588257d2888e5f61b72aeea3a` was both `:latest` and `:previous`. Root cause: onnxruntime-node ABI bug caused the new image to fail health checks, and the previous image was also broken (pre-incident state). The framework correctly detected both failures but left both tags at the same digest (a later fix addressed this).

### Why this happened (pre-v0.81.0)

`_handle_rollback()` retagged `:previous` → `:latest` **before** the rollback health check passed. When the rollback health check also failed, the function exited without restoring `:latest`, leaving both tags at the same (broken) digest.

### Fix in v0.81.0

`_handle_rollback()` now captures `:latest`'s digest before the retag. If the rollback container fails to start OR its health check fails, `:latest` is restored to the pre-rollback digest. `:previous` is left intact.

Edge case: on a **first deploy** (`:latest` not yet present), `docker inspect` returns empty. The guard `[[ -n "${latest_digest}" ]]` skips the restore in that case.

### Recovery (if you encounter this on pre-v0.81.0 deployments)

1. Find a known-good SHA tag in the registry:
   ```bash
   # Via Zot registry UI, or:
   docker image ls --digests <registry>/<stack>/<svc>
   ```

2. Pull the known-good image:
   ```bash
   docker pull <registry>/<stack>/<svc>:<known-good-sha-tag>
   ```

3. Retag it as `:previous`:
   ```bash
   docker tag <sha256:hash> <registry>/<stack>/<svc>:previous
   ```

4. Verify divergence:
   ```bash
   docker inspect --format '{{.Id}}' <registry>/<stack>/<svc>:latest
   docker inspect --format '{{.Id}}' <registry>/<stack>/<svc>:previous
   # Must return different hashes
   ```

## External invariant: image-map.json producer inclusion

The webhook receiver's `IMAGE_MAP` table determines which local services
receive pull-signal `.trigger` writes on an inbound `/webhook/pull-image`
call. The table is loaded from `image-map.json` at receiver startup
(`app.py:_load_image_map`); a missing producer entry there is
operationally indistinguishable from a silent fan-out failure — pushes
look healthy in the webhook log, but the producer's container stays on
the old image.

**Assertion:** For every service with `build: {strategy: remote}` and
`image:` defined, the `image-map.json` rendered against that host's
`services.yml` MUST include that service under its own image ref —
**not just its pull-only siblings**. The producer is a consumer of its
own image and must appear in the fan-out list.

**Test guards:**
- `tests/test_image_consumers.py` —
  `test_shared_image_groups_all_consumers` pins the producer-inclusive
  shape of the `bay_image_consumers` filter plugin output.
- An `image-map.json` template render test is planned to
  pin the end-to-end render path (active-services scoping +
  `git_deploy_rebuild_services` composition + filter plugin output).

**Why it matters:** This is not a `rebuild.sh.j2` exit-path concern (the
exit-path map above is scoped to `rebuild.sh` terminal states), but it
shares the same "silent failure" failure mode the rest of this contract
exists to prevent. A stale or producer-less `image-map.json` causes the
webhook to write triggers for siblings but not for the producer; the
producer's path-unit never fires; no Telegram is emitted; the operator
sees a healthy `triggered N services` line in the webhook log and a
stale producer container. This gap surfaced as a cross-region regression
where a consumer's image map went stale after a producer rebuild. See
`docs/build-pipeline.md` "image-map.json Lifecycle" for the
operational lifecycle (auto-render on every deploy, auto-restart on
content change).

## How to amend this contract

When adding a new exit path to `rebuild.sh`:

1. Add the row to the exit-path map above (line, exit code, terminal
   state, current emission, required severity).
2. If `Telegram-info`/`warn`/`critical`: implement the `notify_build`
   call following the existing message style (host, service, timestamp,
   recovery command if applicable).
3. If `log-only-debug`: add the stdout marker to the categorized list in
   "Severity levels" and justify why the state is benign.
4. Update the CI test's allowlist so it covers the new path.
5. Bump the commit reference at the top of "Exit-path map" when changing
   line numbers.
