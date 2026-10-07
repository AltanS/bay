# Bay Documentation

The full reference for the Bay framework. Start with **[features.md](features.md)** for the
big picture and **[onboarding.md](onboarding.md)** to stand up your first project, then dive into
the topic you need below.

> New here? The fastest path is the [Quick start in the main README](../README.md#quick-start),
> then **[install.md](install.md)** and **[onboarding.md](onboarding.md)** for your first project.

## Start here

| Doc | What it covers |
|-----|----------------|
| [../CHANGELOG.md](../CHANGELOG.md) | What changed in each release, with upgrade notes. Read this before `bay self update`. |
| [install.md](install.md) | Install the `bay` command once per machine, update it with `bay self update`, and make or clone a fleet. |
| [features.md](features.md) | What Bay is, the full feature set, and how it compares to alternatives. |
| [onboarding.md](onboarding.md) | Your first project: make a fleet, run `bay init`, the files a fleet keeps, and your first deploy. |
| [layout-scenarios.md](layout-scenarios.md) | Where every file lives, in fifteen scenarios: one box, an app with its own repo, two environments, a box move, rollback, a tailnet route and a fresh machine. |

## Configuration

| Doc | What it covers |
|-----|----------------|
| [bay-toml.md](bay-toml.md) | The `bay.toml` schema v3 for Bay v2: every key, the rules, the corrected example and `bay toml validate`. |
| [bay-toml-blind-readers.md](bay-toml-blind-readers.md) | The blind-reader test that proves the `bay.toml` example has one reading, and its results. |
| [services.md](services.md) | The `services.yml` schema — services, accessories, access modes, env/secrets, ports, build, backups, update policy. The single source of truth for your app surface. |

## Access & networking

| Doc | What it covers |
|-----|----------------|
| [access-gateways.md](access-gateways.md) | VPN backends for `access: vpn` services — WireGuard vs self-hosted Headscale, traffic flow, split-DNS. |
| [tailnet-ingress.md](tailnet-ingress.md) | Trusted HTTPS for tailnet-only services on self-hosted Headscale — DNS-01 wildcard certs, default-deny ACL, per-device identity injection. |
| [tailnet-naming.md](tailnet-naming.md) | Naming and authorization on a Headscale tailnet — users vs node names vs `hosts:` aliases, ACL tags as classes, the hybrid pattern, onboarding, verification, rollback. |
| [crowdsec.md](crowdsec.md) | CrowdSec IDS/IPS — log parsing, the nftables bouncer, trusted IPs, and lockout recovery. |
| [forward-auth.md](forward-auth.md) | The ForwardAuth SSO gateway for putting an auth layer in front of services. |

## Build & deploy pipeline

| Doc | What it covers |
|-----|----------------|
| [build-strategies.md](build-strategies.md) | Choosing a build strategy — registry pull vs local build vs remote/cloud build. |
| [build-pipeline.md](build-pipeline.md) | Operator reference for the webhook → build → deploy flow: trigger files, circuit breaker, troubleshooting. |
| [build-pipeline-observability-contract.md](build-pipeline-observability-contract.md) | The CI-enforced contract mapping every pipeline exit path to an observable terminal state. |
| [reconciler.md](reconciler.md) | The server-side Python reconciler (`bay_reconcile`) — the sole container-deploy path since v0.97.0. |
| [plan.md](plan.md) | The daily verbs of Bay v2: `bay init`, `plan`, `approve`, `up`, `show` and `rollback`, the plan JSON and its verdicts (exit 0, 10, 20, 30), the risk table and the lockfile deploy record. |
| [deploy-receipt.md](deploy-receipt.md) | The receipt each box writes after a deploy, the `bay status --json` document, and the missing-secret check. Both JSON formats are versioned and stable. |
| [rollout-playbook.md](rollout-playbook.md) | Multi-host deploy playbook — deploy order, port-drift recreation, and the post-deploy audit checklist. |

## Operations

| Doc | What it covers |
|-----|----------------|
| [backups.md](backups.md) | restic backups — S3 config, per-accessory repos, retention, restore, and monitoring. |
| [multi-region.md](multi-region.md) | Deploying one stack to multiple regional servers from a single fleet. |
| [alerting.md](alerting.md) | Where alerts go — Telegram plus an optional generic webhook sink (Campfire/Slack/raw), and the fail-open guarantees. |
| [debug-agent.md](debug-agent.md) | The `debugbot` limited-permission SSH user for AI-assisted read-only debugging. |
| [performance.md](performance.md) | How fast a deploy is and why — Mitogen, SSH pipelining, and the `--profile` flag. |

## Architecture & decisions

| Doc | What it covers |
|-----|----------------|
| [design-decisions.md](design-decisions.md) | Features and approaches explicitly decided against or deferred — with reasoning, so we don't re-litigate them. |
| [adr/001-docker-run-over-compose.md](adr/001-docker-run-over-compose.md) | ADR — why container lifecycle uses `docker run` over Docker Compose. |
| [adr/002-log-archival.md](adr/002-log-archival.md) | ADR — host-side per-service log archival via cursor-based `docker logs --since`. |

## Historical (superseded)

Kept for the analysis only — do not treat their plans as live work. The self-hosted Headscale
path shipped instead; see [access-gateways.md](access-gateways.md) and [tailnet-ingress.md](tailnet-ingress.md)
for the current architecture.

| Doc | What it covers |
|-----|----------------|
| [external-tailscale-research.md](external-tailscale-research.md) | Feasibility research for using tailscale.com's hosted control server. |
| [external-tailscale-implementation-plan.md](external-tailscale-implementation-plan.md) | Implementation plan for the same — never built. |

## Glossary

- **rig** — The infrastructure layer: Traefik, CrowdSec, Watchtower, Headscale, Zot, the
  webhook receiver — deployed by dedicated roles, as opposed to the apps
  declared in `services.yml`. Tracked by a `.rig-state` file on the host; see the main
  README's "Deploy modes: rig vs fast" section.
- **service vs accessory** — In `services.yml`, a **service** is an app container that gets
  Traefik routing, SSL, and access control; an **accessory** is infrastructure (a database,
  a cache) deployed alongside services with no public routing of its own. See
  `services.md`.
- **canary swap** — The zero-downtime deploy pattern for `zero_downtime: true` services: a
  new container starts alongside the old one, passes health checks, then the old one
  stops — Traefik load-balances across both during the overlap. See `services.md`'s
  "Zero-Downtime Deploys" section.
- **config-hash gate** — The mechanism that decides whether a container needs to change: a
  SHA-256 hash (`bay_spec_hash`) over the compose-visible spec plus a digest of its
  rendered env file. Same container + same hash = no-op; a mismatch triggers a recreate or
  canary swap. The hash covers config text only, so the reconciler also compares the image
  id the container runs against the id its reference resolves to on the host, and a
  rebuilt image redeploys. See `reconciler.md`'s "Parity with the Ansible gate" section.
- **bundle** — The resolved deploy payload the server-side reconciler consumes: every
  container spec with secrets already merged and `config_hash` precomputed (vault is
  decrypted client-side, never on the host). See `reconciler.md`.
- **given_name** — A Headscale node's display name: what `headscale nodes list` shows, what
  MagicDNS resolves, and (under `tailnet_identity_enabled`) the identity injected into
  `X-Tailnet-Device`. Distinct from the enrolling user and from any `hosts:` ACL alias. See
  `tailnet-naming.md`.
- **control region** — In a multi-region deploy with `access_gateway: headscale`, the one
  region that runs the actual Headscale coordination server; every other region runs only
  the Tailscale daemon and registers against the control region's REST API. Set via
  `headscale_control_region`. See `multi-region.md`'s "Headscale Access Gateway in
  Multi-Region" section.
