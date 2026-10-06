# Rename map: Argo → Bay

## Status as of v2.0

The consumer-side shim is gone (see "Pinning / fleet scaffolding"). The box-side
shim (the dual-read rows below) is still live. This is
deliberate, not an oversight: those literals name **real state on live
production hosts** — unix accounts, systemd units, docker labels, pin files —
and removing them requires a coordinated host-side migration (`rename_migration`
role), not a code-only edit. Earlier drafts of this map said "removed in
v1.1" for every aliased/dual-read row; that timeline was wrong and has been
corrected below to "a future major release." No specific version is committed
yet — see the rename tracker milestone for the removal plan.

Canonical old→new identifier mapping for this rename (Argo → Bay). Every rename
batch in S02–S05 works from this table only — no blanket `sed`. `tests/test_rename_sweep.py`
enforces that no stray `argo` (case-insensitive) survives outside the allowlist there,
using the `PENDING` list to track which of the surface classes below are not yet renamed.

Migration classes:

- **hard-cut** — renamed in one commit, no back-compat needed.
- **aliased (remove in a future major release)** — old name keeps working via an explicit shim/alias for one
  transition release, removed in a future major release.
- **dual-read (remove in a future major release)** — code reads both old and new forms during the transition
  (e.g. both docker labels, both pin files), removed in a future major release.

Verified 2026-08-14 against `main` by `git grep`; each row below was confirmed to exist
before being listed.

`ORG` below is a placeholder for the GitHub organisation and `<domain>` for your
DNS zone. This map only needs the *shape* of each identifier, so it carries no real
fleet or operator names — `scripts/leak-scan.sh` fails the build if any reappear.

## Distribution / packages

| Old | New | Surface class | Migration class |
|---|---|---|---|
| argo: PyPI/dist name `argo` (`pyproject.toml` `[project].name`) | `bay` | python | hard-cut |
| argo: Package `argo_cli` (`src/argo_cli/`, all internal imports) | `bay_cli` | python | hard-cut |
| argo: Package `argo_reconcile` (`src/argo_reconcile/`) | `bay_reconcile` | python | hard-cut |
| argo: `ArgoError` (`src/argo_cli/errors.py` + ~180 call sites across `src/argo_cli/commands/*`, `tests/*`) | `BayError` | python | hard-cut |
| argo: GitHub repo URL `ORG/argo` (`pyproject.toml` Homepage/Documentation/Source/Issues, `README.md`, `docs/onboarding.md`) | `ORG/bay` | url | hard-cut (GitHub redirects old URLs, so no dual-read needed) |

## Pinning / fleet scaffolding (removed in v2)

Bay 2.0 removed the whole layer these names lived in, so there is nothing left to rename
or to keep as a fallback. The old names were: the `.argo/` clone inside a project, the
`.argo-version` pin file, the `.argo-dev` sentinel, the `bin/argo` wrapper, `argo.mk`, the
`argo:*` make targets, `ARGO_DIR`, `ARGO_REPO` and the `argo` console script. Their `bay`
twins went in the same release. Bay is now installed once per machine. See `docs/install.md`. The 2.0
section of `CHANGELOG.md` lists each removal with its replacement.

## Version machinery

| Old | New | Surface class | Migration class |
|---|---|---|---|
| argo: `version.yml` key `argo_version` | `bay_version` | ansible-var | hard-cut |
| argo: `argo_minimum_version` (fleet-set gate) | `bay_minimum_version` | ansible-var | hard-cut (the old key is no longer read; v2 removed the fallback) |

## Jinja filters (both `filter_plugins/argo_filters.py` copies: top-level and `roles/container_lifecycle/filter_plugins/`)

| Old | New | Surface class | Migration class |
|---|---|---|---|
| argo: `argo_html_escape` / `argo_html_unescape` | `bay_html_escape` / `bay_html_unescape` | jinja-filter | hard-cut |
| argo: `argo_prefix_volumes` | `bay_prefix_volumes` | jinja-filter | hard-cut |
| argo: `argo_traefik_labels` | `bay_traefik_labels` | jinja-filter | hard-cut |
| argo: `argo_traefik_global_labels` | `bay_traefik_global_labels` | jinja-filter | hard-cut |
| argo: `argo_watchtower_labels` | `bay_watchtower_labels` | jinja-filter | hard-cut |
| argo: `argo_alert_ids_for` | `bay_alert_ids_for` | jinja-filter | hard-cut |
| argo: `argo_build_dedup_map` | `bay_build_dedup_map` | jinja-filter | hard-cut |
| argo: `argo_image_consumers` | `bay_image_consumers` | jinja-filter | hard-cut |
| argo: `argo_image_region_map` | `bay_image_region_map` | jinja-filter | hard-cut |
| argo: `argo_repo_slug` | `bay_repo_slug` | jinja-filter | hard-cut |
| argo: `argo_repo_groups` | `bay_repo_groups` | jinja-filter | hard-cut |
| argo: `argo_log_rotation_spec` | `bay_log_rotation_spec` | jinja-filter | hard-cut |
| argo: `argo_spec_hash` | `bay_spec_hash` | jinja-filter | hard-cut |
| argo: `argo_healthcheck` | `bay_healthcheck` | jinja-filter | hard-cut |
| argo: `argo_token_url` | `bay_token_url` | jinja-filter | hard-cut |
| argo: `argo_alert_recipient` | `bay_alert_recipient` | jinja-filter | hard-cut |
| argo: `argo_alert_body` / `argo_alert_content_type` (re-exported from `roles/alert_channel/files/argo_alert.py`) | `bay_alert_body` / `bay_alert_content_type` | jinja-filter | hard-cut |

## Shell / host artifacts

| Old | New | Surface class | Migration class |
|---|---|---|---|
| argo: Shell fn `argo_notify` (`roles/alert_channel/templates/_notify.sh.j2` and every symlinking role) | `bay_notify` | shell-fn | hard-cut |
| argo: Shell fn `argo_alert` (CLI wrapper invoked by units, e.g. `roles/git_deploy/templates/*.j2`, `roles/outbound_monitor/templates/*.j2`) | `bay_alert` | shell-fn | hard-cut |
| argo: Python fn `argo_send_webhook` (`roles/alert_channel/files/argo_alert.py`) | `bay_send_webhook` | python | hard-cut |
| argo: File `argo_alert.py` — 3 copies: `roles/alert_channel/files/argo_alert.py`, `roles/docker_monitor/templates/argo_alert.py`, `roles/git_deploy/files/webhook/argo_alert.py` | `bay_alert.py` | host-path | hard-cut |
| argo: Env var `ARGO_ALERT_POLICY_FILE` (`roles/alert_channel/templates/_notify.sh.j2`) | `BAY_ALERT_POLICY_FILE` | host-path | hard-cut |
| argo: Path `/etc/argo/` (mute-state file `/etc/argo/alert-overrides`; `roles/alert_policy/defaults/main.yml`, `roles/alert_channel/templates/_notify.sh.j2`) | `/etc/bay/` | host-path | dual-read (remove in a future major release) — migration role (S04) copies state and a fallback reads the old path with a deprecation warning until the copy lands on every host |
| argo: Systemd unit prefix `argo-*` — enumerated unit template basenames (`git ls-files roles/ \| grep argo`, see below) | `bay-*` | systemd-unit | dual-read (remove in a future major release) — S04 migration role stops+disables the old unit and enables the new one per host, no simultaneous run |
| argo: Container `argo-webhook` (`roles/deploy_stack/templates/_webhook_receiver.j2`, `roles/git_deploy/handlers/main.yml`, `roles/git_deploy/tasks/webhook.yml`, `roles/git_deploy/tasks/render_image_map.yml`, `roles/container_lifecycle/tasks/build_specs.yml`) | `bay-webhook` | docker-label | hard-cut (recreated by S04 host migration, not dual-run) |
| argo: Docker label `argo.managed` (`roles/container_lifecycle/defaults/main.yml`, `roles/git_deploy/templates/rebuild.sh.j2`, `src/argo_reconcile/bundle.py`, `src/argo_reconcile/sdk_client.py`) | `bay.managed` | docker-label | dual-read (remove in a future major release) — reconciler `observe()` checks both labels so already-running containers stay adopted across the transition |
| argo: Docker label `argo.stack` (same files as above) | `bay.stack` | docker-label | dual-read (remove in a future major release) |
| argo: Default stack-name fallback `stack_name \| default('argo')` (`roles/git_deploy/templates/rebuild.sh.j2`, 2 occurrences plus 2 more feeding `argo_prefix_volumes`) | `default('bay')` | ansible-var | hard-cut |
| argo: `argo_buildx_builder` (default value `"argo-builder"`; `roles/cronjobs/defaults/main.yml`, `roles/git_deploy/defaults/main.yml`, `roles/git_deploy/tasks/*.yml`, `roles/git_deploy/templates/rebuild.sh.j2`, `src/argo_cli/commands/prune.py` comment) | `bay_buildx_builder` (default `"bay-builder"`) | ansible-var | hard-cut |
| argo: Reconciler flags `argo_reconciler_enabled` / `argo_reconciler_plan_only` / `argo_reconciler_remove_orphans` | `bay_reconciler_enabled` / `bay_reconciler_plan_only` / `bay_reconciler_remove_orphans` | ansible-var | hard-cut |

### Systemd unit template basenames (`argo-*` → `bay-*`)

Enumerated via `git ls-files roles/ | grep argo`:

- `roles/backup/templates/argo-backup-maintenance@.service.j2`, `argo-backup-maintenance@.timer.j2`
- `roles/backup/templates/argo-backup@.service.j2`, `argo-backup@.timer.j2`
- `roles/boot_safety/templates/argo-infra-boot.service.j2`, `argo-infra-ensure.sh.j2`
- `roles/cronjobs/templates/argo-docker-builder-prune.sh.j2` (also installed as `/usr/local/bin/argo-docker-builder-prune`)
- `roles/git_deploy/templates/argo-build-alert@.service.j2`
- `roles/git_deploy/templates/argo-build@.path.j2`, `argo-build@.service.j2`
- `roles/git_deploy/templates/argo-trigger-watchdog.service.j2`, `argo-trigger-watchdog.sh.j2`, `argo-trigger-watchdog.timer.j2`
- `roles/log_archive/templates/argo-logarchive@.service.j2`, `argo-logarchive@.timer.j2`
- `roles/log_archive/templates/argo-logrotate@.service.j2`, `argo-logrotate@.timer.j2`
- `roles/outbound_monitor/templates/argo-disk-alert.service.j2`, `argo-disk-alert.sh.j2`, `argo-disk-alert.timer.j2`
- `roles/outbound_monitor/templates/argo-outbound-check.j2`, `argo-outbound-check.service.j2`, `argo-outbound-check.timer.j2`

## Legal / docs / misc

| Old | New | Surface class | Migration class |
|---|---|---|---|
| argo: `LICENSE` `Licensed Work: Argo` (and the "Argo" service-name clause) | `Licensed Work: Bay` | legal | hard-cut |
| argo: `scripts/leak-scan.sh` URL/org regex | Must match **both** slug generations (`ORG/argo` and `ORG/bay`) during the transition (old GitHub URLs still resolve via redirect and appear in historical docs/CHANGELOG) | url | dual-read (remove in a future major release, once `main` no longer contains any un-redirected old-org references) |
| argo: `alerts/registry.yml` `source:` fields naming `argo-*` script files (`git_deploy/argo-trigger-watchdog.sh.j2`, `outbound_monitor/argo-outbound-check.j2`, `outbound_monitor/argo-disk-alert.sh.j2`, and the `alert_channel/argo alerts test` synthetic source, `argo alerts test` CLI invocation text) | `git_deploy/bay-trigger-watchdog.sh.j2`, `outbound_monitor/bay-outbound-check.j2`, `outbound_monitor/bay-disk-alert.sh.j2`, `alert_channel/bay alerts test`, `bay alerts test` | host-path | hard-cut (must move in lockstep with the systemd-unit rename above, else the registry drift check in `tests/test_alert_registry.py` fails) |

## Not in scope for this rename

- Unix accounts `argo` / `argo-admin` on provisioned hosts — `app_user` stays a consumer
  variable; its value stays `argo` on existing hosts.
- DNS zones `*.argo.<domain>` and `registry.infra.argo.<domain>` (e.g. `registry.infra.argo.example.com`).
- Git history / old `v0.x` tags; `CHANGELOG.md` history sections are not rewritten.
