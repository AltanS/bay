# Rollout Playbook (multi-host deploys)

Lessons from the 2026-04-22 `expose:` migration. Treat a deploy session as
an end-to-end operation, not a sequence of per-host successes.

1. **Before touching anything**, run `bin/bay validate` on each consumer
   and fix anything that fails. Vault gaps, stale refs, and same-stack
   link errors are easier to debug when they surface before the
   ansible-playbook output.

2. **Deploy order for multi-region stacks**: always deploy the region
   that owns the shared resource first (e.g. demo EU for postgres).
   Verify the resource is healthy on that region before touching peers.

3. **Port-drift requires manual recreation in some cases.**
   `roles/container_lifecycle/tasks/deploy_accessory.yml:16` has a
   "Remove container with stale port bindings" task that *should* fire
   when an accessory's host-IP changes, but today it silently no-opped
   on both demo postgres instances after an `expose: tailnet`
   migration. Until M83-S10 lands, after any accessory IP change verify
   by hand:
   ```bash
   ssh debugbot@<host> "docker ps --filter name=<acc> --format '{{.Ports}}'"
   # if old IP still bound:
   ssh argo-admin@<host> "sudo docker stop <acc> && sudo docker rm <acc> && sudo docker compose -f /opt/<stack>/docker-compose.yml up -d <acc>"  # legacy-argo: live host account value
   ```

4. **Post-deploy audit must hit user-facing URLs**, not just `docker ps`
   and `ss -tlnp`. Container-up with unhealthy-healthcheck is not the
   same as "service is serving 2xx to users". For each domain in
   `services.yml`:
   ```bash
   curl -sS -o /dev/null -w 'HTTP %{http_code}\n' --max-time 10 https://<domain>/
   ```
   A 4xx/5xx means the service is broken *right now*, regardless of
   whether the breakage predates your work. Until M83-S11 adds
   `bin/bay healthcheck`, do this by hand.

5. **Pre-existing unhealthy containers are still your problem during a
   rollout.** If a container is `Up N days (unhealthy)` when you finish
   deploying, `docker restart` it and verify it comes back. A silent
   Node death inside a container (nginx PID 1 stays alive, Node worker
   exits) does not trigger Docker's restart policy — the only way to
   recover is an explicit restart. If the restart doesn't fix it,
   *then* it's an investigation item, not before.

6. **Security-relevant verifications for data ports**
   (postgres/redis/mongo/etc.): after deploy, confirm the bind IP from
   the host itself:
   ```bash
   ssh debugbot@<host> "docker ps --filter name=postgres --format '{{.Ports}}'"  # expect <tailnet_ip>:5432 or 127.0.0.1:5432, never 0.0.0.0:5432
   ssh debugbot@<host> "curl --connect-timeout 3 -sS -o /dev/null -w 'exit=%{exitcode}\n' http://<host_public_ip>:5432"  # expect exit 7 (refused)
   ```
   External timeouts (exit 28) from a workstation prove only that
   upstream firewalls drop the SYN, not that Docker itself refuses —
   the on-host check is the one that matters.

7. **Separate your blast radius from others' pre-existing state in
   commit messages and reports.** Call out what you changed vs. what
   was already broken, with evidence (`docker inspect --format
   '{{.State.StartedAt}} Restarts={{.RestartCount}}'`). Don't hide
   pre-existing outages in a "mostly green" summary.
