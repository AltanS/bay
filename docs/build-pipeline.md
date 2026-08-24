# Build Pipeline Reference

Operator-facing reference for the webhook → build → deploy pipeline. For the
formal exit-path/severity contract enforced by CI, see
[build-pipeline-observability-contract.md](build-pipeline-observability-contract.md).

For the high-level "which strategy do I want" question, see
[build-strategies.md](build-strategies.md).

## Trigger File Format

Trigger files live at `/opt/<stack>/triggers/<service>.trigger`. Their format
defines the correlation contract between webhook, fan-out, and rebuild.sh
(v0.82.6+):

- **Webhook-written triggers (primary path):**
  ```
  <corr_id>      # line 1 — first 8 hex chars of the UUID for this push
  <pull_signal>  # line 2 — "pull" or equivalent signal from the webhook
  ```
  Written atomically: `printf '%s\n' "$CORR_ID" "$SIGNAL" > tmpfile && mv -f tmpfile trigger`.

- **Alias fan-out triggers (dedup path):** line 1 only (CORR_ID, no pull
  signal). Alias rebuilds retag from the primary's local image rather than
  pulling from registry, so no pull signal is needed. Also written atomically
  via temp+mv.

- **`[unknown]` in logs** — zero-byte trigger file (bare `touch`). Fixed in
  v0.82.6: all trigger writers now use `printf`+atomic-mv. If you see
  `[unknown]` after v0.82.6, the trigger was written by a manual `touch`
  (pre-fix operator habit) or by an external script that hasn't been
  updated.

- **`manual-<epoch>` in logs** — trigger was recreated by rebuild.sh after
  being consumed (clean-failure reboot case). Sequence: (1) rebuild.sh
  consumed the trigger, (2) build failed cleanly and rebuild.sh exited,
  (3) host rebooted before a new push arrived. On reboot, the path unit has
  no trigger to re-fire; a later operator or health-check creates a bare
  trigger. This is a **known limitation** — the original CORR_ID is
  irrecoverably gone once the trigger is deleted. Documented for journalctl
  triage. Compare to reboot-after-hang, where the trigger survives and the
  original CORR_ID is preserved.

- **Ownership rule:** trigger files are a one-way signal — webhook writes,
  rebuild.sh reads+deletes, nobody else rewrites. rebuild.sh must never
  write content back to the trigger file; any build state belongs in
  `/opt/<stack>/state/<service>.json`.

## Circuit Breaker State (rebuild.sh)

`rebuild.sh` maintains a per-service state file at
`/opt/<stack>/state/<svc>.json`. Schema v1 (v0.76.0+):

```json
{
  "version": 1,
  "consecutive_failures": 0,
  "opened_at": null,
  "last_failure": {
    "sha": "abc123",
    "stage": "Health check",
    "reason": "Container exited with code 1",
    "at": "2026-04-16T15:26:00Z"
  },
  "alerts": {
    "opened_sent": false,
    "last_blocked_alert_at": null
  }
}
```

- **CB trips at `git_deploy_cb_max_failures` consecutive failures (default: 5)**
  — when the breaker opens, rebuild.sh fires a one-shot Telegram alert and
  exits 0 on all subsequent pushes. `journalctl` will show
  `[rebuild] Circuit breaker OPEN for <svc>` but no Telegram per subsequent
  push (rate-limited to 1/hour via `last_blocked_alert_at`).
- **Rollback alert includes CB preview** — even when CB is not yet open,
  rebuild.sh's rollback success message includes current failure count and
  a reminder to reset if needed.
- **CB-open is silent from the webhook side** — `docker logs bay-webhook`
  will continue to show `triggered N services` on every push (webhook
  correctly wrote the trigger); the CB guard in rebuild.sh exits early.
  This is the incident pattern: webhook looks healthy, service is stuck.
- **Recovery:** `bin/bay build reset <svc>` from the consumer directory
  (see `--help` for flags). See "Webhook Auto-Build Troubleshooting" below
  for the manual JSON fallback.
- **Incident (2026-04-16)** — `blog` on the `demo` NA region. Build failed,
  rollback succeeded, but CB counter reached 3 (old threshold). 2+ hours
  of pushes silently hit the CB guard with no Telegram. Triggered the
  CB-visibility work (CLI surface). Threshold bumped 3→5 in v0.76.1.

## Webhook Auto-Build Troubleshooting

**Build server webhook not receiving pushes:**
- Check `docker logs bay-webhook` on the build server (203.0.113.14)
- Verify GitHub webhook URL points to `https://deploy.example.com/webhook`
- Test HMAC: `curl -X POST https://deploy.example.com/health` should return service list
- If CrowdSec blocked the GH IP: `ssh debugbot@203.0.113.14 "sudo cscli decisions list"`

**Build failures (no container restart):**
```bash
# Check build service logs on build server:
ssh debugbot@203.0.113.14 "journalctl -u bay-build@<svc>.service --since '1h ago' --no-pager"
# Check state file for CB status:
ssh debugbot@203.0.113.14 "cat /opt/demo/state/<svc>.json"
# If CB is open (consecutive_failures >= git_deploy_cb_max_failures=5):
ssh debugbot@203.0.113.11 "bin/bay build reset <svc>"  # from demo consumer
```

**Pull signal not reaching deployment servers:**
- Check build server `docker logs bay-webhook` for `[pull-signal]` lines
- Check deployment server `docker logs bay-webhook` for the incoming pull signal
- Verify deployment server is in `svc.regions` in services.yml

**Container not restarting after pull signal:**
```bash
# Check deployment server's rebuild log:
ssh debugbot@203.0.113.12 "journalctl -u bay-build@<svc>.service --since '30m ago' --no-pager"
# Check trigger file was written:
ssh debugbot@203.0.113.12 "ls -la /opt/demo/triggers/"
# If trigger exists but service didn't run, check path unit:
ssh debugbot@203.0.113.12 "systemctl status bay-build@<svc>.path"
```

**Circuit breaker (CB) recovery workflow:**
```bash
# Check CB state (or: ssh debugbot@<host> "cat /opt/<stack>/state/<svc>.json"):
bin/bay build status
# Reset CB (writes clean state + sends Telegram audit; see --help for flags):
bin/bay build reset <svc>
# Manual reset (if CLI unavailable):
ssh argo-admin@<host> "sudo -u bay printf '{\"version\":1,\"consecutive_failures\":0,\"opened_at\":null,\"last_failure\":null,\"alerts\":{\"opened_sent\":false,\"last_blocked_alert_at\":null}}\n' > /opt/<stack>/state/<svc>.json"  # legacy-argo: live host account value
# After reset, push again or touch trigger to re-fire:
ssh debugbot@<host> "touch /opt/<stack>/triggers/<svc>.trigger"
```

**Health check failure causing rollback loop (per-service timeout override):**
```yaml
# services.yml — increase rebuild.sh's wait window for slow-starting services
# (default git_deploy_health_check_timeout: 90s)
services:
  myapp:
    health_check_timeout: 180  # seconds — use for JVM/DB-warmup heavy services
```
The Docker container `healthcheck.start_period` and `rebuild.sh`'s
`HEALTH_CHECK_TIMEOUT` are independent: Docker uses `start_period` to
suppress early failures from its restart policy; rebuild.sh uses
`HEALTH_CHECK_TIMEOUT` to decide when to roll back. Both need to be large
enough for the slowest legitimate cold start. If `rebuild.sh` rolls back
before `start_period` expires, the issue is `health_check_timeout`
(rebuild.sh side), not `start_period` (Docker side).

`health_check_timeout` has a **second consumer**: the post-deploy
`bin/bay healthcheck` URL probe uses it as the readiness window for a
still-booting upstream (connection refused / 502). There it can only *widen*
the 90s framework default — a smaller value is ignored, so tuning rebuild.sh's
rollback poll down can never make the probe stricter than baseline. See
`docs/services.md` → "Cold starts and the readiness window".

**Registry pushes 404 / large-layer timeouts after enabling split entrypoints (GitHub #27):**
- Two failure signatures: `unexpected status from POST .../v2/<repo>/blobs/uploads/: 404 Not Found`
  on `docker push` from the infra build host; or, if the registry domain is
  repointed at the public IP instead, large layers hang and fail with
  `read tcp <public-ip>:...-><public-ip>:443: read: connection timed out`.
- Root cause: with `traefik_split_entrypoints: true`, `websecure` binds the
  public IP only, but the Zot router was hardcoded to `websecure`. The
  zot-control host resolves `zot_domain` to its own tailnet IP (to avoid a
  public-IP hairpin that times out large layer uploads), so infra-originated
  pushes hit `websecure_tailnet` instead — where no router existed — and got
  a Traefik 404.
- Fixed in the framework (GitHub #27): the zot router now binds
  `websecure,websecure_tailnet` automatically whenever `traefik_split_entrypoints`
  is on (`websecure` alone otherwise — unchanged for non-split consumers).
  Override via `zot_entrypoints` in group_vars (same idiom as
  `vpn_entrypoints`). The zot role also manages an `/etc/hosts` pin on the
  control host — `zot_tailnet_pin_ip` (defaults to
  `headscale_server_tailnet_ip`) pins `zot_domain` to the tailnet IP via a
  marker-commented, Ansible-managed line; set it to `''` to disable (the
  managed line is removed). Deployment nodes resolve the registry via public
  DNS and are unaffected.
- Diagnostic: on the control host, `getent hosts <zot_domain>` should return
  the tailnet IP; `curl --resolve <zot_domain>:443:<tailnet-ip> https://<zot_domain>/v2/`
  should return `200`/`401`, not `404`.

## Remote Build Strategy Gotchas (v0.72.0+)

- **`strategy: remote` builds on the build server, not the controller** —
  The `build_server` variable (required, no default) identifies which
  inventory host runs `docker build`. Tasks use
  `delegate_to: "{{ build_server }}"` with `run_once: true` inside the
  deploy play. The build server needs Docker, the Docker Python SDK, and
  registry credentials.
- **`push` is a deprecated alias for `remote`** — `resolve_strategy.yml`
  normalizes `push` → `remote` early and logs a deprecation warning. All
  downstream code checks `remote` only.
- **Shared images across services** — Multiple services can reference the
  same `image:` (e.g., per-locale services sharing
  `storefront:latest`). Only one service needs a `build:` block
  with `strategy: remote`. The `build_image` role skips images produced
  by remote builds; `git_deploy` pulls each unique image ref once after
  the build+push completes.
- **Webhook auto-builds work for `strategy: remote` (v0.76.0+)** —
  GitHub pushes to a remote-strategy repo land on the build server's
  webhook receiver (`IS_BUILD_SERVER=true`). The build server builds,
  pushes to registry, then posts a pull signal to each deployment server
  in `svc.regions`. Deployment servers receive the pull signal, skip the
  build, and pull+restart the container. Full pipeline: GitHub push →
  build server webhook → `docker buildx build` + push to Zot registry →
  `X-Bay-Pull-Signal` HTTP call to each region's webhook → deployment
  server writes `pull` trigger → `bay-build@.path` fires `rebuild.sh` →
  `docker pull` + `docker compose up -d` + health check. See "Webhook
  Auto-Build Troubleshooting" above.
- **`build_server` must be in the inventory** — The host must be
  SSH-reachable from the controller and have `app_user` in the docker
  group. For demo, this is the infra host (203.0.113.14) which
  also runs the Zot registry, but these are independent concerns.
- **Persistent clone directory** — Remote builds clone repos to
  `/opt/<stack>/push-builds/<svc>/` on the build server (not `/tmp/`).
  Build secrets go to `.secrets/` subdirectory and are cleaned up in
  `always:` blocks.

## image-map.json Lifecycle

`image-map.json` is the source of truth for which services the webhook
receiver fans pull signals to after a remote build completes. It lives at
`/opt/<stack>/webhook/image-map.json`, is mounted read-only into the
`bay-webhook` container at `/config/image-map.json`, and is loaded into
the receiver's in-memory `IMAGE_MAP` table on process start
(`app.py:_load_image_map`).

- **What's in it** — A `{ "<image_ref>": ["svc1", "svc2", ...] }` dict
  produced by the `bay_image_consumers` filter plugin
  (`filter_plugins/bay_filters.py`). For each image ref produced by
  a remote build, the value lists every service on this host that
  references that image — **including the producer service that owns the
  `build:` block**, not just its pull-only siblings. The unit test
  `tests/test_image_consumers.py` (`test_shared_image_groups_all_consumers`)
  pins this producer-inclusive contract.

- **When it's rendered** — Automatically on every `bin/bay deploy`,
  including `--tags deploy_stack`, `--tags build`, AND `--tags git_deploy`.
  The render lives in a dedicated, self-contained task file
  (`roles/git_deploy/tasks/render_image_map.yml`) that the role
  includes early enough to compute its own facts under any tag context.
  No separate `--tags git_deploy` step is required after a `services.yml`
  change touching `image:` or `build:` blocks — the next normal
  `bin/bay deploy <env>` keeps the map current.

- **When the receiver picks up changes** — The `bay-webhook` container
  loads its map once at startup, so a fresh render only takes effect
  after a container restart. The render task fires an Ansible handler
  (`Restart bay-webhook` in `roles/git_deploy/handlers/main.yml`) when
  the destination file content changes; Ansible's idempotence means the
  handler is a no-op when the rendered content matches what's already on
  disk. Operators do **not** need to run `docker restart bay-webhook`
  manually after a normal deploy — the framework handles it.

- **Local-strategy producers sharing an image with siblings** —
  Cross-host fan-out for this topology is a separate latent gap (the
  build server only sends image-level pull webhooks for remote-strategy
  producers; local-strategy producers have no cross-host signal path).
  Not in scope for this fix — see issue #13 audit notes for detail.

### Migration: upgrading past the image-map.json fix

> **Operators upgrading from versions prior to this fix:** If your current
> `image-map.json` on any deployment host is stale — for instance, the
> producer service is absent from the webhook receiver's pull-signal
> fan-out for its own image ref — run the immediate operational fix
> **once**:
>
> ```
> bin/bay deploy <env> --tags git_deploy
> docker restart bay-webhook
> ```
>
> After upgrading to this framework version, standard `bin/bay deploy`
> keeps the map current automatically and the receiver auto-restarts
> via the handler whenever the file content changes. The manual
> `docker restart bay-webhook` step is needed **only once**, to flush
> the stale in-memory map that a prior receiver loaded at startup.
>
> **Edge case — corrupt mount from before this fix:** on some hosts a stale
> bind-mount source path exists as an empty *directory* at
> `/opt/<stack>/webhook/image-map.json` (created by docker when the
> compose service started before the file was ever rendered). In that
> state the render task fails with `Destination ... not writable`
> until the directory is removed (`sudo rmdir
> /opt/<stack>/webhook/image-map.json`), and `docker restart` cannot
> recover the container after the source switches from directory to
> file — the container must be **recreated** once
> (`docker rm -f bay-webhook && docker compose -f docker-compose.yml
> -f docker-compose.infra.yml up -d bay-webhook` from the stack
> directory). Subsequent restarts via the handler work normally.

### Historical context (GH #13)

The `storefront` consolidation surfaced this gap: the producer
(`storefront`) was rebuilt remotely and pushed to the registry, but
the producer's container stayed on the old image because it was absent
from the stale `image-map.json` on the EU deployment host. The receiver
loaded the stale map at container start and never re-read the file,
so even out-of-band manual edits to `image-map.json` had no effect
until the container was restarted. Root cause was operational, not
structural — `git_deploy` was tagged `[build, git_deploy]` only, so
the `--tags deploy_stack` deploys operators commonly run for service
config changes never re-rendered the map. The fix: the render
now runs under `deploy_stack` too, and a handler restarts the receiver
on actual content change.

## GitHub Webhook / Cross-Region Fan-out

- **Each region runs its own webhook container** — `_webhook_receiver.j2`
  is deployed on every host that has at least one local buildable
  service. GitHub normally points a single webhook URL per repo, so
  pushes only reach one region directly.
- **`webhook-config.json.j2` iterates `services` (unfiltered), not
  `active_services`** — every region's webhook knows about every build
  service in the project, so a push for a non-local service can be
  forwarded to the region that owns it. Don't switch this back to
  `active_services` — it reintroduces the cross-region blind spot.
- **`LOCAL_REGION` env var is optional but required for fan-out** —
  passed to the webhook container from the host's `region` variable
  (`{{ region | default('') }}`). If unset, the webhook runs in
  single-region legacy mode: every push writes a local trigger, no
  fan-out happens. Multi-region consumers MUST set `region: <name>` in
  `group_vars/<region>/main.yml` for fan-out to work.
- **`git_deploy_peer_webhook_urls` is consumer-defined** — a dict of
  `region: https://deploy.<region>.<domain_base>` pairs in
  `group_vars/all/main.yml`. Empty default is fine for single-region
  consumers; required on multi-region consumers where any service's
  `regions` does not include every region.
- **Loop-safety** — when a region forwards a push to a peer, it sets
  `X-Bay-Webhook-Forwarded: 1`. The receiving peer writes its local
  trigger but does NOT fan out again. Without that flag, services whose
  `regions` includes both EU and NA would bounce between peers forever.
- **Forward failures always return 200 to GitHub** — the webhook logs
  the error and fires a Telegram alert, but never returns non-200 so
  GitHub doesn't disable the hook. Check `docker logs bay-webhook` +
  Telegram if cross-region builds stop happening.
- **HMAC signature survives forwarding** — both regions share
  `WEBHOOK_SECRET` via `group_vars/all/secrets.yml`, and the forward
  reuses the original `X-Hub-Signature-256` header, so the peer
  re-validates against the same secret + body.
- **Webhook ownership boundary** — `bay-webhook` is split
  between two roles by design: `git_deploy` owns the image build,
  webhook config rendering, and `/triggers`+`/state` directory creation
  (these are git-deploy concerns — triggers come from webhooks, state
  feeds rebuild.sh). `deploy_stack` owns the compose snippet
  (`_webhook_receiver.j2`) and the container lifecycle via
  `docker_container`. Before the hash-based recreation fix this split silently
  dropped compose-snippet changes (the v0.76.0 stale-mount incident). With
  hash-based recreation, compose changes now correctly trigger
  container recreation. Do NOT move container orchestration into
  `git_deploy` — keep the boundary clean.
- **Regional webhooks on deployment servers are NOT no-ops for
  remote-strategy services** — Even though deployment servers don't
  build for remote-strategy services, they need the webhook receiver
  for cross-region fan-out. When GitHub pushes to `blog`
  (regions: na) and the push lands on EU's webhook, EU computes
  `write_local=False`, `forward_targets=[('na', '...')]`, and forwards
  to NA. Without EU's webhook receiver, NA would never see pushes that
  hit the EU URL first. Do not remove regional webhook receivers just
  because a service uses `strategy: remote`.
