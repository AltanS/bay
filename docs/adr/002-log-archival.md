---
# ADR-002: Host-Side Per-Service Log Archival via Cursor-Based `docker logs --since`

- **Status:** Accepted
- **Date:** 2026-04-22
- **Relates to:** `log_archival` role (to be added), `services.yml` `log_retention:` key (sibling of the `log_rotation:` key and the accessory `expose:` key), `bin/bay logs --path` CLI (extends `bin/bay logs`), `bin/bay healthcheck` (the companion debug tool that points operators at the archive when a probe fails)
- **Supersedes:** rough-draft proposal that used `docker logs -f` with `Restart=always` (see "Rejected alternatives")

## Context

Docker's default log storage (`/var/lib/docker/containers/<id>-json.log`) is tied to the container object. When a container is recreated — Ansible deploy, webhook-driven rebuild, manual `docker rm`, or crash-and-restart with a new ID — the log file is deleted with the old container. An earlier change added per-service `log_rotation` tunability but does nothing about durability; rotated logs still live inside the container's storage and evaporate on recreation.

A signup-forensics investigation found that a request to a signup endpoint on a production service was not in the retained logs; the gap was about five hours, and the logs had evaporated after a container recreation. This ADR adds an operator-controlled durable archive so logs survive recreation and are accessible via standard Unix tools (`grep`, `zgrep`, `less`) without requiring a running container or knowledge of Docker's internal paths.

Target behaviour:
- Logs for a service with `log_retention:` configured land under `/opt/<stack>/logs/services/<svc>/` with day-granularity archives and a time-based retention window.
- Container recreation leaves a boundary sentinel in the archive, not a gap.
- `debugbot` read-only SSH does **not** automatically grant access to archived logs (PII concern).
- Prune actions are alerted and manifested before any file is deleted (evidence-destruction concern).

Before writing this decision, three mechanisms were evaluated and five counsel advisors reviewed the rough draft. The draft's original approach (`docker logs -f` with `Restart=always`) was rejected unanimously.

## Decision

**Chosen mechanism:** cursor-based `docker logs --since <ts> --timestamps` on a 1-minute systemd timer, one timer pair per opted-in service.

Per active service with `log_retention:` set:

```
bay-logarchive@<svc>.timer     # OnCalendar=*:*:0/1  (every 60s)
bay-logarchive@<svc>.service   # ExecStart=/opt/<stack>/bin/archive-logs.sh <svc>
```

Script flow per tick:
1. Read `/opt/<stack>/logs/services/<svc>/.cursor` (RFC 3339 timestamp of the last line appended; empty on first run).
2. `docker logs --since <cursor> --timestamps <container-name>`, piped through a line classifier that routes malformed/non-UTF-8 input to `.malformed/YYYY-MM-DD.log` and complete lines to `live.log`.
3. On success, atomically write the new cursor: `printf '%s\n' "$LAST_TS" > .cursor.tmp && mv -f .cursor.tmp .cursor`.
4. On `container not found`: sleep 5s, retry once, skip this tick if still absent. A transient recreation window is expected.

A separate daily timer at **00:00 UTC** (not 04:15) rotates `live.log` into `YYYY-MM-DD.log.gz` named after the UTC date it covers, writes `.sha256` sidecars, and enforces retention (`days:` and `max_total_size:`) with an alert-before-prune step.

Permissions:
- Dirs: `0750 bay:argo-logreaders`  <!-- legacy-argo: unix group on hosts, kept as-is during the pre-1.0 rename -->
- Files: `0640 bay:argo-logreaders`  <!-- legacy-argo: unix group on hosts, kept as-is during the pre-1.0 rename -->
- `mode: sensitive` → `0600 root:root` on both, `argo-logreaders` cannot read  <!-- legacy-argo: unix group on hosts, kept as-is during the pre-1.0 rename -->
- Archiver runs as `bay` with `UMask=0027`
- `argo-logreaders` is a new group; `debugbot` is **not** added by default — operators must opt in explicitly per host  <!-- legacy-argo: unix group on hosts, kept as-is during the pre-1.0 rename -->

Container recreation sentinel: when a tick detects a new container ID for a known service name, the archiver appends a single line to `live.log` before the first line of the new container's output:

```
--- bay: container <name> recreated at <ts>, old-id <short>, new-id <short> ---
```

This gives operators a grep-able boundary marker without a gap in the timeline.

## Reasons

### Plain-text output lands where operators expect

Option B writes directly into `/opt/<stack>/logs/services/<svc>/live.log` with Docker-prepended RFC 3339 timestamps. `grep POST /opt/demo/logs/services/blog/live.log` works without `jq`. Options A (logrotate on Docker's JSON log) and C (steady-state `-f` tailer) both either require JSON-unwrapping or add symlink indirection.

### Container recreation is handled without special-casing

The cursor is a timestamp, not a container ID. `docker logs --since` on the new container picks up cleanly; the archiver notices the new container ID and emits a recreation sentinel. Option C tries to solve this with a pre-recreation Ansible hook plus a steady-state `-f` tailer, but the hook only covers Ansible-driven recreations — crash-restarts bypass it.

### Idempotent and bounded in blast radius

If a tick fails mid-write, the cursor is not updated, and the next tick re-fetches the same window. Duplicate suppression falls out of the monotonic cursor: `--since` is inclusive to the second, so at most one second of overlap is possible, and a simple "skip lines already at or before cursor" check on append eliminates it. No leader election, no coordination — each service's archiver is independent.

### No `docker logs -f` in the hot path

`docker logs -f` with `Restart=always` — the rough-draft proposal — produces **both** gaps and duplicates at container recreation. When the container object is replaced, the follower on the old ID raises an error; systemd restarts the unit and a new follower on the new ID starts from the beginning of its buffer. The overlap window between follow-start and the actual recreation contains lines that are seen twice; any line written after the follower errored but before the replacement unit started is lost. Counsel unanimously rejected this pattern. Option B avoids it by polling with an explicit cursor instead of following a stream.

### Evidence-preservation ordering

`max_total_size:` pruning mid-incident would look like evidence destruction to an auditor. The rotation timer alerts via Telegram and appends a sha256 manifest line to `.prune-log` **before** deleting any file. An operator can copy archives out of the host between the alert and the prune if the content is subject to investigation.

### Budget validated at deploy time, not runtime

The sum of `max_total_size:` across all services on a host is checked at `bin/bay validate` time against a fraction of total disk (`log_retention_budget_fraction`, default `0.30`). A misconfiguration that would sum to more than the disk fails the validate step instead of silently filling `/` at 3am.

## Consequences

- **New moving part:** `archive-logs.sh` and two systemd unit templates per service. Runs as `bay` with a tight `UMask`, `Restart=on-failure`, and a 30-second `TimeoutStartSec` so a stuck tick doesn't block the next one.

- **~60-second freshness window at reboot:** if the host reboots between timer ticks, the last ~60 seconds of log lines may not be archived. This is acceptable — the primary failure mode being addressed is container recreation, not host power-loss.

- **Archived lines are not byte-identical to stdout:** Docker's `--timestamps` prepends RFC 3339 timestamps. Great for forensic timeline reconstruction, but operators cannot use archives to prove "the app logged exactly X bytes" — only "the app logged X at time T". Documented in the schema reference (S7).

- **`log_retention` + `log_rotation` double-stores log bytes:** Docker's JSON log file keeps growing under its own rotation policy while the archiver also keeps a copy. Once `log_retention` is enabled for a service, recommend tightening `log_rotation` to `max_size: 10m, max_file: 2` to keep Docker's copy small. Documented in S7.

- **Stdout data becomes GDPR Record-of-Processing-Activities material:** enabling `log_retention:` for a service that handles personal data (access logs, emails, IDs in request paths) creates a processing activity. `bin/bay logs --scrub <svc> --pattern <regex> --yes` (S6) provides erasure; operators must add the archive to their RoPA. Warning included in the schema reference.

- **`debugbot` is no longer a read-only SSH:** it becomes a 7-day PII dump once `log_retention` is enabled on a PII-bearing service **if** `debugbot` is added to `argo-logreaders`. Default is off. Role documentation and the CrowdSec inventory allowlist guidance must call this out (S7).  <!-- legacy-argo: unix group on hosts, kept as-is during the pre-1.0 rename -->

- **Infra containers (`traefik`, `watchtower`, `bay-webhook`) are out of scope for v1.** They are not in `services.yml` and don't accept a `log_retention:` key. If durable Traefik logs become necessary, a follow-up milestone extends the mechanism to infra containers explicitly.

- **One script to maintain, written once:** `archive-logs.sh` is small (~150 lines est.) and lives in `roles/log_archival/files/` so it can be unit-tested against captured `docker logs` fixtures.

## Alternatives considered

### Rejected: logrotate + copytruncate on Docker's JSON log file (Option A)

Mechanism: a `logrotate` config with `copytruncate` rotates `/var/lib/docker/containers/<id>-json.log` in place. Symlinks from `/opt/<stack>/logs/services/<svc>/` point into the Docker log path. A `deploy_stack` task refreshes symlinks on container recreation.

Why rejected:
- **Symlink refresh is a silent-failure trap.** If the refresh task misses (tag skip, handler never fires, permission error), the symlink points at a deleted container's log file and the archive goes dark with no log line indicating so. Counsel flagged this as the primary risk.
- **Rotated archives are JSON-wrapped.** Docker's daemon writes `{"log":"...","stream":"stdout","time":"..."}\n` — grep requires `jq` or a wrapper, defeating the "logs are accessible via standard Unix tools" success criterion.
- **`maxsize` is less precise than cursor-based size accounting.** logrotate rotates when the file exceeds `maxsize` at the next check interval; real pruning still has to be done separately.
- **Date-named filenames require a post-rotate hook** that renames using yesterday's UTC date, which is a second moving part with the same silent-failure profile.

### Rejected: pre-recreation snapshot hook + steady-state `docker logs -f` tailer (Option C)

Mechanism: a `deploy_stack` pre-recreation hook captures `docker logs <old-id> --timestamps` to a date-stamped archive before the old container is removed. A systemd service runs `docker logs -f --timestamps <name>` with `Restart=on-failure` to write `live.log` during normal operation.

Why rejected:
- **The `docker logs -f` anti-pattern is still in the hot path.** On crash-restart (unplanned, no Ansible hook fires), the tailer errors on the old container ID, restarts, and starts the new container's stream from the beginning of its buffer. Counsel trap #1 applies: gaps **and** duplicates at every unplanned restart.
- **The snapshot hook only covers Ansible-driven recreations.** Manual `docker rm`, watchtower-driven updates, and OOM-kill-then-restart all bypass the hook and go through the tailer's broken path.
- **Doubles the moving parts for a partial fix.** Both the Ansible hook and the steady-state tailer must work correctly; any bug in either introduces a log durability hole. Counsel explicitly described this as "doubles the complexity with a partial fix".

### Rejected: `docker logs -f` with `Restart=always` (original rough draft)

Mechanism: a per-service systemd unit runs `docker logs -f --timestamps <name>` with `Restart=always`, redirecting stdout to `live.log`.

Why rejected unanimously by all five counsel advisors: produces **both gaps and duplicates at container recreation.** The follower errors on the old container ID; systemd restarts the unit; the new follower on the new ID starts from the beginning of its available buffer. The overlap window (time between follow-start and the recreation event) appears twice in the archive; any line emitted after the error but before the restart is lost entirely. No cursor means there is no way to detect or compensate for either. This is **the** anti-pattern this design must avoid.

### Rejected: external log shipping to a central aggregator (Loki, Vector, OpenObserve)

Out of scope for v1. Bay is self-hosted single-operator-per-consumer infrastructure; adding a network-attached log aggregator introduces availability dependencies (what if Loki is down during an incident on its own host?) and access-control surface. Host-side archives with `grep` remain the fastest path to "what did this service log five days ago" for the target operator profile. Can be revisited as a separate milestone if the operator profile changes.

## Follow-ups

- **S2** — schema + JSON schema + `validate.yml` driver/budget assertions
- **S3** — `archive-logs.sh`, systemd unit templates, `log_archival` role skeleton
- **S4** — daily rotation timer, prune-with-manifest, alert-before-prune wiring
- **S5** — `bin/bay logs <svc> --path`
- **S6** — `bin/bay logs --scrub <svc> --pattern <regex> --yes`
- **S7** — docs: schema reference, GDPR note, `debugbot` PII warning, `log_retention` + `log_rotation` duplication guidance
- **S8** — tests (unit + sandbox integration covering write-recreate-write-assert)
- **S9** — framework release v0.84.0
