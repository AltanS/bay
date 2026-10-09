# Build Pipeline Reference

Operator-facing reference for the webhook → build → deploy pipeline. For the
formal exit-path/severity contract enforced by CI, see
[build-pipeline-observability-contract.md](build-pipeline-observability-contract.md).

For the high-level "which strategy do I want" question, see
[build-strategies.md](build-strategies.md).

## Trigger File Format

Trigger files live at `/opt/<stack>/triggers/<service>.trigger`. Their format
defines the correlation contract between the webhook receiver, the alias
fan-out, and rebuild.sh (v0.82.6+). Two writers make triggers: the receiver
and `rebuild.sh` (for alias fan-out only). See the ownership rule at the end
of this section.

- **Webhook-written triggers (primary path, format v2):**
  ```
  <corr_id>      # line 1: the UUID4 the receiver made for this push
  <pull_signal>  # line 2: "pull", or empty for a build
  <revision>     # line 3, optional, pull signals only: the commit the build server pushed
  <built_at>     # line 4, optional, pull signals only: epoch second the build finished
  ```
  A GitHub push writes line 1 only, and `rebuild.sh` builds. An image-level
  pull signal writes `pull` on line 2, and `rebuild.sh` pulls and restarts.
  The receiver writes the file in one `write_text` call
  (`roles/git_deploy/files/webhook/app.py`).

- **Alias fan-out triggers (dedup path):** line 1 only (CORR_ID, no pull
  signal). Alias rebuilds retag from the primary's local image rather than
  pulling from registry, so no pull signal is needed. `rebuild.sh` writes them
  when a primary service finishes a build, atomically through a temp file and
  `mv -f`. It is the only case where `rebuild.sh` writes a trigger.

- **Legacy one-line form (v1):** a file whose only line is `pull` is still a
  pull trigger, and an empty file is still a build trigger. `rebuild.sh`
  accepts both and logs the correlation id as `unknown`.

- **`[unknown]` in logs** — zero-byte trigger file (bare `touch`). Fixed in
  v0.82.6: all trigger writers now use `printf`+atomic-mv. If you see
  `[unknown]` after v0.82.6, the trigger was written by a manual `touch`
  (pre-fix operator habit) or by an external script that hasn't been
  updated.

- **`manual-<epoch>` in logs**: `rebuild.sh` ran with no trigger file, so
  it made its own correlation id, `manual-<epoch>`. It does not write a
  trigger file for this. This happens when an operator starts
  `bay-build@<service>.service` by hand, or when the unit starts and no
  trigger is there. A common case is a reboot after a clean failure:
  (1) `rebuild.sh` consumed the trigger, (2) the build failed cleanly and
  `rebuild.sh` exited, (3) the host rebooted before a new push arrived.
  The original CORR_ID is gone once `rebuild.sh` consumes the trigger. This
  is a **known limitation**, noted here for journalctl triage. Compare to
  reboot-after-hang, where the trigger survives and the original CORR_ID is
  preserved.

- **Ownership rule:** the receiver writes the trigger of a service, one
  per push or pull signal. `rebuild.sh` consumes the trigger it processes
  (it moves it to `<service>.trigger.running` and deletes that on exit). The
  only trigger `rebuild.sh` writes is the alias fan-out trigger above, for
  other services. It never writes content back to its own trigger file. Any
  build state belongs in `/opt/<stack>/state/<service>.json`. An operator may
  write a trigger by hand with `printf` (see
  [build-strategies.md](build-strategies.md)); a bare `touch` logs as
  `[unknown]`.

## Track, hold and freeze

Since 2.1 every build is tagged by its commit, and `:latest` moves only when
the push may deploy. The rules for each webhook build:

1. **Tag by commit.** `rebuild.sh` builds `<image>:<commit12>` (the first 12
   characters of the commit, `git rev-parse --short=12`). The image carries the
   labels `com.bay.commit=<commit12>` and
   `org.opencontainers.image.revision=<commit12>`. The deploy-time builds
   (`git_deploy/tasks/build.yml`, `remote_build.yml`) tag and label the same
   way. A remote build pushes `:<commit12>` always, and `:latest` only when the
   push may deploy. When `:<commit12>` is already in the registry, the build is
   skipped and the registry moves `:latest` with
   `docker buildx imagetools create --prefer-index=false` (a failure is
   `build.failed`, "Registry retag").
2. **Hold guard.** `_hold_reason` decides. (A held push waits for `bay up`. `bay up` deploys the whole box environment, not one project.) The push is held when:
   - the compiled `build.track` is `pin` (`[deploy.<env>] track = "pin"`).
     This holds every push, so `build.held` (warn) fires on every push of a
     `pin` project, by design: each one waits for `bay up`. Mute it per
     service with a TTL'd override (docs/alerting.md) if that is too loud;
   - the compiled `build.frozen` is true (`bay rollback` froze the env);
   - the `bay.toml` at the pushed commit has another canonical hash than the
     pinned one (`build.bay_toml_hash`, path `build.bay_toml_path`). The hash
     is the SHA-256 of the parsed TOML as sorted, compact JSON
     (`python -m bay_reconcile.tomlhash <file>`), so a comment or a key order
     change does not count. A file that is gone, is not valid TOML, or that the
     box cannot hash (no `tomllib`, Python older than 3.11, and no `tomli`)
     holds too: Bay never deploys a config it could not check.
   `bay_toml_hash` (and `bay_build_hash`, see "Config-only push") is compiled
   only for a project whose `bay.toml` lives in the app repo. A project in the
   fleet has nothing to compare.
3. **A held build is not a failure.** `_hold_build` logs
   `HOLD <svc> at <commit>: <reason>`, sends one `build.held` alert (warn) with
   the commit, the image and what to do ("config changed, run bay up"), and
   exits 0. The image keeps only its commit tag. `:latest` and the running
   container are not touched, the trigger was already consumed, and the
   circuit breaker is neither counted nor reset. A remote build that is held
   sends no pull signal.
4. **Deploy.** When the push may deploy, `_promote_latest` tags the old
   `:latest` as `:previous` and moves `:latest` to `:<commit12>`. So
   `:previous` rotates at promote time, before the health check: it is the
   image that ran before this push, not the last image that passed a check.
   A held build or a config-only push does not rotate it. When
   `:latest` already is that image (a rebuild of the running commit),
   `:previous` stays where it was, so the health-check rollback still has the
   last good image. The pull path does the same: the running image becomes
   `:previous` only when it is not the image just pulled. The container
   is recreated with the `com.bay.config-hash` label of the container it
   replaces, so the next `bay up` plans a noop for it (M116/03). Then
   `rebuild.sh` stamps the container's `commit` and `image` into the receipt
   (`python -m bay_reconcile.receipt stamp`, see
   [deploy-receipt.md](deploy-receipt.md)).
5. **Pull path.** An app box that gets a pull signal checks `track` and
   `frozen` from its own compiled config (the build server may be behind). A
   held pull fetches `<image>:<commit12>` only, so `bay up` and
   `bay rollback --to` find it, and sends `build.held`. A pull that may deploy
   also tags the pulled image as `<image>:<commit12>`.

`bay up` releases a hold: it pins the commit, compiles its config and asks the
box to point `:latest` at that commit's image before the container pass
(`bay_reconcile.codepin`). `bay rollback` points `:latest` back at the previous
receipt's image and freezes the env. Both are in [plan.md](plan.md), "Code and
config".

**The build lock covers the promotion and the start.** `rebuild.sh` takes the
build lock (`git_deploy_build_lock_path`, default `<stack_dir>/build.lock`,
an exclusive `flock` on fd 9) before any work and holds it until it exits:
through the build, the hold guard, the release, the move of `:latest`, the
container start, the health check and the receipt stamp. The pull path holds
it the same way. `bay_reconcile.codepin` takes the same lock before it moves
`:latest` or `:previous` for `bay up` or `bay rollback`, with a bounded wait
(`container_lifecycle_build_lock_wait`, default 300 seconds). So a deploy
cannot move `:latest` back to the pinned image after a build promoted its own
image and before it started it, which would start old code under the new
commit. When a build holds the lock past the wait, the deploy does not touch
`:latest`, the code move is `skipped` (`build running: ...`) and `bay up`
notes the container as kept. The lock file is created by the app user, mode
0644; codepin opens it read only, which is enough for `flock`. Only one build
runs at a time on a box, so the wait can also be for a build of another
service.

**The receipt names the image that started.** The container still starts by
tag: the reconciler finds a moved `:latest` through the reference a container
was started with, so a start by image ID would hide every later move. Right
after the promotion `rebuild.sh` reads the image ID of `:latest` (on the pull
path, of the pulled image) once. After `docker run` it reads the image ID the
container runs. When the two match, the receipt names the pushed commit, as
before. When they differ, `rebuild.sh` logs `started image differs from
promoted :latest: started <id12>, promoted <id12>; ...`, and the receipt names
the commit label of the started image, or no commit when it has none. It never
names the pushed commit for another image, and a failed health check then
marks no commit as failed. On the pull path, an image without a revision
label passes the revision check only when the container runs the image ID
read after the pull.

**A failed health check marks the commit.** When the container of a webhook
deploy fails its health check, `_handle_rollback` removes the tag
`<image>:<commit12>` of the failed build (`docker rmi` of the tag; the image
may stay under other tags) and appends `<commit12> <image id>` to
`/var/lib/bay/failed-commits/<container>` (the deploy creates the directory,
group `docker`, mode 0775). `bay_reconcile.codepin` refuses a target whose
commit or image is listed there: in `branch` mode the code move is skipped and
`:latest` stays, in `pin` mode the deploy stops. So a later `bay up` whose
WANTED is that commit never promotes the failed image, also not from the
registry and not under the commit tag a config-only push gave it. The
`build.rolled_back` alert names the commit. A deploy of the same commit that
passes its health check (a manual rebuild after a failure that was not the
code) clears its line. Each box keeps its own list: a box that never ran the
failed image does not know it failed elsewhere.

Troubleshooting a hold: `journalctl -u bay-build@<svc>` shows the `HOLD` line
and its reason. `docker image ls <image>` on the box lists the commit tags
that `bay rollback --to` can use.

### Config-only push

A push that changes only the project's `bay.toml` (`build.bay_toml_path`) and
the files its mounts read (`build.bay_toml_files`: every `from =` path,
relative to the repo root, without the `fleet:` ones; a directory counts for
every file under it) carries no code. `rebuild.sh` checks this first, right
after it reads the pushed commit and before the build and the hold guard, on
the box (local builds) and on the build server (remote builds).

Two projects can build from one app repo and branch, each with its own
`bay.toml` (for example `bay/web.toml` and `bay/admin.toml`). A push that
changes only the other project's `bay.toml` carries no code for this one
either, and `bay up` pins every project of the repo at the repo's head. So it
counts as config only for this project too, and its image gets the tag of the
pushed commit. The other project's `bay.toml` paths are compiled into the
script (`SHARED_TOML_PATHS`) and into the receiver config
(`shared_toml_paths`). Only the other `bay.toml` files count, not the files
their mounts read: such a file can be an input of this project's build. A
push of one `bay.toml` therefore tags the image of every project of that repo
and branch, on every box that builds it (local builds) or in the registry
(remote builds; the box pulls `<image>:<commit12>` at `bay up`).

The receiver lets a sibling's `bay.toml` push pass as a config-only candidate
only when it carries no code for the other project. If the same push also
changes other files, each must be a `bay.toml` or a file this project's mounts
read, or match this project's `watch`. A push of the sibling's `bay.toml` plus
the sibling's code, outside this project's `watch`, is not config only, but it
changes no build input of this project either: since 2.5.1 it is a
no-input-change push for this project (next section), so this image gets the
commit tag too, with no build.

The previous commit is the `com.bay.commit` label of the running container.
A container created before 2.1.0 has no such label (and on a build server
there may be no container). Then the previous commit is the commit that the
checkout was at before this run's pull or fetch: the last commit the box
built or deployed. Local builds share one checkout per repo and branch, and
the first service's run of a push pulls it for all. So each service keeps its
own mark, the git ref `refs/bay/seen/<service>` in that checkout: the commit
the checkout was at after that service's last pull. The first run after an
upgrade gives every service of the checkout a mark at the checkout's HEAD. A
remote build has one checkout per service and needs no mark. The changed files
are `git diff --name-only <previous> <pushed>`.

The push also needs an image of the previous commit:

1. `<image>:<commit12>` of the previous commit (`docker image inspect` on the
   box, `docker manifest inspect` in the registry for a remote build).
2. Else `<image>:latest`, but only when it holds the previous commit's code.
   The checkout also moves on a build that failed, and then `:latest` is
   older than the previous commit. `:latest` counts when its
   `com.bay.commit` (else `org.opencontainers.image.revision`) label names the
   previous commit. A local build from before 2.1.0 has no label. Then
   `:latest` counts only when the circuit breaker shows no failure, neither
   the commit nor the image is in the failed-commits record, and the running
   container (if any) runs that image. A remote build always needs the label
   (`docker buildx imagetools inspect`).
3. Else the push is not config only: the script logs
   `config-only push <commit12>: no image known to hold <previous>, building`
   and the normal path runs.

A config-only push logs `config-only push <commit12>: run bay up` and ends the
run. It builds no image, does not move `:latest`, recreates nothing, and sends
no `build.held` or other alert. The circuit breaker is not touched, and the
trigger was consumed at the start. The image of the previous commit also gets
the tag `<image>:<commit12>` of the pushed commit (a tag, not a build), so
`bay up` finds the code for the pushed commit. When the image came from
`:latest`, it also gets the tag of the previous commit, so the next push finds
it by its tag. Nothing is recreated, so a container from before 2.1.0 still
has no label after a config-only push. The fallback to the checkout keeps
working, and the next build that deploys gives the container its label.
When no previous commit is known, the commit is not in the checkout, nothing
changed, or no image of the previous commit is found, the normal path runs.
If a tag command fails (a registry that refuses the tag, say), the push is not
config only. The script logs `config-only push <commit12>: tag failed`, counts
a failure for the circuit breaker, sends `build.failed` with the output of the
tag command, and exits 1. It builds nothing.

A registry tag is a carbon copy: the new tag points at the same manifest, so
it has the same digest. `rebuild.sh` runs `docker buildx imagetools create
--prefer-index=false` for every single-source retag (the config-only tag and
the `:latest` repoint of a skipped build). Without the flag, buildx wraps a
single manifest in a new image index. The child is the same, but the top-level
digest is new. On a box with the containerd image store the image ID is that
digest, so the reconciler would read the retagged image as a new one and
recreate the container for a config-only push. The remote strategy needs
buildx 0.15 or newer on the build server, the first version with
`--prefer-index`. There is no fallback: on buildx 0.12 to 0.14 every retag
fails with `unknown flag` and the push ends as `build.failed`. A test fails
when any `imagetools create` under `roles/` has one source and lacks the
flag, also when the command continues over several lines.

A change of what the image is built from is never config only. `bay compile`
writes `build.bay_build_hash` next to `bay_toml_hash`: the canonical hash of
`[build]`, `image` and their `[deploy.<env>]` and `[services.<name>]`
overrides (`python -m bay_reconcile.tomlhash --section build <file>`). A push
is config only when that hash of the pushed `bay.toml` equals the pinned one.
An edit of `[build.args]`, `dockerfile`, `context` or `target` therefore
builds the new image, and the hold guard holds it (the whole `bay.toml` hash
differs): `bay up` deploys it. A services file with no `bay_build_hash` is
never config only. The webhook's
`watch`/`ignore` filter runs before this (in the receiver, before the trigger),
so a push that it filters out never reaches `rebuild.sh`. A push that changes
`bay.toml` or a mounted file always passes that filter. "Edit `bay.toml`,
push, `bay up`" is the clean flow: the push does nothing on the box, and
`bay up` deploys the config. The adopt commit of `bay adopt` is such a push,
once `bay up` has written the new script: run `bay up` before `git push`
(docs/plan.md, "bay adopt").

### No-input-change push

`bay up` pins every project of a repo at the repo's head. So every project that
builds from the pushed repo and branch needs an image tagged with the pushed
commit, also when the push changed nothing it is built from. Before 2.5.1 the
receiver skipped such a push, the project had no `<image>:<commit12>` for the
head, and every `bay up` noted `code: kept <project>` (gap 39).

The build inputs of a project are:

- its `watch` patterns (`build.paths.include`), else its `[build]` context when
  that is a subdirectory of the repo (then the files under it, the Dockerfile
  and `<Dockerfile>.dockerignore` count), else every file;
- minus its `ignore` patterns (`build.paths.exclude`);
- plus its own `bay.toml` and the files its mounts read (the config-only rule
  decides those).

The `bay.toml` of another project of the repo is never an input.

**Receiver.** A push on the deploy branch that changes none of these files is
not skipped any more. The receiver writes the trigger with line 2
`no_input_change` (line 1 is the correlation ID), logs
`no input change for <svc>: tag only (<reason>; files: [...])`, answers HTTP 200
with `"status": "triggered", "mode": "no_input_change"`, and sends no
`webhook.received` alert. A trigger that is already waiting is never replaced
by the marker: it is a normal push or a manual rebuild, and its build covers
this push. A force push or a push of 20 or more commits is always a normal
trigger.

**`rebuild.sh` checks, it never trusts the marker.** One trigger can cover
several pushes, so the receiver's view can be stale. Right after the
config-only check (same place, both strategies, before the build and the hold
guard), a run with the marker diffs the previous commit (the same one the
config-only check uses) against the pushed head itself:

1. A changed file that is the project's own `bay.toml` or a file its mounts
   read: an input. A `bay.toml` of another project of the repo
   (`SHARED_TOML_PATHS`): never an input.
2. The other changed files go through `python3 -m bay_reconcile.pushinputs`
   with the project's `watch`, `ignore` and narrower context (`INPUT_ARGS` in
   the script). It is a stdlib port of the receiver's `pathspec` gitignore
   matching (`tests/test_no_input_change.py` checks both give the same
   answers). A pattern it cannot read counts as an input change.
3. For a `bay.toml` in the app repo, the `[build]` hash at the head must be the
   pinned `bay_build_hash`, as for a config-only push.

No input changed: the run takes the config-only tag path (`_config_only_push`):
the same source image rules (`<image>:<prev12>`, else a `:latest` that holds
the previous commit), the same carbon copy (`docker tag` on the box,
`docker buildx imagetools create --prefer-index=false` in the registry). It logs
`no-input-change push <commit12>: tagged, run bay up` and exits 0: no build, no
`:latest` move, no recreate, no alert, the circuit breaker untouched. When the
previous commit is the pushed one (a redelivered push), there is nothing to tag.
A failed tag command is a failed push, as for config only:
`no-input-change push <commit12>: tag failed`, stage `No-input-change tag`,
`build.failed`, exit 1.

It builds, logged, only when its own diff finds an input change (the
receiver's view was stale) or cannot read a pattern or the `[build]` hash:
`no-input-change push <commit12>: an input changed since <prev12>, building`.
The build is of the real head, and the hold guard still decides whether it
deploys.

It skips the push, as the receiver did before 2.5.1, when there is no previous
commit to diff from (none is known, or the checkout does not have it) or no
image holds the previous commit. It logs one line and exits 0, with no build,
no tag, no recreate and no alert:
`no-input-change push <commit12>: no previous commit known, skipped (as before)`
or `no-input-change push <commit12>: no image for the previous commit <prev12>, skipped (as before)`.
Building there would start builds and restarts for pushes that change nothing
the project uses. The project then has no image for that head, as before; the
next push that changes one of its inputs builds it.

A project with no `watch` and no narrower context counts every file, so every
push builds it, as before. A project with only `ignore` gets the tag for a
push of only ignored files. A project in the fleet gets the tag too: it has no
`bay.toml` in the app repo, so there is no `[build]` hash to compare.

A `rebuild.sh` from before 2.5.1 reads any line 2 but `pull` as a normal
push, so a new receiver in front of an old script builds such a push. An old
receiver never writes the marker.

### Order of the guards on a push

A push meets these checks in this order. The first one that applies decides.

In the webhook receiver, before any trigger file exists:

1. A deleted branch, or a ref that is not the deploy branch, is ignored (HTTP 200).
2. A push that changes the project's `bay.toml` or a file its mounts read (`bay_toml_path`
   and `bay_toml_files` of the receiver config, a file under a listed directory too), or the
   `bay.toml` of another project that builds from the same repo and branch
   (`shared_toml_paths`; only when every other changed file is a config file or matches
   `watch`), passes, whatever `watch` and `ignore` say. The receiver logs
   `config file changed: <files>` and writes the trigger, so the config-only check (7) and the
   hold guard (9) decide. A project in the fleet has no such paths.
3. The `[build] watch` and `ignore` lists (compiled to the include and exclude path
   filters, gitignore syntax), or the build context when there is no `watch`, filter the
   other pushed files. A push that fails the filter changes no build input: the receiver
   writes the trigger with the marker `no_input_change` (see "No-input-change push"), logs
   `no input change for <svc>: tag only`, and sends no alert. A force push, or a push of 20
   or more commits, skips both checks and writes a normal trigger.

In `rebuild.sh`, for a trigger that got through:

4. The circuit breaker is open: exit 0 (one alert per hour).
5. A pull signal for a held project (`track = "pin"` or frozen): fetch the commit tag only,
   exit 0. A pull-only service exits 0 here as well.
6. Fetch or pull, and read the commit.
7. Config-only check (see "Config-only push" above): only `bay.toml`, its mounted files and
   the `bay.toml` of the other projects of the repo changed since the previous commit (the
   container's label, else the checkout's HEAD before the pull, as this service last saw it)
   and the `[build]` hash is the same. Tag the previous commit's image (its commit
   tag, else a `:latest` that holds it) with the commit, exit 0.
   A project in the fleet never gets the keys for this, so every push of it builds.
   With the `no_input_change` marker, the no-input-change check runs next (see
   "No-input-change push"): no input in the diff, tag and exit 0; no previous commit
   or no source image, skip and exit 0; an input in the diff, build.
8. Build `<image>:<commit12>`, unless that image already exists.
9. Hold guard `_hold_reason`, in this order: `track = "pin"`, frozen, `bay.toml` missing at
   the commit, `bay.toml` hash unreadable, `bay.toml` hash differs from the pinned one. A hold
   keeps the commit tag, does not move `:latest`, sends `build.held` and exits 0.
10. Otherwise move `:latest`, recreate the container, run the health check and stamp the
    receipt. A failed health check rolls back on the box and exits 1.

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
- **Recovery:** `bay --fleet <path> build reset <svc>` on your machine
  (see `--help` for flags). `build` does not take the fleet from the
  directory you stand in, so name it with `--fleet` or `BAY_FLEET`. See "Webhook Auto-Build Troubleshooting" below
  for the manual JSON fallback.
- **Incident (2026-04-16)** — `blog` on the `demo` NA region. Build failed,
  rollback succeeded, but CB counter reached 3 (old threshold). 2+ hours
  of pushes silently hit the CB guard with no Telegram. Triggered the
  CB-visibility work (CLI surface). Threshold bumped 3→5 in v0.76.1.

## Webhook Auto-Build Troubleshooting

**Build server webhook not receiving pushes:**
- Check `docker logs bay-webhook` on the build server (203.0.113.14)
- Verify the GitHub hook URL is `https://<webhook domain>/webhook/<container name>`, for example
  `https://deploy.example.com/webhook/shop`. The receiver answers a POST only on
  `/webhook/<container name>` (and `/webhook/pull-image` for the build server's pull signal).
- Probe the receiver: `curl https://deploy.example.com/health` (a GET, no HMAC) returns
  `{"status": "ok", "services": <count>}`
- If CrowdSec blocked the GH IP: `ssh debugbot@203.0.113.14 "sudo cscli decisions list"`

**Build failures (no container restart):**
```bash
# Check build service logs on build server:
ssh debugbot@203.0.113.14 "journalctl -u bay-build@<svc>.service --since '1h ago' --no-pager"
# Check state file for CB status:
ssh debugbot@203.0.113.14 "cat /opt/demo/state/<svc>.json"
# If CB is open (consecutive_failures >= git_deploy_cb_max_failures=5), reset it from
# your machine, not on the box. Bay writes the state file on the box over SSH:
bay --fleet <path to the demo fleet> build reset <svc>
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

`bay build` runs on your machine and reaches the box over SSH. Name the fleet with
`--fleet <path>` or `BAY_FLEET`: `build` does not take it from the directory you stand
in. The `ssh` lines below run one command on the box. `bay-admin` is the `admin_user`
of the example fleet (`example/group_vars/all/main.yml`), `bay` (in `sudo -u bay`) is its
`app_user`, and `debugbot` is the default
`debug_agent_user` (`roles/debug_agent`). Use the accounts of your own fleet.

```bash
# Check CB state (or: ssh debugbot@<host> "cat /opt/<stack>/state/<svc>.json"):
bay --fleet <path> build status
# Reset CB (writes clean state + sends Telegram audit; see --help for flags):
bay --fleet <path> build reset <svc>
# Manual reset (if CLI unavailable):
ssh bay-admin@<host> "sudo -u bay printf '{\"version\":1,\"consecutive_failures\":0,\"opened_at\":null,\"last_failure\":null,\"alerts\":{\"opened_sent\":false,\"last_blocked_alert_at\":null}}\n' > /opt/<stack>/state/<svc>.json"
# After reset, push again or touch trigger to re-fire:
ssh debugbot@<host> "touch /opt/<stack>/triggers/<svc>.trigger"
```

**Health check failure causing rollback loop (per-service timeout override):**
```yaml
# services.yml (compiled form): increase rebuild.sh's wait window for slow-starting services
# (default git_deploy_health_check_timeout: 90 seconds, roles/git_deploy/defaults/main.yml)
services:
  myapp:
    health_check_timeout: 180  # seconds — use for JVM/DB-warmup heavy services
```
`bay.toml` has no key for the per-service value, and `bay compile` writes
`services.yml`. In such a fleet, raise `git_deploy_health_check_timeout` in the
fleet's group_vars. That value applies to every service.
The Docker container `healthcheck.start_period` and `rebuild.sh`'s
`HEALTH_CHECK_TIMEOUT` are independent: Docker uses `start_period` to
suppress early failures from its restart policy; rebuild.sh uses
`HEALTH_CHECK_TIMEOUT` to decide when to roll back. Both need to be large
enough for the slowest legitimate cold start. If `rebuild.sh` rolls back
before `start_period` expires, the issue is `health_check_timeout`
(rebuild.sh side), not `start_period` (Docker side).

`health_check_timeout` has a **second consumer**: the post-deploy
`bay healthcheck` URL probe uses it as the readiness window for a
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
  is on (`websecure` alone otherwise — unchanged for non-split fleets).
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
  build server webhook → `docker buildx build --push` straight to the Zot
  registry (`:sha` always, plus the moving tag of `image`, usually `:latest`,
  in the same call unless the push is held; a held push stops here) →
  `X-Bay-Pull-Signal` HTTP call to each region's webhook → deployment
  server writes `pull` trigger → `bay-build@.path` fires `rebuild.sh` →
  `docker pull` + `docker stop`/`docker rm` + `docker run` + health check. There is no compose project on the box for this container. See "Webhook
  Auto-Build Troubleshooting" above.
- **Remote builds push from BuildKit, not from the local daemon** — the
  build uses `--push`, so the image is exported once, straight to the
  registry. Two consequences. First, a remotely built image is *not* in the
  build server's local image store afterwards; `docker images` there will not
  list it and the local content-hash tag pruning has nothing left to prune.
  Second, **registry credentials now matter at build time, not at push time**.
  A build server that can build but cannot authenticate to the registry used
  to fail at a separate `docker push` with a clear message; it now fails
  inside `buildx build`, and the auth error appears at the tail of the build
  log. If a build fails with a `401`/`denied` near the end of the output,
  check `docker login` on the build server before suspecting the Dockerfile.
- **Registry layer cache is opt-in** — `git_deploy_registry_cache: true` adds
  `--cache-to`/`--cache-from type=registry,ref=<repo>:buildcache`. Default is
  off. See `docs/build-strategies.md` → "Registry layer cache (opt-in)".
- **Remote builder over the tailnet is opt-in.** Set
  `git_deploy_remote_builder_endpoint` and the build server sends builds to a
  remote BuildKit over mTLS. When the remote does not answer, builds use the
  local builder. Each build logs one `builder=<name>` line in
  `journalctl -u bay-build@<svc>`. See
  [build-strategies.md](build-strategies.md#remote-builder-over-the-tailnet).
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

- **When it's rendered** — Automatically on every `bay deploy`,
  including `--tags deploy_stack`, `--tags build`, AND `--tags git_deploy`.
  The render lives in a dedicated, self-contained task file
  (`roles/git_deploy/tasks/render_image_map.yml`) that the role
  includes early enough to compute its own facts under any tag context.
  No separate `--tags git_deploy` step is required after a `services.yml`
  change touching `image:` or `build:` blocks — the next normal
  `bay deploy <env>` keeps the map current.

- **When the receiver picks up changes** — The `bay-webhook` container
  loads its map once at startup, so a fresh render only takes effect
  after a container restart. The render task fires an Ansible handler
  (`Restart bay-webhook` in `roles/git_deploy/handlers/main.yml`) when
  the destination file content changes; Ansible's idempotence means the
  handler is a no-op when the rendered content matches what's already on
  disk. Operators do **not** need to run `docker restart bay-webhook`
  manually after a normal deploy — the framework handles it.
  The same handler fires when the receiver config (`config.json`, the list
  of build containers) changes, because the receiver reads it once at start
  too. The handler only restarts. A new receiver *image* is applied
  separately: `roles/git_deploy/tasks/render_webhook.yml` builds
  `bay-webhook:latest` when the receiver files changed, the image is
  missing, or the image's `com.bay.receiver-hash` label differs from the
  hash of the receiver files (`bay_tree_hash` in
  `filter_plugins/bay_filters.py`: sha256 over the file contents in sorted
  path order; `__pycache__`, bytecode, dotfiles other than `.dockerignore`, `*~` and `*.swp`
  are left out, and a dangling symlink is an error). That file runs under `deploy_stack`
  too, so `bay up` builds it. The container spec carries the same label, so
  a receiver change changes the container's config hash: `bay plan --remote`
  predicts the recreate, although check mode builds nothing. Under `bay up`,
  the `deploy_stack` container pass recreates the receiver when the label or
  its image ID differs from `bay-webhook:latest`. A `--tags git_deploy`
  run reconciles the receiver alone (`roles/git_deploy/tasks/webhook.yml`)
  through the same spec and reconciler, so it is enough after a release that
  changed the receiver. That reconcile waits for the receiver env file that
  `deploy_stack` renders. On a new box, `bay up` creates the receiver.
  The spec also carries `com.bay.receiver-config-hash`, a hash of the rendered
  `config.json` and `image-map.json`, so a config change recreates
  `bay-webhook` in the plan and in `applied`; the restart handler is then only
  a safety net.

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
> bay deploy <env> --tags git_deploy
> docker restart bay-webhook
> ```
>
> After upgrading to this framework version, standard `bay deploy`
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
  fan-out happens. Multi-region fleets MUST set `region: <name>` in
  `group_vars/<region>/main.yml` for fan-out to work.
- **`git_deploy_peer_webhook_urls` is fleet-defined** — a dict of
  `region: https://deploy.<region>.<domain_base>` pairs in
  `group_vars/all/main.yml`. Empty default is fine for single-region
  fleets; required on multi-region fleets where any service's
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
