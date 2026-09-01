---
name: bay
description: Operate a Bay infrastructure repo — deploy and provision servers, manage services and secrets, run the Headscale tailnet gateway, and find the right deep doc. Use when the repo has a .bay/ framework clone or a bin/bay wrapper (or a services.yml beside group_vars/), when developing the Bay framework itself, or when the user mentions Bay, bin/bay, or a bay deploy.
---

# Bay

Bay is an Ansible + Python framework for running Docker services on hardened
servers: Traefik reverse proxy, CrowdSec/nftables at the firewall, a self-hosted
Headscale tailnet, declarative services, restic backups, and a build pipeline.

**Framework + consumer.** Bay is a standalone repo that a *consumer* clones into
`.bay/` inside its own repo. The consumer owns `services.yml`, `group_vars/`,
`hosts/` and `bin/bay`; the framework owns roles, playbooks and the CLI. In
production only the consumer repo exists — it pulls the framework at the version
pinned in `.bay-version`.

```
consumer/
├── .bay/          # framework clone (gitignored, pinned by .bay-version)
├── bin/bay        # wrapper → uv run --project .bay bay
├── services.yml   # the app surface — single source of truth
├── group_vars/    # config + vault-encrypted secrets
└── hosts/         # inventory
```

This file is generated in part. `bin/bay --skill` prints it; `make docs-skill`
in the framework repo rebuilds the generated sections.

## Rules — operating a consumer

- **Always go through `bin/bay`.** Never call `ansible-playbook` directly — the
  CLI runs pre-deploy validation, version checks and other guards.
- **`bin/bay validate` before any deploy that touches config.** An invalid
  Headscale ACL crash-loops the control server; an invalid `services.yml`
  reaches the host.
- **`git pull` the consumer before deploying.** A stale local clone silently
  reverts remote-only config to framework defaults.
- **A deploy ships app code, not just config.** `git_deploy` pulls each
  service's repo, so `bin/bay deploy` can change what is running even when no
  framework or config file changed. Weigh blast radius accordingly.
- **Not every role runs on deploy.** Some (e.g. `outbound_monitor`) live in
  `provision.yml`, so a deploy will never apply them no matter which tags you
  pass — a role can sit broken for months this way. Check the CHANGELOG's
  *Upgrade notes* for `bin/bay provision --tags <role>` instructions.
- **Enrolling a tailnet node does not grant it access.** Under a default-deny
  ACL an unlisted node is dead on arrival; rules are **directional** (a node
  that is reachable still cannot initiate); failures are silent, because
  tailscale ACLs are accept-only and an ungranted peer is simply absent from
  `tailscale status`. sshd `AllowUsers`/`ListenAddress` is a second, independent
  gate. **Read `docs/tailnet-naming.md` before `gateway enroll` or any ACL
  edit**, and verify with `bin/bay gateway acl audit` — which only checks the
  inbound side.
- **Secrets live in `group_vars/<env>/secrets.yml`** under `ansible-vault`.
  Key casing is load-bearing: UPPERCASE = container env var, lowercase =
  Ansible role variable.
- **Rig infrastructure is not in `services.yml`.** Traefik, CrowdSec,
  Watchtower, Headscale, Zot and the webhook receiver are framework-managed
  roles; `services.yml` is the consumer's app surface only.

## Rules — developing the framework

- **Framework changes are invisible to consumers until tagged.** Commit in
  `bay/`, add a `CHANGELOG.md` entry, then `make release VERSION=X.Y.Z` (never
  `git tag`/`git push` by hand — `version.yml` would drift from the tags). Then
  `bin/bay update` in the consumer.
- **For local iteration use `bin/bay dev-link`** to symlink `.bay/` at a sibling
  framework checkout, and `bin/bay dev-unlink` to restore the pinned version.
- **Alerts fan out from `roles/alert_channel`.** Call `bay_notify <literal.id>`
  with an ID registered in `alerts/registry.yml` — never add a private curl to
  the notification API of the day.

## Common tasks

| Goal | Command |
|---|---|
| First-time setup | `git clone https://github.com/AltanS/bay.git .bay`, then `.bay/bootstrap.sh`, then `bin/bay setup` |
| Deploy services | `bin/bay deploy production` |
| Deploy including infra roles | `bin/bay deploy --rig production` |
| Recreate containers | `bin/bay deploy production --tags deploy_stack` |
| Dry run | `bin/bay deploy production -- --check --diff` |
| Provision a fresh server | `bin/bay provision production` |
| Edit secrets | `bin/bay vault edit production` |
| Check config before deploying | `bin/bay validate` |
| Add a machine to the tailnet | `bin/bay gateway enroll <name>`, then the ACL |

## CLI reference

One line per command — the inventory, not the manual. Run any command with
`--help` for its flags; that output is always current, so nothing below tries
to reproduce it. The flags that change what a command *means*:

- `deploy --rig` — also run the infrastructure roles (nftables, Traefik,
  monitoring, backups). Without it, only services are deployed.
- `deploy --tags <tag>` — restrict to one role. `deploy_stack` recreates
  containers; `traefik` rewrites routing config without touching containers.
- `-- <args>` — everything after `--` goes straight to `ansible-playbook`
  (`-- --check --diff` for a dry run).
- `deploy --skip-validate` — bypasses the pre-deploy guards. Emergency use only.
- `logs --scrub` — destructive: GDPR erasure that rewrites live and archived
  logs on the host. Dry-run preview unless you add `--yes`.

<!-- BEGIN GENERATED CLI REFERENCE -->

### Framework

- `bin/bay dev-link [path]` — Link .bay/ to a local framework checkout for development.
- `bin/bay dev-unlink` — Remove the dev link and restore the pinned framework clone.
- `bin/bay guide` — Show tailored next steps for this project's current state.
- `bin/bay install` — Install the framework version pinned in .bay-version.
- `bin/bay setup` — Run the interactive setup wizard to configure your project.
- `bin/bay status` — Show the pinned framework version, update status, and feature flags.
- `bin/bay update` — Update to the latest framework release (bumps .bay-version).

### Operations

- `bin/bay admin-shell <host>` — Open an SSH session as the configured admin user on the named host.
- `bin/bay alerts` — Inspect and configure Bay's alert surface.
- `bin/bay alerts disable <pattern>` — Mute one or more alerts.
- `bin/bay alerts doctor` — Diagnose the failure modes that have actually bitten.
- `bin/bay alerts enable <pattern>` — Un-mute one or more alerts.
- `bin/bay alerts list` — Show every alert with its effective per-recipient state.
- `bin/bay alerts test [alert_id]` — Show — or with --live, prove — where an alert would be delivered.
- `bin/bay build` — Inspect and reset the webhook build circuit breaker.
- `bin/bay build reset [service]` — Reset the build circuit breaker after fixing the underlying issue.
- `bin/bay build status` — Show circuit breaker state for all services on the target host(s).
- `bin/bay deploy <env>` — Deploy services to the target environment.
- `bin/bay gateway` — Manage the access gateway (headscale tailnet / wireguard).
- `bin/bay gateway acl` — Inspect the tailnet ACL policy.
- `bin/bay gateway acl audit` — Flag tailnet nodes that no accept rule can reach.
- `bin/bay gateway add-user <name>` — Create a new headscale user (headscale only).
- `bin/bay gateway apikey` — Generate a Headscale API key (headscale only).
- `bin/bay gateway delete-node <name>` — Delete a node from the tailnet.
- `bin/bay gateway delete-user <name>` — Delete a headscale user.
- `bin/bay gateway enroll` — Enroll a device: create user, generate key, print the join command.
- `bin/bay gateway key <name>` — Generate a pre-auth key for a user (headscale only).
- `bin/bay gateway nodes` — List tailnet nodes with user, IP, and last-seen time (headscale only).
- `bin/bay gateway rename-node <old_name> <new_name>` — Rename a node in the tailnet.
- `bin/bay gateway rename-user <old_name> <new_name>` — Rename a headscale user.
- `bin/bay gateway route-approve <node_name> <route>` — Approve or revoke an advertised route for a node.
- `bin/bay gateway routes` — List all advertised routes across the tailnet.
- `bin/bay gateway status` — Show access gateway status.
- `bin/bay gateway user-info <name>` — Show details for a user and their nodes.
- `bin/bay gateway users` — List all headscale users with node counts.
- `bin/bay healthcheck <env>` — Hit every public service's domains and report reachability.
- `bin/bay logs <service>` — Show container logs for a service, or operate on its log archive.
- `bin/bay provision <env>` — Provision and harden a server (base OS, users, firewall, Docker).
- `bin/bay prune <env>` — Reclaim disk space by pruning unused Docker images and build cache.
- `bin/bay region` — Manage deployment regions (multi-region inventories).
- `bin/bay region add` — Add a new region to an existing multi-region deployment (interactive).
- `bin/bay restart [service...]` — Restart service containers without a full deploy.
- `bin/bay restore <env>` — Run the restore playbook directly (low-level).
- `bin/bay webhook <env>` — Deploy webhook infrastructure and show GitHub setup instructions.

### Stack Manager

- `bin/bay server` — Manage inventory servers.
- `bin/bay server add <ip>` — Add a server to the inventory.
- `bin/bay server inspect [env]` — Inspect live network configuration from servers via SSH.
- `bin/bay server list [env]` — List servers from the inventory.
- `bin/bay server remove <ip>` — Remove a server from the inventory.
- `bin/bay service` — Manage services and accessories in services.yml.
- `bin/bay service add [catalog_id]` — Add a service or accessory from the catalog or a custom definition.
- `bin/bay service catalog` — List available service/accessory definitions from the catalog.
- `bin/bay service edit <name>` — Edit an existing service's configuration in services.yml.
- `bin/bay service list` — List all configured services and accessories.
- `bin/bay service prune-webhooks <repo>` — List and optionally delete orphan GitHub webhooks for a repository.
- `bin/bay service remove <name>` — Remove a service or accessory from services.yml.
- `bin/bay service show <name>` — Show the full configuration for a service or accessory.

### Vault

- `bin/bay vault` — Manage encrypted secrets (ansible-vault).
- `bin/bay vault decrypt <env>` — Decrypt a secrets file in place — leaves PLAINTEXT on disk.
- `bin/bay vault edit <env>` — Edit encrypted secrets in $EDITOR (decrypt, edit, re-encrypt).
- `bin/bay vault encrypt <env>` — Encrypt a plaintext secrets file in place.
- `bin/bay vault set <env> <key> [value]` — Set one secret key non-interactively (decrypt, modify, re-encrypt).
- `bin/bay vault view <env>` — View encrypted secrets (read-only, no temp files).

### Backup

- `bin/bay backup` — Manage restic backups (list, run, restore, status, check).
- `bin/bay backup check <accessory>` — Verify backup repository integrity (restic check).
- `bin/bay backup list <accessory>` — List backup snapshots for an accessory (newest first).
- `bin/bay backup restore <env> <accessory>` — Restore an accessory from a backup snapshot (interactive).
- `bin/bay backup run [accessory]` — Trigger a backup now (one accessory, or all).
- `bin/bay backup status` — Show the backup status dashboard (last backup, snapshots, repo size).

### Utilities

- `bin/bay doctor [env]` — Run pre-flight checks on your project before deploying.
- `bin/bay secret` — Generate random secrets or hash passwords.
- `bin/bay test` — Run the consumer's infrastructure tests (tests/test_infra.sh).
- `bin/bay validate` — Validate configuration files before deploying.

<!-- END GENERATED CLI REFERENCE -->

## Documentation map

Paths are relative to the framework root (`.bay/` in a consumer).

<!-- BEGIN GENERATED DOC MAP -->

**Start here**

- `CHANGELOG.md` — What changed in each release, with upgrade notes. Read this before `bin/bay update`.
- `docs/features.md` — What Bay is, the full feature set, and how it compares to alternatives.
- `docs/onboarding.md` — The `bin/bay setup` wizard, the files it generates, and your first deploy.

**Configuration**

- `docs/services.md` — The `services.yml` schema — services, accessories, access modes, env/secrets, ports, build, backups, update policy. The single source of truth for your app surface.

**Access & networking**

- `docs/access-gateways.md` — VPN backends for `access: vpn` services — WireGuard vs self-hosted Headscale, traffic flow, split-DNS.
- `docs/tailnet-ingress.md` — Trusted HTTPS for tailnet-only services on self-hosted Headscale — DNS-01 wildcard certs, default-deny ACL, per-device identity injection.
- `docs/tailnet-naming.md` — Naming and authorization on a Headscale tailnet — users vs node names vs `hosts:` aliases, ACL tags as classes, the hybrid pattern, onboarding, verification, rollback.
- `docs/crowdsec.md` — CrowdSec IDS/IPS — log parsing, the nftables bouncer, trusted IPs, and lockout recovery.
- `docs/forward-auth.md` — The ForwardAuth SSO gateway for putting an auth layer in front of services.

**Build & deploy pipeline**

- `docs/build-strategies.md` — Choosing a build strategy — registry pull vs local build vs remote/cloud build.
- `docs/build-pipeline.md` — Operator reference for the webhook → build → deploy flow: trigger files, circuit breaker, troubleshooting.
- `docs/build-pipeline-observability-contract.md` — The CI-enforced contract mapping every pipeline exit path to an observable terminal state.
- `docs/reconciler.md` — The server-side Python reconciler (`bay_reconcile`) — the sole container-deploy path since v0.97.0.
- `docs/rollout-playbook.md` — Multi-host deploy playbook — deploy order, port-drift recreation, and the post-deploy audit checklist.

**Operations**

- `docs/backups.md` — restic backups — S3 config, per-accessory repos, retention, restore, and monitoring.
- `docs/multi-region.md` — Deploying one stack to multiple regional servers from a single consumer repo.
- `docs/alerting.md` — Where alerts go — Telegram plus an optional generic webhook sink (Campfire/Slack/raw), and the fail-open guarantees.
- `docs/debug-agent.md` — The `debugbot` limited-permission SSH user for AI-assisted read-only debugging.
- `docs/performance.md` — How fast a deploy is and why — Mitogen, SSH pipelining, and the `--profile` flag.

**Architecture & decisions**

- `docs/design-decisions.md` — Features and approaches explicitly decided against or deferred — with reasoning, so we don't re-litigate them.
- `docs/adr/001-docker-run-over-compose.md` — ADR — why container lifecycle uses `docker run` over Docker Compose.
- `docs/adr/002-log-archival.md` — ADR — host-side per-service log archival via cursor-based `docker logs --since`.

<!-- END GENERATED DOC MAP -->
