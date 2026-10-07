---
name: bay
description: Operate Bay infrastructure — deploy and provision servers, manage services and secrets, run the Headscale tailnet gateway, and find the right deep doc. Use when the repo has a bay.toml or bay.fleet.toml, or a fleet at ~/.config/bay/fleets, when developing Bay itself, or when the user mentions Bay or a bay deploy.
---

# Bay

Bay is an Ansible + Python framework for running Docker services on hardened
servers: Traefik reverse proxy, CrowdSec/nftables at the firewall, a self-hosted
Headscale tailnet, declarative services, restic backups, and a build pipeline.

**Framework + fleet.** Bay is a command you install once per machine, from a
checkout at `~/.local/share/bay/framework` (see `docs/install.md`). A *fleet* is
the repo that holds your boxes and the apps on them. It keeps `group_vars/`,
`hosts/` and `bay.fleet.toml`, at `~/.config/bay/fleets/<name>`. An app repo
holds a `bay.toml` that names its fleet. The framework owns roles, playbooks and
the CLI. Pick the fleet with `bay --fleet <path>`, `BAY_FLEET=<path>`,
`BAY_FLEET_NAME=<name>`, or by running inside an app repo whose `bay.toml` names
`fleet = "<name>"`.

```
~/.local/share/bay/framework/   # the framework checkout (bay self update)
~/.config/bay/fleets/prod/      # a fleet
├── bay.fleet.toml              # fleet settings
├── group_vars/                 # config + vault-encrypted secrets
└── hosts/                      # inventory
my-app/bay.toml                 # an app repo, names its fleet
```

This file is generated in part. `bay --skill` prints it; `make docs-skill`
in the framework repo rebuilds the generated sections.

## Rules — operating a fleet

- **Always go through `bay`.** Never call `ansible-playbook` directly — the
  CLI runs pre-deploy validation, version checks and other guards.
- **`bay validate` before any deploy that touches config.** An invalid
  Headscale ACL crash-loops the control server; an invalid `services.yml`
  reaches the host.
- **`git pull` the fleet before deploying.** A stale local clone silently
  reverts remote-only config to framework defaults.
- **A deploy ships app code, not just config.** `git_deploy` pulls each
  service's repo, so `bay deploy` can change what is running even when no
  framework or config file changed. Weigh blast radius accordingly.
- **Not every role runs on deploy.** Some (e.g. `outbound_monitor`) live in
  `provision.yml`, so a deploy will never apply them no matter which tags you
  pass — a role can sit broken for months this way. Check the CHANGELOG's
  *Upgrade notes* for `bay provision --tags <role>` instructions.
- **Enrolling a tailnet node does not grant it access.** Under a default-deny
  ACL an unlisted node is dead on arrival; rules are **directional** (a node
  that is reachable still cannot initiate); failures are silent, because
  tailscale ACLs are accept-only and an ungranted peer is simply absent from
  `tailscale status`. sshd `AllowUsers`/`ListenAddress` is a second, independent
  gate. **Read `docs/tailnet-naming.md` before `gateway enroll` or any ACL
  edit**, and verify with `bay gateway acl audit` — which only checks the
  inbound side.
- **Secrets live in `group_vars/<env>/secrets.yml`** under `ansible-vault`.
  Key casing is load-bearing: UPPERCASE = container env var, lowercase =
  Ansible role variable.
- **Rig infrastructure is not in `services.yml`.** Traefik, CrowdSec,
  Watchtower, Headscale, Zot and the webhook receiver are framework-managed
  roles; `services.yml` is the app surface only.

## Rules — developing the framework

- **Framework changes are invisible to users until tagged.** Commit in
  `bay/`, add a `CHANGELOG.md` entry, then `make release VERSION=X.Y.Z` (never
  `git tag`/`git push` by hand — `version.yml` would drift from the tags). Then
  `bay self update` on each machine.
- **For local iteration use `bay --fleet <path>`** to try a framework or fleet
  change without a release, and `bay self update --to <tag>` to move the
  installed copy to a given tag.
- **Alerts fan out from `roles/alert_channel`.** Call `bay_notify <literal.id>`
  with an ID registered in `alerts/registry.yml` — never add a private curl to
  the notification API of the day.

## Common tasks

| Goal | Command |
|---|---|
| First-time setup | Install Bay (`docs/install.md`), then `bay fleet init <name>`, then `bay init` in an app repo |
| Deploy services | `bay deploy production` |
| Deploy including infra roles | `bay deploy --rig production` |
| Recreate containers | `bay deploy production --tags deploy_stack` |
| Dry run | `bay deploy production -- --check --diff` |
| Provision a fresh server | `bay provision production` |
| Edit secrets | `bay vault edit production` |
| Check config before deploying | `bay validate` |
| Add a machine to the tailnet | `bay gateway enroll <name>`, then the ACL |

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

- `bay fleet` — Create, clone and list the fleets on this machine.
- `bay fleet init <name>` — Create a fleet at ~/.config/bay/fleets/<name>, or clone one with --from.
- `bay fleet ls` — List the fleets in ~/.config/bay/fleets.
- `bay self` — Show or change the Bay version installed on this machine.
- `bay self update` — Move this machine to the newest Bay release, or to the tag given by --to.
- `bay self version` — Print the installed Bay version and where the checkout lives.
- `bay status` — Show the installed Bay version, the fleet it works on, and the feature flags.

### Operations

- `bay admin-shell <host>` — Open an SSH session as the configured admin user on the named host.
- `bay alerts` — Inspect and configure Bay's alert surface.
- `bay alerts disable <pattern>` — Mute one or more alerts.
- `bay alerts doctor` — Diagnose the failure modes that have actually bitten.
- `bay alerts enable <pattern>` — Un-mute one or more alerts.
- `bay alerts list` — Show every alert with its effective per-recipient state.
- `bay alerts test [alert_id]` — Show — or with --live, prove — where an alert would be delivered.
- `bay build` — Inspect and reset the webhook build circuit breaker.
- `bay build reset [service]` — Reset the build circuit breaker after fixing the underlying issue.
- `bay build status` — Show circuit breaker state for all services on the target host(s).
- `bay deploy <env>` — Deploy services to the target environment.
- `bay gateway` — Manage the access gateway (headscale tailnet / wireguard).
- `bay gateway acl` — Inspect the tailnet ACL policy.
- `bay gateway acl audit` — Flag tailnet nodes that no accept rule can reach.
- `bay gateway add-user <name>` — Create a new headscale user (headscale only).
- `bay gateway apikey` — Generate a Headscale API key (headscale only).
- `bay gateway delete-node <name>` — Delete a node from the tailnet.
- `bay gateway delete-user <name>` — Delete a headscale user.
- `bay gateway enroll` — Enroll a device: create user, generate key, print the join command.
- `bay gateway key <name>` — Generate a pre-auth key for a user (headscale only).
- `bay gateway nodes` — List tailnet nodes with user, IP, and last-seen time (headscale only).
- `bay gateway rename-node <old_name> <new_name>` — Rename a node in the tailnet.
- `bay gateway rename-user <old_name> <new_name>` — Rename a headscale user.
- `bay gateway route-approve <node_name> <route>` — Approve or revoke an advertised route for a node.
- `bay gateway routes` — List all advertised routes across the tailnet.
- `bay gateway status` — Show access gateway status.
- `bay gateway user-info <name>` — Show details for a user and their nodes.
- `bay gateway users` — List all headscale users with node counts.
- `bay healthcheck <env>` — Hit every public service's domains and report reachability.
- `bay logs <service>` — Show container logs for a service, or operate on its log archive.
- `bay provision <env>` — Provision and harden a server (base OS, users, firewall, Docker).
- `bay prune <env>` — Reclaim disk space by pruning unused Docker images and build cache.
- `bay region` — Manage deployment regions (multi-region inventories).
- `bay region add` — Add a new region to an existing multi-region deployment (interactive).
- `bay restart [service...]` — Restart service containers without a full deploy.
- `bay restore <env>` — Run the restore playbook directly (low-level).
- `bay webhook <env>` — Deploy webhook infrastructure and show GitHub setup instructions.

### Stack Manager

- `bay server` — Manage inventory servers.
- `bay server add <ip>` — Add a server to the inventory.
- `bay server inspect [env]` — Inspect live network configuration from servers via SSH.
- `bay server list [env]` — List servers from the inventory.
- `bay server remove <ip>` — Remove a server from the inventory.
- `bay service` — Manage services and accessories in services.yml.
- `bay service add [catalog_id]` — Add a service or accessory from the catalog or a custom definition.
- `bay service catalog` — List available service/accessory definitions from the catalog.
- `bay service edit <name>` — Edit an existing service's configuration in services.yml.
- `bay service list` — List all configured services and accessories.
- `bay service prune-webhooks <repo>` — List and optionally delete orphan GitHub webhooks for a repository.
- `bay service remove <name>` — Remove a service or accessory from services.yml.
- `bay service show <name>` — Show the full configuration for a service or accessory.

### Vault

- `bay vault` — Manage encrypted secrets (ansible-vault).
- `bay vault decrypt <env>` — Decrypt a secrets file in place — leaves PLAINTEXT on disk.
- `bay vault edit <env>` — Edit encrypted secrets in $EDITOR (decrypt, edit, re-encrypt).
- `bay vault encrypt <env>` — Encrypt a plaintext secrets file in place.
- `bay vault set <env> <key> [value]` — Set one secret key non-interactively (decrypt, modify, re-encrypt).
- `bay vault view <env>` — View encrypted secrets (read-only, no temp files).

### Backup

- `bay backup` — Manage restic backups (list, run, restore, status, check).
- `bay backup check <accessory>` — Verify backup repository integrity (restic check).
- `bay backup list <accessory>` — List backup snapshots for an accessory (newest first).
- `bay backup restore <env> <accessory>` — Restore an accessory from a backup snapshot (interactive).
- `bay backup run [accessory]` — Trigger a backup now (one accessory, or all).
- `bay backup status` — Show the backup status dashboard (last backup, snapshots, repo size).

### Utilities

- `bay compile` — Compile bay.fleet.toml and every pinned bay.toml into services.yml.
- `bay doctor [env]` — Run pre-deploy checks: fleet, format, CLI, vault, boxes, repos, git, and more.
- `bay import` — Import a fleet from today's YAML files into bay.fleet.toml, bay.toml files and lockfiles.
- `bay secret` — Generate random secrets or hash passwords.
- `bay secret missing <env>` — List secret NAMES the services need that the env's vault lacks.
- `bay test` — Run the consumer's infrastructure tests (tests/test_infra.sh).
- `bay toml` — Work with bay.toml files.
- `bay toml validate [path]` — Check a bay.toml file against schema v3; print one line per violation.
- `bay validate` — Validate configuration files before deploying.

### Daily

- `bay adopt <name>` — Move a project's bay.toml and its files from the fleet into this app repo.
- `bay approve <plan_id>` — Approve a saved plan with destructive or shared steps.
- `bay init` — Draft a bay.toml in this app repo and register the app in the fleet.
- `bay plan [env]` — Compare WANTED (bay.toml at HEAD), PINNED (the lock) and RUNNING (the box).
- `bay remove <project>` — Plan the removal of a project: its containers stop, its data stays.
- `bay rollback [env]` — Return an environment to its previous pin and its previous code, and freeze it.
- `bay route` — Tailnet routes in bay.fleet.toml: add, list, remove, import.
- `bay route add <name>` — Add a tailnet route to bay.fleet.toml.
- `bay route import` — Move tailnet_proxies from the old YAML file into bay.fleet.toml, names kept.
- `bay route ls` — List the tailnet routes in bay.fleet.toml: name, domain, upstream, host, identity.
- `bay route rm <name>` — Remove a tailnet route from bay.fleet.toml.
- `bay show [name]` — Print WANTED, PINNED and RUNNING for a project, and a status per environment.
- `bay up [env]` — Pin the project's commit in the fleet and deploy it.

<!-- END GENERATED CLI REFERENCE -->

## Documentation map

Paths are relative to the framework root (`~/.local/share/bay/framework`).

<!-- BEGIN GENERATED DOC MAP -->

**Start here**

- `CHANGELOG.md` — What changed in each release, with upgrade notes. Read this before `bay self update`.
- `docs/install.md` — Install the `bay` command once per machine, update it with `bay self update`, and make or clone a fleet.
- `docs/features.md` — What Bay is, the full feature set, and how it compares to alternatives.
- `docs/onboarding.md` — Your first project: make a fleet, run `bay init`, the files a fleet keeps, and your first deploy.
- `docs/layout-scenarios.md` — Where every file lives, in fifteen scenarios: one box, an app with its own repo, two environments, a box move, rollback, a tailnet route and a fresh machine.

**Configuration**

- `docs/bay-toml.md` — The `bay.toml` schema v3 for Bay v2: every key, the rules, the corrected example and `bay toml validate`.
- `docs/bay-toml-blind-readers.md` — The blind-reader test that proves the `bay.toml` example has one reading, and its results.
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
- `docs/plan.md` — The daily verbs of Bay v2: `bay init`, `plan`, `approve`, `up`, `show` and `rollback`, the plan JSON and its verdicts (exit 0, 10, 20, 30), the risk table and the lockfile deploy record.
- `docs/deploy-receipt.md` — The receipt each box writes after a deploy, the `bay status --json` document, and the missing-secret check. Both JSON formats are versioned and stable.
- `docs/rollout-playbook.md` — Multi-host deploy playbook — deploy order, port-drift recreation, and the post-deploy audit checklist.

**Operations**

- `docs/backups.md` — restic backups — S3 config, per-accessory repos, retention, restore, and monitoring.
- `docs/multi-region.md` — Deploying one stack to multiple regional servers from a single fleet.
- `docs/alerting.md` — Where alerts go — Telegram plus an optional generic webhook sink (Campfire/Slack/raw), and the fail-open guarantees.
- `docs/debug-agent.md` — The `debugbot` limited-permission SSH user for AI-assisted read-only debugging.
- `docs/performance.md` — How fast a deploy is and why — Mitogen, SSH pipelining, and the `--profile` flag.

**Architecture & decisions**

- `docs/design-decisions.md` — Features and approaches explicitly decided against or deferred — with reasoning, so we don't re-litigate them.
- `docs/adr/001-docker-run-over-compose.md` — ADR — why container lifecycle uses `docker run` over Docker Compose.
- `docs/adr/002-log-archival.md` — ADR — host-side per-service log archival via cursor-based `docker logs --since`.

<!-- END GENERATED DOC MAP -->
