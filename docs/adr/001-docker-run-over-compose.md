---
# ADR-001: Docker Run Over Docker Compose for Container Lifecycle

- **Status:** Accepted
- **Date:** 2026-04-16
- **Relates to:** `container_lifecycle` role, `rebuild.sh.j2`

## Context

Bay deploys Docker containers to production servers via Ansible. Two independent paths create and update containers:

1. **Ansible deploys** -- the `container_lifecycle` role uses `community.docker.docker_container` to manage container state declaratively.
2. **Webhook-triggered rebuilds** -- `rebuild.sh.j2` (rendered per-service) runs `docker run` commands to swap containers after a GitHub push.

Both paths derive their container specifications (image, labels, volumes, env, networks, healthchecks) from `services.yml` through Jinja2 templates.

The question is whether Docker Compose should be used instead of (or alongside) direct `docker run` / `docker_container` for managing the service container lifecycle.

## Decision

Use standalone `docker run` semantics -- Ansible's `community.docker.docker_container` module for deploys and rendered bash scripts for webhook rebuilds -- instead of Docker Compose for service container lifecycle management.

Docker Compose is used only for the narrow case of boot-time infrastructure recovery (`docker-compose.infra.yml` with `docker compose up -d --no-recreate`), where its idempotent "ensure exists" behavior is a good fit.

## Reasons

### Zero-downtime canary deploys

The `container_lifecycle` role implements a canary swap pattern:

1. Create a `-new` container with the same Traefik labels (Traefik load-balances across both).
2. Wait for the canary's healthcheck to pass.
3. Stop the old container (Traefik shifts all traffic to the canary).
4. Remove the old container.
5. Rename the canary to the final name.

Docker Compose's `up -d` performs a simple stop-then-start, causing a brief window where no container is serving. Compose has no native concept of canary containers, label-based traffic shifting, or health-gated cutover.

### Label parity between paths

Docker Compose injects `com.docker.compose.project`, `com.docker.compose.service`, and other internal labels on every container it manages. These labels cannot be suppressed.

Ansible's `docker_container` module compares the full label set when determining whether a container needs recreation. If a webhook rebuild creates a container via Compose but the next Ansible deploy uses `docker_container`, the missing Compose labels are detected as drift, forcing an unnecessary container recreation (with downtime). The reverse is also true.

By using the same `docker run` semantics in both paths, both produce identical label sets derived from the same `services.yml` source. No phantom drift.

### No phantom state

Compose tracks "project" membership via labels and maintains implicit state about which containers belong to a project. An accidental `docker compose down` in the stack directory would remove containers that Ansible expects to exist, with no warning or guard.

With standalone containers, each container is an independent unit. There is no project-level operation that could cascade-remove unrelated containers.

### Single source of truth

Both the Ansible role and `rebuild.sh` derive container specifications from `services.yml` through Jinja2 templates. Adding Docker Compose as a third derivation path (services.yml -> Jinja2 -> docker-compose.yml -> Compose runtime) adds a layer of indirection without providing benefit.

## Consequences

- **`rebuild.sh.j2` must bake in `docker run` commands** that duplicate some logic from the `container_lifecycle` role (label construction, volume mounts, network attachment). This duplication is acceptable because both derive from the same source (`services.yml` + shared Jinja2 macros).

- **Container creation config exists in two places** -- the Ansible role (Python/YAML) and the bash template (shell). A change to the container spec schema must be reflected in both. In practice this is rare because both consume the same rendered spec structure.

- **Boot-time recovery uses Compose intentionally** -- `docker-compose.infra.yml` is rendered for infrastructure containers (Traefik, CrowdSec, Headscale) and used by `bay-infra-boot.service` at startup. This is a narrow, one-directional use of Compose (ensure containers exist after a crash) where its idempotent behavior is a good fit and label drift is not a concern because these containers are not managed by the webhook path.

## Alternatives Considered

| Alternative | Why rejected |
|---|---|
| Docker Compose for webhook restarts | Label pollution causes drift detection on next Ansible deploy, forcing unnecessary recreations |
| Docker Compose for everything (Ansible generates compose files, both paths use `docker compose up`) | Loses the canary deploy pattern; Compose's stop-then-start causes downtime on every update |
| Docker Swarm | Overkill for the target scale (~3 servers, ~10 services); adds operational complexity (raft consensus, overlay networks, secret management) without clear benefit |
| Watchtower for all updates | Available and deployed for monitoring, but polling-based (minimum 30s interval); too slow for webhook-driven deploys where sub-second trigger-to-deploy is expected |
