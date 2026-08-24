# Server-side reconciler (`bay_reconcile`)

> **Status:** the **sole** container-deploy path, framework-wide, since v0.97.0
> (M85-S8). Parity-proven on sandbox (M85-S1–S7, S9), shipped in v0.96.0, soaked
> per-consumer behind a flag through v0.96.x, then made **unconditional** in
> v0.97.0 — the per-container Ansible deploy loops and the
> `bay_reconciler_enabled` flag were retired. Runs on **myapp** and
> **demo** (3-region). See [Operation](#operation) and [Rollout](#rollout-complete).

## Why

A deploy is ~480 Ansible task evaluations, each a controller→server round-trip,
across plays that re-establish connections — yet the actual container work is
cheap and *local on the server*. Profiling a no-op deploy showed container
reconciliation is **not** the wall-clock bottleneck, but it scales linearly with
service count (~10 Ansible tasks per container). The reconciler collapses that
whole layer into **one server-side pass** (observe in one batched `docker list`,
diff, execute), measured at **0.47s for a 6-container fleet** vs container_
lifecycle's ~60 tasks — a win that stays ~constant as the fleet grows.

## Architecture — functional core / imperative shell

```
desired (ContainerSpec)  ┐
                         ├─ plan() ─→ Plan(tuple[Action])  ─→ execute() ─→ JSON report
observed (ContainerState)┘  (pure)                            (DockerClient)
```

`src/bay_reconcile/` is **self-contained** (stdlib + docker SDK only — no
Typer/Ansible), so the whole package is shipped to the host and run there:

- `models.py` — frozen `ContainerSpec` / `ContainerState` / `Action` union
  (`NoOp`/`Create`/`Recreate`/`CanarySwap`/`Remove`) / `Plan` / `ExecutionReport`.
- `planner.py` — pure `plan(desired, observed)`: hash match → NoOp; missing →
  Create; drift → CanarySwap (zero-downtime service) else Recreate; port-binding
  drift forces a standard recreate (canary can't share a host port). Orphan
  removal (`remove_orphans`) defaults to the `container_lifecycle_cleanup` knob
  (true) — parity with the retired `cleanup.yml`; a bundle that doesn't
  enumerate every managed container never removes one.
- `observe.py` — pure `parse_state()` + port normalization (mirrors the
  `bay_port_*_tuple` filters).
- `executor.py` — phased (infra → accessory → service), concurrent within a
  phase; the 9-step canary swap with a rescue → standard-recreate fallback;
  every action recorded (observability contract).
- `docker_client.py` / `sdk_client.py` — the `DockerClient` Protocol (the
  mockable seam) and the docker-SDK-backed implementation.
- `__main__.py` — `python -m bay_reconcile <bundle.json> [--plan-only]`.

It is **typed to `mypy --strict`** and linted with ruff; `make typecheck` runs
both and is wired into `make lint`. The pure core is unit-tested against a
`FakeDockerClient` (no daemon).

## Parity with the Ansible gate

`config_hash` is the same SHA-256 the Ansible config-hash gate (M85-S1) stamps:
`bay_spec_hash(spec, env_digest=sha256(env_file))`. So a host transitioning
from the Ansible path to the reconciler sees **0 recreations** — verified on
sandbox (plan-only: 6 NoOp / 0 Recreate) and on myapp (11 NoOp / 0). Secrets
never enter the hash (it's an opaque digest) or any new store; the rendered
bundle (resolved env) lives at mode `0600` on the host — the same posture as the
env files — and is removed after the run.

**The reconciler folds the env digest into every container's hash** — see
`reconcile.yml`, which slurps each spec's `env_file` and computes
`bay_spec_hash(spec, env_digest=sha256(env_file))`. This was the subtle part of
parity: `type: infra` rig containers (traefik, zot, watchtower, …) with an
`env_file` must fold the digest too. Until v0.96.1 the Ansible infra path
(`deploy_infra.yml`, since retired) hashed the bare spec *without* the env
digest, so a rig container with an `env_file` stamped a hash the reconciler's
env-aware hash could never match — a spurious one-time recreate of
routing-critical containers on the first cutover (surfaced on the multi-region
demo stack; myapp's env-less rig containers happened to match and
masked it). v0.96.1 aligned the infra path, and v0.97.0 retired the Ansible
deploy paths entirely — the reconciler's bundle is now the single source of the
hash. Env-less infra containers hash identically either way.

## Operation

The reconciler is the container-deploy path inside `container_lifecycle`
(`roles/container_lifecycle/tasks/reconcile.yml`) — there is no on/off flag.
Two operational knobs remain:

| var | default | effect |
|---|---|---|
| `bay_reconciler_plan_only` | `false` | render + observe + plan, print the report, **mutate nothing** |
| `bay_reconciler_remove_orphans` | `container_lifecycle_cleanup` (true) | remove managed containers absent from the desired set (parity with the retired `cleanup.yml`) |

Dry-run any deploy (safe — mutates nothing) to preview the plan:

```bash
bin/bay deploy <env> -- -e bay_reconciler_plan_only=true
```

### Config-hash dependency (one-time restamp)

The NoOp decision depends on the `com.bay.config-hash` label the S1 gate
(v0.96.0+) stamps. A container created by a **pre-v0.96.0** framework has no such
label, so its first reconcile **recreates it once to stamp the label** —
zero-downtime services canary-swap, others recreate in place. One-time cost;
every deploy after is a clean NoOp. All current consumers are stamped, so a
normal deploy reads all-NoOp.

## Rollout (complete)

1. **sandbox** — validated (parity + 0.47s reconcile). ✓
2. Released v0.96.0 (engine), v0.96.1 (infra-path env-digest parity fix), and
   v0.96.2 (access_gateway allowlist append fix — M85-S10). ✓
3. **myapp** — flag-enabled in `group_vars/all/main.yml`; plan-only 11 NoOp /
   0 Recreate, real deploy `changed: false`, all containers healthy. ✓
4. **demo** (3-region: infra/eu/na) — cutover on v0.96.x: bump, redeploy on
   the Ansible path to re-stamp infra containers with env-aware hashes, gate on a
   plan-only that must read all-NoOp, *then* flip the flag. All regions healthy.
   (The env-digest fix above is why this gate is clean instead of recreating
   traefik on every host.) ✓
5. 1-week stability soak. ✓
6. **v0.97.0 (M85-S8):** retired the per-container Ansible deploy loops
   (`deploy_infra` / `deploy_accessory` / `deploy_service` + the `_prepare` and
   `cleanup` helpers) and removed the `bay_reconciler_enabled` flag — the
   reconciler is now unconditional. Orphan cleanup carried over via
   `remove_orphans` (defaults to `container_lifecycle_cleanup`). All consumers
   redeployed on v0.97.0 behind a plan-only gate (0 Recreate / 0 unexpected
   Remove), all containers healthy. ✓

The reconciler container pass is fast regardless of fleet size; the remaining
deploy wall-clock (connection/bootstrap, rig roles) is addressed by the rig-skip
fix (M85-S9) and, ultimately, a persistent-connection daemon.
