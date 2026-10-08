# Changelog

Notable changes to Bay, newest first.

Machines move between releases with `bay self update`. Read the entries
between your installed version and the latest before upgrading. Anything
needing manual action is called out under **Upgrade notes**. Entries for
1.x and older describe the earlier model: a clone of Bay in `.bay/` and a
`bin/bay` wrapper, which 2.0 removes.

## [2.5.0] - 2026-10-08

A registry retag keeps the image ID, the deploy receipt names the running code of tag-only images, a backup run checks every snapshot, and the database role step stops reporting `changed` on every run.

### Added

- The deploy receipt names the commit of a container that has no commit label, when its image has exactly one commit tag (12 lowercase hex) in the container's own image repository. A tag of another repository never counts. Container rows get `commit_source` (`label`, `tag` or null) and `commit_tags` (the own-repo commit tags of an unlabelled container's image, else null). The change is additive: `receipt_version` stays 1 and `status_version` stays 2.
- `bay show` prints `code <c12> (tag)` when the commit comes from a tag, also when the pin is one of several own commit tags. `bay show --json` has `envs[].running.code_source`.
- `bay plan` names each build container whose running code is unknown: `code of <name> on <box> is unknown (no commit label or tag); up will try to pin it to <c12>`. In `track = "pin"` the note ends `up will stop unless <box> has or can pull an image tagged <c12> (track pin)`. For an in-fleet project or an adopt commit, the note has no last clause. There is no new step and no change to the exit codes.
- Filters `bay_scram_verifier`, `bay_md5_verifier` and `bay_db_password_verifier` compute a Postgres password verifier on the control node.
- A check-mode task, "Predict database, role and password changes", reads the database state in a read-only session.
- A test fails when a single-source `docker buildx imagetools create` under `roles/` has no `--prefer-index=false`, also when the command continues over several lines.

### Changed

- The backup role accepts restic 0.19.x. The assert is now `>= 0.17.0` and `< 0.20.0`. The size check reads the JSON from the last stdout line that starts with `{`, so a progress line before it no longer breaks the check.

### Fixed

- A registry retag copies the manifest as it is. `rebuild.sh` runs `docker buildx imagetools create --prefer-index=false` at all three single-source retag sites: the config-only push (remote strategy) and the `:latest` repoint when the commit is already in the registry. Before, buildx wrapped the manifest in a new index: same child, new top-level digest. On a box with the containerd image store that digest is the image ID, so a config-only push recreated the container (gap 34).
- `rebuild.sh` renders `IMAGE_REGIONS` in sorted order. `bay_image_region_map` iterated a set, so the task "Deploy rebuild script" reported `changed` on every run (gap 35).
- The backup size check reads every snapshot the run made, by id (`restic stats <id>`). Before, a run with several databases checked only the last one (`stats latest`). A small snapshot fails the run, and the alert names the file. A size that cannot be read now fails the run with `backup.failed`.
- A second failing database in one run no longer trips `set -u` in `backup_from`.
- `bay up` no longer notes `kept` when the box already runs the target commit, for example after a local build that tagged only `:latest`. The move is `noop` with `already running <c12>`. If `:latest` points at another image, codepin first moves that old `:latest` to `<repo>:previous`, then points `:latest` back at the running image (gap 37).
- The task "Provision databases, roles and grants" no longer reports `changed` on every run (gap 36). A read step fetches each role's verifier kind and its SCRAM `<iter>:<salt>`, never the keys. The control node computes the verifier of the password from the encrypted secrets file. The server sets the password only when the stored verifier differs. A rotated password is still set and reported. The password and the verifier travel as psql variables, never inside the SQL text (see the review fixes).

### Pre-release review fixes (2.5.0)

- Database passwords never sit inside SQL text (security). In the first 2.5.0 draft, and in every release before, the password was a literal inside `DO $bay$ ... $bay$`. A password with `$bay$` in it ended that body. With a newline and a psql meta-command after it, the rest ran as SQL as the database superuser; a test proves the old template created a superuser role that way. The script now sets the password and the computed verifier as psql variables (`\set bay_pw '...'`, with every `'` and every `\` doubled and a newline written as `\n`) and reads them as `:'bay_pw'`. Each statement is a guarded `SELECT format(...)` run by `\gexec`, so the script has no `DO` block left. The check-mode predict script holds only the verifier. A YAML int password is cast to a string, so it is compared too. Tests run passwords with `'`, `\`, `$bay$`, `$$`, `:`, `;`, spaces, a newline, a leading `-` and an int against real Postgres 17 and 18: each sets, logs in, and the next run reports no change.
- Commit tags count only in the container's own image repository. A 12-hex tag of another repository on the same image no longer names the commit and never becomes the receipt's `image`. The codepin noop check uses the same rule. The new receipt field `commit_tags` lists the own-repo commit tags of an unlabelled container's image. When `commit` is null and the pinned commit is among them, `bay plan` and `bay show` read the running code as the pin, source `tag`. So after a config-only push gives the running image a second tag, `bay show` keeps `code <pin12> (tag)`.
- The unknown-code note in `track = "pin"` now says that `bay up` will stop unless the box has or can pull the image. The verdict stays: the plan reads only the receipt, not the images on the box, and a push in pin mode builds that image, so `bay up` does not always fail.
- The retag guard test joins backslash-continued lines before it matches. Before, a one-source `imagetools create` whose line ended in `\` read the backslash as a second source and passed without the flag.

### Upgrade notes

- Remote build strategy: the build server needs buildx 0.15 or newer. On buildx 0.12 to 0.14 every retag fails with `unknown flag` and the push ends as `build.failed`. There is no fallback. The next `bay up` renders the new `rebuild.sh`, so no separate deploy is needed. Index tags that are already in the registry stay. A box that already runs one has the same image ID.
- Backups: the restic pin stays 0.17.3. The role never upgrades an existing binary. The backup scripts reach a box only through `bay --fleet <fleet> deploy <env> --tags backup`, because `bay up` runs only the stack, unless the plan has a volume backup step (then `bay up` adds the `backup` tag). Redirect the output of that command, because `bay deploy` has no `--log`.
- Receipt and plan: no manual step. The first receipt after the upgrade fills the commit. Until then, the plan reads the old receipt and may note unknown code for a container that has a tag.
- Databases: no manual step. A role stored as md5 on a scram-sha-256 server is set once more, then compared. `bay plan --remote` (deploy in check mode) now opens two read-only psql sessions per database accessory. The comparison needs a superuser `POSTGRES_USER`, which the official image makes. Without one, the old behaviour stays. Test CI with docker pulls `postgres:17-alpine` and `postgres:18-alpine`. `BAY_TEST_PG_IMAGES` overrides them.

## [2.4.0] - 2026-10-08

The plan predicts the receiver recreate and drops a false push note, two projects in one app repo both get commit tags, jobs get a timeout and entrypoint-safe commands, a failed dump no longer stores a cut-off backup, and a route of a box that has not deployed since the pin reads `unknown`.

Spec 01: plan and up notes tell the truth

- `bay up` no longer asks you to push a `bay adopt` commit that is already on a branch of the remote. It asks the remote first (as the plan already did). The note "is the bay adopt commit; no code moves, git push it after this bay up" now comes only when the commit is not on the remote, or when Bay cannot tell. In every case `bay up` still takes the adopt commit and moves no code for it. Before, the 2.2.0 gates printed this note for eight apps whose adopt commits were already pushed.
- `bay plan --remote` predicts the `bay-webhook` recreate when the receiver changes. Bay hashes the receiver files on the control node (`bay_tree_hash` in `filter_plugins/bay_filters.py`: sha256 over the file contents in sorted path order, with `__pycache__`, bytecode, dotfiles (but not `.dockerignore`), `*~` and `*.swp` left out, a dangling symlink an error, and mtimes and modes not read). It puts the hash on the image and on the container as the label `com.bay.receiver-hash`. The label is part of the container spec, so a new receiver changes the config hash, also in check mode, where the image is not built. The box then predicts `recreate`, with the reason `labels: differ for com.bay.receiver-hash`. With the same receiver files it predicts `noop`. Before, the check-mode run saw the old image ID and predicted `noop`, and the real `bay up` then recreated `bay-webhook` on every box.
- The receiver image is also rebuilt when its `com.bay.receiver-hash` label is not the hash of the receiver files. Before, an image that an earlier run left stale (files copied, build failed) stayed until the files changed again.
- Docs: plan.md (the adopt note, the check mode list, the receiver image), build-pipeline.md and reconciler.md.

Spec 02: commit tags for two projects in one app repo

- A push that changes only the `bay.toml` of one project now also counts as config only for every other project that builds from the same repo and branch. `bay up` pins each project of the repo at the repo's head, so each image needs the commit tag of that push. Before, the push gave the other project no tag, and the next `bay up` kept its code (`code: kept <container> (<image>:<commit12> is not on this box)`). The second adopt commit of two projects in one repo hit this.
- The compile adds no key. At render time, `rebuild.sh` gets `SHARED_TOML_PATHS` per service and the webhook receiver config gets `shared_toml_paths`: the `bay.toml` paths of the other projects of the same repo and branch (filter `bay_shared_toml_paths`). Only a project whose `bay.toml` is in the app repo gets them. The files that the other project's mounts read do not count, because such a file can be an input of this project's build.
- The webhook receiver passes a push of one of those files whatever `watch` and `ignore` say, and logs `config file changed: <files>`, as it does for the project's own `bay.toml`.
- Local builds share one checkout per repo and branch. For a container with no `com.bay.commit` label (created before 2.1.0), the second service's run read the HEAD that the first service's pull had left, saw no change, and built. Each service now keeps its own mark in the checkout, the git ref `refs/bay/seen/<service>`: the commit the checkout was at after that service's last pull. The first run after the upgrade gives every service of the checkout a mark at the current HEAD. Remote builds have one checkout per service and are unchanged.
- A remote build still tags in the registry only. The box pulls `<image>:<commit12>` at `bay up`.
- Docs: build-pipeline.md ("Config-only push" and the order of the guards), the observability contract (row 20, and the line numbers of every row), bay-toml.md and plan.md ("bay adopt").

Spec 03: deploy idempotence, job timeouts and entrypoint-safe commands

- Env directory (gap 33): the zot task that creates `<stack_dir>/env` now sets the same owner, group and mode as `deploy_stack` and `traefik` (owner app user, group `docker`, mode 0750). It set 0755 with group app user, so on a box that ran all three tasks the mode flipped on every deploy and two tasks reported changed. Docker reads the zot env file as root, so 0750 is enough. A test parses the roles and fails when the tasks disagree.
- Jobs: a new optional job key `timeout` (integer seconds, 1 to 86400, default 3600) becomes `TimeoutStartSec` of the job. The shared `bay-job@.service` carries the default 3600. Each job gets a drop-in `bay-job@<job>.service.d/timeout.conf` with its own value. Past the limit systemd stops the run and `ExecStopPost` removes the container. The compile writes `timeout` into `jobs:` only when the job sets it. A job that is gone loses its drop-in. The timer keeps no `Persistent=` setting, so a run missed while the box was off is not made up. The docs say so.
- Command forms: `release` (project, per environment) and a job `command` accept a string or a non-empty list of strings. A string runs as `/bin/sh -c "<cmd>"` with the image entrypoint overridden, so an image with `ENTRYPOINT ["node"]` no longer runs `node sh -c ...`. A list keeps the image ENTRYPOINT and is passed to it as the arguments, so `release = ["bin/migrate", "up"]` runs `node bin/migrate up`. The same rule holds in the reconciler (`run_release`), in `rebuild.sh` (`_run_release`) and in the job script. In the shell templates each list element is quoted on its own, and the hostile-render test covers the new paths.
- The compiled output of a string `release` or `command` and of a job without `timeout` is unchanged, byte for byte. The compile writes a list as a YAML list. The `services.yml` schema accepts both forms.
- Docs: `bay-toml.md` and `services.md` describe the two command forms, `timeout` and the missed-run rule.

Spec 04: backups that cannot be cut off

- No backup method pipes its dump into `restic backup --stdin` any more. A dump that failed mid-stream closed the pipe like a finished one, and restic stored the cut-off dump as the newest snapshot before the script sent `backup.failed`. restic now runs the dump command itself (`restic backup --stdin-from-command`) and stores no snapshot when the command exits non-zero. This covers `pg_dump`, `mysql` (`mysqldump`), `redis` (the `docker cp` of the RDB) and `file` (every volume backup, the Headscale state and an accessory with `method: file`). The dump commands are unchanged: the same `docker exec` user, env and flags. The snapshot file names are unchanged (`<name>.sql`, `<name>-<db>.sql`, `<name>.rdb`, `<name>.tar`), so `restore.yml`, `bay backup list`, `bay backup restore` and the snapshot size check read them as before.
- The `backup.failed` text changes. A failed dump reads `Dump command failed with exit code <n>, no snapshot stored (<file>)`; the script reads `<n>` from restic's error line (`command failed: exit status <n>`), and the dump command's own error is in the journal. When restic fails for its own reason, the text is `Restic backup failed with exit code <n> (<file>)`. If a later restic changes the wording of that error line, a failed dump falls back to the restic text: still an alert, still no snapshot.
- `pg_dump` with several databases: a failed database no longer hides behind a later one that succeeds. Before, only the exit codes of the last database were checked. Each database is still its own snapshot, the next one still runs, and the alert names the first failure.
- The backup role stops when the restic at `backup_restic_bin` is older than 0.17.0, the first version with `--stdin-from-command`, or is 0.19.0 or newer (see the review fixes). The role still installs `backup_restic_version` (0.17.3) only when no binary is there. It does not upgrade an existing one. Under `--check` the version is not read and the check is skipped.
- A deploy waits for a running volume backup (lock `<stack_name>_<volume>.lock`) before it recreates containers, in the same task and with the same 300 s timeout as for accessory backups. Only volumes whose container runs on the box are waited for.
- Redis restore fix: `bay backup restore` of a `redis` accessory wrote a tar archive to `/data/dump.rdb`. The `<name>.rdb` snapshot holds the output of `docker cp <name>:/data/dump.rdb -`, a tar with one entry `dump.rdb`, and the restore copied it as a plain file. It now unpacks it with `docker cp - <name>:/data`. The backup format does not change, so existing Redis snapshots restore correctly with 2.4.0. The container is stopped while the file is replaced and now starts again also when the restore fails (before, a failed restore left it stopped).
- Docs: backups.md describes the new command, the failure behavior and the volume lock wait.

Spec 05: route status and the last route removal

- `bay show --routes` says `unknown`, not `drift`, for a route whose box has not deployed since the pin. The receipt must list routes and must come from a deploy with the pinned routes: Bay reads the compiled services file at the receipt's fleet commit and compares its `tailnet_proxies` with the pinned map. The check costs one git call for the whole table. `--json` has a new `running_current` field.
- Removing the last tailnet route now removes `dynamic/tailnet-proxies.yml` from the ingress box. Traefik drops the routers, the receipt lists no routes, and the plan step converges. Check mode predicts the removal. A fleet with routes renders the file as before.
- Docs: plan.md and tailnet-ingress.md.

Pre-release review fixes (2.4.0)

- Route status: the `git log -G` pin check is gone. It missed domain list items (an alias edit) and matched `domains:` and `entrypoint:` lines of services. Bay now reads the compiled services file at the receipt's fleet commit (one git call) and compares its `tailnet_proxies` with the pinned map. Equal: the receipt speaks (`ok` or `drift`). Different, a commit the local clone does not know, or no `fleet_commit`: `unknown`. A route alias edit reads `unknown` until the ingress box deploys; an unrelated service edit leaves `ok`.
- Webhook receiver: a push of a sibling project's `bay.toml` passes only when every other changed file is a config file or matches this project's own `watch`. A sibling `bay.toml` plus sibling code outside the watch is skipped for this project, as in 2.3.0. Before, it passed, and `rebuild.sh` refused it as config only, so it built and recreated this project. A skipped push can still end in a build when a later push of only a sibling `bay.toml` has the skipped code in its diff range. That is safe (docs/build-pipeline.md).
- A failed tag command on a config-only push is a failed push. `docker tag` and `docker buildx imagetools create` ended in `|| true`, so the push reported success and `bay up` then found no `<image>:<commit12>`. The script now logs `config-only push <commit12>: tag failed`, counts a failure for the circuit breaker, sends `build.failed` with the output of the command, and exits 1. It builds nothing. New row 22 in the observability contract.
- The backup role refuses restic 0.19.0 and newer: 0.19 prints a progress line before the JSON of `restic stats --json`, so the snapshot size check would fail after every backup. A box needs restic 0.17.0 up to 0.18.x.
- A `tailnet_proxies:` that is null (an empty key in YAML) reads as no routes in the Traefik role (the removal task, the render task and the template) and in the Headscale templates. `default({})` alone keeps null and `length` of it failed.
- A dump that exits 0 and writes nothing no longer leaves a snapshot. restic fails with `no data read` but still stores a snapshot of the empty file. The script removes it (`restic forget` of the id restic printed for this run), sends `backup.failed` with `Dump produced no data, no snapshot stored (<file>)` and exits 1. The script also reads the exit code of a failed dump from restic's `Fatal: command failed:` line, not from the first `command failed:` in stderr. backups.md says `--stdin-from-command` and notes the Redis restore change.
- CI installs the restic the role pins (version and sha256 from `roles/backup/defaults/main.yml`, checked before use) and sets `BAY_TEST_RESTIC`, so the backup tests run against the real binary. The tests do not use the network.
- String commands: a string `release` or job `command` bypasses the image ENTRYPOINT entirely. A wrapper entrypoint ending in `exec "$@"` does not run. bay-toml.md and services.md say so and recommend the list form for an image that needs its entrypoint.
- The deploy wait for volume backups names its locks with `stack_name | default('bay')`, so a render problem cannot turn into "wait for nothing" under `ignore_errors`.
- `bay_tree_hash` leaves editor files out (dotfiles other than `.dockerignore`, `*~`, `*.swp`) and raises on a dangling symlink instead of skipping it. The Dockerfile of the receiver copies named files only, so none of these is a build input. The hash value differs from the first 2.4.0 builds; the label is new in this release, so nothing is affected.
- Jobs: the deploy prunes an orphan `bay-job@<job>.service.d` directory of a removed job even when its timer and script are already gone, and reloads systemd. The job `timeout` has a maximum of 86400 seconds in both schemas.

### Upgrade notes

- `bay-webhook` is recreated once per box. The first `bay up` (or `bay deploy`) of each box env with a build app after 2.4.0 rebuilds the receiver image and recreates the container: the receiver code changed (spec 02), and the running container and its image have no `com.bay.receiver-hash` label yet (spec 01). `bay plan --remote` shows this as a `bay-webhook` recreate step with the reason `labels: differ for com.bay.receiver-hash`. It is expected, and it belongs in the 2.4.0 allowed set. The next plan predicts `noop` for `bay-webhook`.
- Shared app repo rule: a push that changes only the `bay.toml` of one project is config only for every project that builds from the same repo and branch, so each of their images gets that commit's tag. `rebuild.sh` and the receiver config change on every box that builds a project. Only projects that share an app repo with another project get a non-empty `shared_toml_paths`. A project of a shared repo whose pinned commit has no image tag (the second adopt commit before 2.4.0) gets it from the next push that changes only a `bay.toml` of that repo, or from a redelivery of that push in the GitHub webhook settings, once 2.4.0 is deployed on the box that builds it. The build log says `config-only push <commit12>: run bay up`. Nothing builds or recreates.
- Job timeout: every existing job gets a `timeout.conf` drop-in with the default of 3600 seconds on the next deploy. A job that ran longer than an hour is now stopped. Set `timeout` on it first. The `deploy_stack` role carries the job tasks, so a normal `bay up` installs the drop-ins. No `provision` run is needed.
- String commands run through the absolute path `/bin/sh`: a string `release` or job `command` runs as `/bin/sh -c "<cmd>"` with the image entrypoint overridden. A release or job that ran as `sh -c "<cmd>"` on an image with an exec-form ENTRYPOINT was broken before (the entrypoint received `sh -c ...`). An image with no `/bin/sh` cannot run a string command. Use a list there, which keeps the image ENTRYPOINT. A string command bypasses the image ENTRYPOINT entirely: a wrapper entrypoint that sets things up and ends in `exec "$@"` no longer runs for a string `release` or job `command`. Use the list form for an image that needs its entrypoint.
- Backup script: the new script reaches a box only when the `backup` tag runs, and a plain `bay up` runs `deploy_stack` only. On each environment with `backup_enabled: true`, run `bay --fleet <path> deploy <env> --tags backup` once. The deploy wait for volume locks comes with the next `bay up` or `bay deploy`.
- restic must be 0.17.0 or newer and older than 0.19.0, and the backup role now asserts it: a box with another restic stops the `backup` tag with a message. Remove the binary and run the tag again, and the role installs 0.17.3. The role does not upgrade an existing restic. All boxes with backups ran restic 0.17.3 at release time, so no upgrade is needed.
- restic 0.19 is not supported yet, and the role refuses it (see the review fixes). Do not move `backup_restic_version` to 0.19 without a fix to the snapshot size check: restic 0.19.0 prints a progress line on stdout before the JSON of `restic stats --json`, and the script reads that JSON with `jq`.
- `mysql`: the password still reaches the dump as `docker exec -e MYSQL_PWD=<value>`, as before. That argument list is now also restic's, so the value shows in the process list of the restic process too, as it did for `docker`. restic does not store the command in the snapshot.
- The first `bay up` or `bay deploy` that touches a box with the zot S3 driver sets its `<stack_dir>/env` to 0750, group `docker`. The next one reports no change for that directory.
- Route status `unknown` rule: `bay show --routes` reports a route as `unknown` until the ingress box has deployed from a fleet commit whose compiled `tailnet_proxies` equal the pinned ones. Only such a receipt can report `drift`. An edit of a service in the compiled file does not make a route `unknown`; an edit of a route alias does. A receipt with no fleet commit, or with a commit that the local fleet clone does not know, also reads `unknown`, so pull the fleet before you read the table.
- A fleet that removed its last route and still has the old route file on the box: the next `bay deploy <env> --tags traefik` (or any `bay up` on the ingress box env) deletes the file. Nothing changes for a fleet that still has routes.

## [2.3.0] - 2026-10-08

Every key the validator accepts now deploys or is an error. The receipt lists routes, and `bay validate` checks the boxes against the inventory.

Spec 04: the compiler deploys what it validates

- `release`: the compile writes it on the main container (the env value wins). The reconciler runs it once in a one-shot container `<name>-release` of the new image, before it creates or recreates the container. A non-zero exit or a timeout (`bay_release_timeout`, 600 s) fails that action, and the old container keeps running. A push build runs it before the swap. On a failure `build.failed` fires and the old container keeps running. For a local build `:latest` stays where it was. For a remote build the build server has already pushed `:latest` to the registry, and that stays moved: the box pulls the image, runs the release with it, and on a failure points its local `:latest` tag back at the running image. A change of `release` alone recreates nothing.
- `[[jobs]]`: compiled to a top-level `jobs:` map (`of`, `schedule`, `on_calendar`, `command`, `memory`). `deploy_stack` installs a script, the one-shot `bay-job@.service` and a UTC `bay-job@<job>.timer` on the box of the main container. The job runs the main container's current image, env file, network and mounts. A job that is gone loses its timer and script. A cron line that sets both the day of the month and the day of the week is a compile error. A job change is a safe plan step of kind `job`.
- A routed service with neither `image` nor its own `build` shares the project build. It gets a copy of the main build plus `build.shared_from`. `bay_build_dedup_map` makes the member without `shared_from` the primary, so the main container builds and the service re-tags its image. An internal service that does this stays unsupported.
- Service `path`: the service gets the main domains and a `Host(...) && (Path(p) || PathPrefix(p/))` router. Its priority is the rule length, so it outranks the main container's router in every shape. The prefix is not stripped. `bay validate` checks (domain, path) pairs for collisions.
- Volume backups: a volume mount with `backup` true (the default) compiles to a top-level `volume_backups:` entry (`container`, `path`, and `schedule` and `retain` from the project `[backup]`, then the fleet `[defaults.backup]`). The backup role adds a restic `file` target `<stack_name>_<volume>`. It reads the path through the Docker daemon (`docker cp`). `bay backup restore <env> <stack_name>_<volume>` extracts into the volume's mountpoint with the container stopped. A change is a safe plan step of kind `backup`, and `bay up` then also runs the `backup` tag.
- `access` keys other than `mode` on a main container that is internal in an environment are a validation error. Before, the compiler dropped them silently.
- `[tailnet] allowlist`: compiled to a top-level `tailnet_allowlist:` list. On every deploy it becomes `vpn_allowed_ips`, with `127.0.0.1` and `::1` kept, before the Headscale range is appended. `bay validate` warns while group_vars also sets `vpn_allowed_ips`. `bay gateway status` prints the enforced list, and `bay doctor` fails when it is empty.
- Docs: plan.md, bay-toml.md, services.md, tailnet-ingress.md and backups.md describe the keys above as deployed.

Pre-release review fixes (specs 04 and 05)

- `-release` is a reserved service suffix, like `-vpn`, `-public`, `-health` and `-new`. A service key that would collide with the one-shot `<name>-release` container is a compile error and a `bay validate` error. A failed push build removes a leftover `<name>-release` container only when its `com.bay.release-of` label names the service.
- The reconciler no longer force-removes a running `<name>-release` container. It removes a leftover only when it is created, exited or dead, and fails the release with a message that names the container when one is still running (a migration that a webhook build started).
- `release` together with `update = "auto"` is a bay.toml validation error, for the project and per environment. Watchtower recreates the container without running the release.
- `path = "/"` on a service is a validation error: it would take every request of the domain. `/api` and `/api/` are one route. Two services whose paths differ only by the slash collide in `bay toml validate` and in the `bay validate` domain check. The match stays case-sensitive, unlike `vpn_routes`; docs/bay-toml.md says why.
- `bay backup restore` of a volume stops every running container that mounts the volume, not only the owner, and starts that same set afterwards (also after a failure). When no targeted box runs the container of the volume, the restore fails and names the container. Before, every box ended and the command printed "Restore complete".
- A failed `bay up` that ran extra tags (`backup`, or `headscale,traefik` for a route) prints the retry: `bay deploy <env> --tags <the tags that up used>`. The failed up has already committed `services.yml`, so a second `bay up` plans no step and would skip those tags. The JSON result has `deploy_tags`.
- A cron step as wide as its field (`0 */24 * * *`, `*/60 * * * *`) compiles to the first value of the field. It became `00/24`, which systemd rejects.
- The receipt no longer crashes on a route file that is not valid UTF-8: `routes` is `[]`.
- The container monitor skips containers with the `com.bay.job-of` label in the restart-loop check. A job that runs every few minutes raised `container.restart_loop`. A failing job still raises `container.crash`.
- The job script scan on the box no longer warns about a missing `jobs/` directory on every deploy of a box that has no jobs.
- Docs: layout-scenarios.md and the README say that path, jobs, volume backups and routes deploy since 2.3.0. tailnet-ingress.md and this entry say that `bay doctor` fails on an empty allowlist, not that it shows it (`bay gateway status` prints it). plan.md says what a route-only up records as `receipt_commit`. build-strategies.md says that `bay up` deploys the whole box environment, and a test keeps that sentence in every doc that names `bay up`.

Spec 05: routes, boxes and validator checks

- The deploy receipt lists the tailnet routes the box serves. `bay_reconcile.receipt` reads the route file the traefik role rendered (`dynamic/tailnet-proxies.yml` in the stack directory) and writes it as `routes`: one `{name, domains, upstream, pass_host_header, identity_inject, entrypoint}` per route, `[]` on a box with no route file. The role passes `stack_dir` in the receipt meta. The field is additive: `receipt_version` stays 1 and `status_version` stays 2. `status.schema.json` declares it. `bay show --routes` now has a RUNNING column to compare.
- `bay status --env <env>` (no `--json`) prints one line per box of that env: the result, the deploy time, the container count and, for a receipt that lists routes, the route count. Plain `bay status` still reads no box. `--no-remote` skips the read.
- Route-only plan and up. When no project has `[deploy.<env>]` and `<env>` is the box env of `[tailnet] ingress_box`, `bay plan <env>` compiles the whole fleet at its pins and shows the route steps (and any other compile difference). The plan has no project and the note `route-only plan for <env>`. `bay up <env>` writes `services.yml`, commits `bay: up <env> (routes)`, deploys `<env>` (with `headscale,traefik` on a route step), reads the receipts and pushes the fleet. It pins nothing and writes no lock. A plan with zero steps still deploys. A step that belongs to a project blocks a route-only plan, because nothing would pin it. Any other env with no project keeps the old note and refusal.
- `bay validate` checks that `[tailnet] ingress_box` is the Headscale host: `access_gateway` must be `headscale`, and with `headscale_control_region` set, the ingress box's region (`region` from `group_vars/<box group>/`, else the box group, else the box name) must equal it.
- `bay validate` checks the boxes against the inventory: the box env needs a host or a group of that name in `hosts/<box env>` (error, with the `[<box env>:children]` hint), a box `group` must be a group there (error), and a box name that is neither a host nor a group there is a warning.
- One `access_gateway` default. `config.access_gateway_type()` returns the value of `group_vars/all/access_gateway.yml`, else the value in `roles/access_gateway/defaults/main.yml` of the framework checkout (`wireguard`). `bay doctor`, `bay gateway`, `bay region` and `bay validate` use it. Before, `bay doctor` read a fleet with no `access_gateway.yml` as `none`, and `bay gateway` assumed `headscale` in one place.
- `bay doctor`: with `wireguard`, an empty `vpn_allowed_ips` (or an empty `[tailnet] allowlist`) fails only when a service uses `access: vpn`. A fleet that serves everything public needs no list.

Docs fix

- backups.md: the dump commands and the manual Headscale restore used `docker compose`. Bay creates the containers with `docker run`, and the compose file only describes the stack. The dump table now names `docker exec` and `docker cp` on the container named after the accessory key, and the Headscale restore runs `docker stop headscale` and `docker start headscale`.

### Upgrade notes

- `backup = false` is no longer needed on volume mounts. A volume mount with the default now gets a backup entry. The backup runs when `backup_enabled` is true in the box env. A backup change makes `bay up` run the `backup` tag as well.
- When `bay.fleet.toml` sets `[tailnet] allowlist`, it is enforced: it replaces `vpn_allowed_ips` from group_vars for the `vpn-only` allowlist on the next deploy. No live fleet sets it today. If you add it, make sure it lists every address that must still reach tailnet-mode services. Run `bay validate` first and read the warning.
- The first `bay up` (or `bay deploy`) of each box env after 2.3.0 ships the new `bay_reconcile` and rewrites that env's receipt with `routes`. Until then `bay show --routes` reports RUNNING as `unknown` and `bay status --json` shows no `routes`.
- A `bay up <ingress box env>` with no project on that env now deploys (route-only) instead of stopping with "nothing to deploy". Read its plan first: it deploys the whole box environment.
- `bay validate` (and the pre-deploy gate of `bay deploy` and `bay up`) can now fail on a fleet whose `ingress_box` is not the Headscale host, whose hosts file has no group named after the box env, or whose box `group` is missing from the hosts file. Fix the fleet file or the hosts file.
- `bay doctor` on a fleet with no `group_vars/all/access_gateway.yml` now reports `wireguard` (what the boxes deploy), not "no access gateway".
- A bay.toml with `release` and `update = "auto"` (project or environment) now fails `bay validate` and `bay toml validate`. Set `update = "notify"` or `"off"`, or drop `release`. A service with `path = "/"` fails too: write a prefix. Rename a service key that ends in `-release`.
- `bay backup restore` of a volume now stops every container that mounts the volume for the length of the extract. Plan for the downtime of each of them.
- After a failed `bay up` with a route or a backup step, do not run `bay up` again to retry. Run the `bay deploy <env> --tags ...` line that the error prints.

## [2.2.0] - 2026-10-08

Spec 01: plan, show and rollback tell the truth

- `bay plan` in branch mode shows a step when the box runs code older than WANTED (a held push, or a build not deployed yet): kind `image`, action `update`, risk `safe`, one per container. `bay up` already moved that code; the plan said nothing before. The note `code at <running>, config pinned at <pin>` ends with `, WANTED <commit>` when WANTED is not the running code.
- `bay show` names the running code: RUNNING prints `<container> code <commit12>` per build container (`code ?` when the receipt names none). `bay show --json` has `envs[].running.code`, and each container has `commit`.
- New status word `ahead`: WANTED is ahead of the pin and every build container runs WANTED (a push deployed it). Run `bay up` to pin it. `behind` now means the box does not run WANTED yet. Order: HALF, unknown, drift, ahead, behind, ok.
- `bay up` exits 40 when the deploy failed only because a build container has no image yet (the first deploy of a build app). It names the way out: push to the deploy branch, wait for the build, run `bay up` again. A push cannot build on a box that has no clone of the app repo yet, because `rebuild.sh` never clones. The note adds a second step for the case where the box builds the app itself: run `bay deploy <env>` once with no `--tags`. That run clones the repo, builds the first image and deploys. The JSON result adds `first_image`. Any other failed deploy still exits 1. The receipt marks a failed container action with `failed: true`, so `bay up` can tell the two apart.
- Plain `bay rollback` takes its code target from the lock: `bay up` records `previous.containers` (the commit and image each build container ran before it) when it moves a pin. A later deploy of the box env no longer moves the rollback target. A container with `commit: null` keeps its image and shows in `code_kept`.
- `bay status --json` schema: `fleet.source` lists only the four sources Bay sets; `cwd` is gone.
- deploy-receipt.md: a container built from source before 2.1 has `commit: null` until its first build after 2.1.

Spec 02: the webhook follows bay up

- `bay up` registers a new build app with the webhook receiver and enables its build trigger. The `git_deploy` role renders, under the `deploy_stack` tag too: the receiver directories, the HMAC key, `config.json`, the receiver files and the `bay-webhook:latest` image (built when the files changed or the image is missing), the trigger units `bay-build@.path`, `bay-build@.service` and `bay-build-alert@.service`, `select-builder.sh`, `build-alert.sh`, and the enable of `bay-build@<container>.path` for each build container. The work moved from `webhook.yml` and `systemd.yml` into `render_webhook.yml` and `render_trigger_units.yml`.
- A change of `config.json` restarts the receiver (`Restart bay-webhook`). The receiver reads the file once, at start.
- `bay up` stops and disables `bay-build@<container>.path` for every container that left the box. After `bay remove`, no trigger of the removed app is left running.
- The webhook receiver passes a push that changes the project's `bay.toml` or a file its mounts read (a file under a mounted directory too), whatever `watch` and `ignore` say. It logs `config file changed: <files>`. Such a push now always reaches the config-only rule and the hold guard. You no longer add `bay.toml` and the mounted files to `watch`. The receiver config carries `bay_toml_path` and `bay_toml_files` per service for this.
- `bay webhook` is removed, with the root `webhook.yml` playbook. It ran without the fleet's inventory and read deploy keys from `/opt/bay/` only.
- Every alert delivery from a shell emitter writes one line to stderr, with no setting: `alert <id> <level> sent <adapter>`, `alert <id> failed <adapter> <reason>` (`http_code=<n>` or `unreachable`), or `alert <id> <level> muted`. The line lands in the journal of the unit that sent it, for example `journalctl -u bay-build@<container>`. `BAY_ALERT_FAILURE_LOG` works as before. See docs/alerting.md, "The delivery log".
- The first clone and the SSH deploy key of a `local` build app still come only from a `bay deploy <env>` with no `--tags`.

Spec 03: init, fleet init and the dead verbs

- `bay fleet init` makes the first commit, `bay: fleet init`, so the README quick start works with no manual `git commit`. When the commit fails (for example, git has no user name), the fleet stays and Bay prints the `git commit` line to run. `--from` makes no commit.
- `bay fleet init` puts `.vault_pass` in the new `.gitignore`. After `--from`, Bay warns when the clone does not ignore `.vault_pass`.
- `bay init` stops in a fleet folder, or below one, before it writes anything. The hint says to write `projects/<name>/bay.toml`.
- `bay init` looks for the `Dockerfile`, `package.json` and `pyproject.toml` at the repo root, the default build context. With `--toml-path services/api/bay.toml` it no longer warns "no Dockerfile" for a root `Dockerfile`.
- `[build] repo` is a new key in `bay.toml`. An app that lives in the fleet and builds from source names its repo there. The compile takes the build repo from `[build] repo`, then from the lock `repo`. With neither, the error names both places. An app in its own repo builds from its lock `repo`, and a different `[build] repo` there is a compile error. `bay validate` reports an in-fleet build with no repo before a deploy.
- `bay adopt` takes `[build] repo` as the lock `repo` and leaves the key out of the `bay.toml` it writes into the app repo. The compiled `build.repo` does not change.
- `bay self update --help` shows the version from `version.yml` in its example, not a fixed tag.
- Removed `bay service add`, `bay service edit` and `bay service remove`. They wrote the generated `services.yml` by hand, and the next compile refused the file. `bay service list|show|catalog|prune-webhooks` and `bay server add|remove` stay. The helpers that only the removed verbs used are gone too: `StackConfig` write methods, `catalog.resolve_dependencies` and `BayError.dependency`.

### Upgrade notes

- New status word `ahead` in `bay show`. A script that matches status words must accept it.
- New exit code 40 from `bay up` (and `bay up <env>`): the expected first-image failure. A script that treats every non-zero exit as an error still works; one that matches exit 1 for a failed deploy must accept 40 too.
- New lock field `previous.containers`, written by the next `bay up` that moves a pin. A lock written before 2.2 has none, so a plain `bay rollback` falls back to the box's `<env>.prev.json` and says so in a note, until the next pin move. The same fallback applies when a `bay up` records no map: the plan did not read the box, a box returned an error during the plan, or the bay.toml of the old pin cannot be read.
- New receipt field `failed` per container. It ships with the deploy, so it needs no manual step.
- `bay webhook` is removed. Use `bay deploy <env>` instead, or `bay up`, which now renders the receiver and the build triggers. The first `bay up` on each box with a webhook receiver after 2.2.0 rebuilds `bay-webhook:latest` (the receiver code changed) and recreates the `bay-webhook` container. The plan shows no step for the render of `rebuild.sh` and the alert scripts. The alert snippet changed, so the first `bay up` after 2.2.0 rewrites them, and it recreates `bay-webhook`. Each emitter gets the delivery line when its script renders next. `bay up` renders `rebuild.sh`, `build-alert.sh` and the log archive scripts. `bay deploy <env>` renders the backup scripts and the trigger watchdog. The disk and outbound checks need `bay --fleet <path> provision <env> --tags outbound_monitor`.
- Preview the stop of old trigger units before the first `bay up` after 2.2.0. Run `bay plan <env> --remote --log <file>` and read the task "Stop and disable the trigger units no container needs" in the log. Every unit it lists stops on the next `bay up`.
- Two fleets must not share a build box. The unit template `/etc/systemd/system/bay-build@.path` is one file for the whole box, and it names the `triggers` directory of one stack. A deploy now reads the installed template first. When it names another stack, the deploy fails with "The build triggers on this box belong to the stack at <path>" and changes nothing. If you move the builds to the new stack on purpose, check that the old stack no longer needs this box, remove that file by hand, and deploy again.
- `bay service add|edit|remove` are removed, write `bay.toml` instead. An in-fleet app that builds from source may set `[build] repo`; without it, the compile still uses the lock `repo`.

## [2.1.13] - 2026-10-07

Docs only. README and features.md checked sentence by sentence against the v2 docs and the code.

- Build strategies are `local` (default), `remote` and `registry`; `push` is a deprecated alias of `remote`. `build_image` only logs in and pulls; `git_deploy` builds. Access modes in `bay.toml` are `public`, `tailnet` and `internal`; `tailnet` compiles to `vpn`, `open` to `public_routes`, `locked` to `vpn_routes`.
- One Docker network per box, `services` (`traefik_docker_network`), shared by every env. A non-primary env carries the env in the container name, so read the injected `<NAME>_URL`. README, features.md, bay-toml.md and layout-scenarios.md say the same (the last two said each env had its own network).
- Restore is `bay backup restore <env> <accessory>`; `bay restore <env>` is the low-level playbook run. Plain `bay status` shows the version, the fleet and the feature flags; `bay status --json` adds the receipt of every box.
- The webhook receiver and its list of build containers come from `bay deploy <env>` with no `--tags` or with `--tags git_deploy`; `bay up` and `bay remove` refresh only `rebuild.sh` and `image-map.json`. `access_gateway: none` fails a deploy that has a `tailnet` app.
- README alerting shows the `alert_recipients` list; the rig role lists, the privilege phases, `make test` (three suites) and `make lint` (mypy and ruff first) match the playbooks and the Makefile.

## [2.1.12] - 2026-10-07

Docs only. Reader round 12: two places where two docs stated different facts.

- Container creation: the reconciler (`bay up`, `bay deploy`) and `rebuild.sh` create containers with `docker run`, on the build path and on the pull path. `docker compose` is not used for a service. The rendered compose file only describes the stack. README, features.md, build-pipeline.md and build-strategies.md say the same.
- Trigger files: the webhook receiver writes the trigger of a service. `rebuild.sh` consumes the trigger it processes and writes a trigger only for the aliases of an alias fan-out. A `manual-<epoch>` correlation id means `rebuild.sh` ran with no trigger file. build-pipeline.md says this in the format section and in the ownership rule.

## [2.1.11] - 2026-10-07

Docs only. Reader round 11: one place where two docs stated different facts.

- deploy-receipt.md lists four values for `fleet.source` in `bay status --json`: `--fleet`, `BAY_FLEET`, `bay.toml` and `~/.config/bay/fleets`. `bay status` never takes the fleet from the directory you stand in, so it never reports `cwd`. This agrees with install.md, plan.md and layout-scenarios.md.

## [2.1.10] - 2026-10-07

Docs only. Reader round 10: four places where two docs stated different facts.

- README "Renaming `stack_name`": containers carry the project name (`shop`, `shop-worker`), not the stack name. A rename changes the volume prefix, the stack directory, the local image tag, the Headscale user and the MagicDNS domain. The migration steps find containers by the volumes they mount.
- The stack name is the default Headscale user that owns the server nodes (`headscale_server_user`). README and tailnet-ingress.md say the same.
- `bay provision` tests the SSH connection as `ansible_user` and falls back to `root` when the host is unreachable. README, onboarding.md and layout-scenarios.md no longer ask for `-- -u root`.
- A `[resources.*.backup]` table in `bay.fleet.toml` requires `method`. Image-name detection applies only to a hand-written `backup: true` in a `services.yml` that `bay compile` does not own (features.md, backups.md).

## [2.1.9] - 2026-10-07

Docs only. Reader round 9: four places where two docs stated different facts.

- features.md says `bay.toml` and `bay.fleet.toml` are the source of truth. `bay compile` writes `services.yml`, and a hand edit makes the next compile refuse. The fleet repo lists `bay.fleet.toml` and `projects/<name>/bay.toml`.
- features.md targets one region with `bay deploy production -- --limit eu`. A deploy with the group name as the env writes `eu.json`, which `bay status` and `bay plan` do not read.
- tailnet-ingress.md: a tailnet-only port is `expose = "tailnet"` in `bay.toml` and compiles to `expose: tailnet`. `gateway` is an older spelling that the deploy still accepts and `bay import` rewrites.
- The webhook receiver image and the build triggers come from a `bay deploy <env>` with no `--tags`, or with `--tags git_deploy` (plan.md, layout-scenarios.md, build-pipeline.md agree). The reconcile step needs the env file from a `deploy_stack` run, so run `bay up` on a new box first.

## [2.1.8] - 2026-10-07

Docs only. Reader round 8: ten places where two docs stated different facts.

- Access gateway: when the fleet does not set `access_gateway`, the deploy uses `wireguard` (the role default), not `none`. `access_gateway` and `headscale_domain` go in `group_vars/all/access_gateway.yml`, the file `bay gateway`, `bay region` and `bay doctor` read.
- App account: the docs name it by `app_user` (`bay` in the example fleet). No role sets a default, so the fleet must set it.
- `git_deploy_health_check_timeout` defaults to 90 seconds in every doc. `bay.toml` has no per-service key for it.
- build-strategies.md speaks in `bay.toml` values for `update` (`notify`, `auto`, `off`) and marks `monitor` and `false` as the compiled form.
- Remote build: `:<commit12>` is always pushed, the moving tag (usually `:latest`) only when the push is not held. Trigger files: build-strategies.md describes format v2 and calls the one-line `pull` or empty file the legacy v1 form, which `rebuild.sh` still accepts.
- multi-region.md is marked historical. It uses `bay deploy production -- --limit <group>` throughout, sets domains in `[deploy.<env>]` of `bay.toml`, drops `bay service add|edit`, and says a region `secrets:` mapping replaces the production one.

## [2.1.7] - 2026-10-07

Docs only. Reader round 7: fourteen places where two docs stated different facts.

- Circuit breaker: the README says 5 consecutive failures by default (`git_deploy_cb_max_failures`). `bay build` takes the fleet from `--fleet` or `BAY_FLEET`, not from the directory you stand in.
- Prune cron: every doc names `docker system prune -af --volumes` and says it keeps named volumes (on Docker Engine 23 and later it removes only anonymous ones), so `--data keep` and `bay remove` volumes stay.
- "Full deploy" is gone: the docs say "`bay deploy <env>` with no `--tags`" for the untagged run, and "every deploy that reaches the container pass" for the `.prev.json` rotation. deploy-receipt.md names the verbs that rotate it.
- Webhook: the hook URL is `https://<webhook domain>/webhook/<container name>`. `bay deploy <env>` installs the receiver and trigger and makes the deploy key on the box; you add the key to GitHub. `bay webhook` is marked as the v1 form.
- `services.yml` is generated: install.md, layout-scenarios.md and onboarding.md no longer tell you to edit it or copy it from `example/`.
- Multi-region: a deploy aimed at a group writes `<group>.json`, which `bay status` and `bay plan` do not read. Use `bay deploy <env> -- --limit <group>`.
- Quick start: commit the new fleet before `bay init`. install.md says what `bay fleet init` writes.
- bay-toml.md: `dockerfile` and `context` are relative to the repo root, `track = "pin"` holds each push with `build.held`, and the old mounts fallback stays until a release removes it. plan.md states WANTED as two cases.

## [2.1.6] - 2026-10-07

Docs only. Reader round 6: nineteen points that confused two new readers.

- bay-toml.md: says where the file lives for an app with no repo, defines WANTED where it first appears, and says before the mounts table to write `backup = false` on every volume mount.
- Config-only push: names the signs that a `bay up` is due (`bay show` says `behind`, the `config-only push <commit12>: run bay up` log line, the new `<image>:<commit12>` tag), and says to add `bay.toml` and its mounted files to `watch`.
- plan.md: lists the three ways `bay plan` reads the box (`--no-remote`, the default, `--remote`), and says which rig roles `bay up` runs and which need a full `bay deploy`.
- Rollback: a happy path for pin mode, how far back `--to` reaches (the `docker system prune -af` cron job, weekly by default), and that `--to` also rolls config back; use `git revert` to keep newer config.
- Two environments: the compile reads only the top-level lock `commit`, so every deploy branch must hold every `[deploy.<env>]` table.
- layout-scenarios.md: the steps for a new box (provision, `bay deploy --rig`, the first full deploy), links to the `bay show` status table, and `service`/`server` take the working directory.
- tailnet-ingress.md: run `bay validate` again after `bay route add`. Scenario 15 keeps the once-per-fleet setup apart from the two ACL edits each route needs.
- README: puts the `bay.toml` or `bay.fleet.toml` form beside each compiled `services.yml` example.

## [2.1.5] - 2026-10-07

### Fixed

- Test only, no behaviour change: `test_adopt_prints_plan_hint` now collapses whitespace in the
  captured output, so it passes at any console width (rich wraps the `push it after bay up` hint
  across lines at some widths).

### Upgrade notes

No change on any box or fleet.

## [2.1.4] - 2026-10-07

### Changed

- Docs: fourth reader round. Every key that the compiler validates but does not deploy is tagged
  `(validated, not deployed yet)` in `docs/bay-toml.md`, and the list in `docs/plan.md` follows the
  compiler (a volume mount with the default `backup = true` is on it, and so is a need of a
  resource on another box). The box name, the inventory host name and the `[<box env>:children]
  group` that the box env needs are defined once, and the sample hosts files are fixed. One
  procedure for adding a tailnet route, with the env name, the deploy of the prerequisites, the
  ACL commit and the Headscale host. The rules for rollback (which record gives the config and
  which gives the code, the approval path, the half rolled-back pin mode), for the config-only
  push (it tags the previous image), for the first `bay up` of an app with no image, for adopt with
  several deploy envs, for the first plan of a new env, for what a plain `bay plan` cannot see, and
  for the risk of a box-predicted step are stated from the code.

### Fixed

- `bay up --json` now carries the adopt note in `notes`. When the commit is the `bay adopt`
  commit, the sentence "no code moves, git push it after this bay up" went to the log only.

### Upgrade notes

- Docs only plus one note fix; no box change. `bay self update` on other machines.

## [2.1.3] - 2026-10-07

### Fixed

- `bay up` no longer stops in its pre-deploy validation for a fleet that moved a mounted
  file beside its `bay.toml` (for example `projects/gatus/config.yaml`, as 2.1 documents).
  The "Config Files" check read only the deprecated `files/<name>/<from>` and failed with
  "has no file at files/gatus/config.yaml", while `bay plan` and the deploy accepted the
  file. The check now looks where the compile and the deploy look: the files root of the
  `bay up` compile, then `projects/<name>/<from>`, then `files/<name>/<from>`. The error
  names every place it looked. `bay validate` uses the last two.

### Upgrade notes

- A fleet that moved a mounted file beside its bay.toml can run `bay up` again.

## [2.1.2] - 2026-10-07

### Fixed

- A `bay.toml`-only push (for example the `bay adopt` commit) is now config only also
  for a container built before 2.1.0. Such a container has no `com.bay.commit` label and
  its image has only `:latest`, so `rebuild.sh` found no previous commit, built the app
  and recreated the container. Now the previous commit falls back to the commit that the
  box's checkout was at before the pull or fetch. The previous image falls back to
  `<image>:latest` when it holds that commit: its commit or revision label names it, or
  (a local build with no label) the circuit breaker shows no failure, the failed-commits
  record does not list it, and the running container runs that image. `:latest` then
  also gets the previous commit's tag. When no such image is found, the push builds as
  before. See `docs/build-pipeline.md`, "Config-only push".

### Upgrade notes

- Deploy once (`bay up`) so the new rebuild.sh reaches the boxes before the first
  config-only push.

## [2.1.1] - 2026-10-07

### Changed

- New `docs/layout-scenarios.md`: fifteen scenarios of where Bay files live, with a
  sample `bay plan` output, a "Bay does not do" list and "What never goes where". It is
  linked from the docs index. Features that are not built stay marked `(planned)`.
- Every current doc that names `bay up` now says it deploys the whole box environment.
- `--data move` and `bay init --secrets` carry `(planned)` wherever the docs name them.
- Docs from the Bay 1 consumer model (M116/06): `design-decisions.md` and the two
  external-tailscale docs carry `Status: historical`. `services.md` and
  `rollout-playbook.md` use `bay --fleet <path> <verb>`, name no consumer clone and say
  `bay self update` instead of a pin bump. `services.md` says `services.yml` is compiled.
- `docs/install.md` has a section "Development mode: the editable install" (M116/08).
- `docs/plan.md` no longer says a code pin is future work, or that a new box in
  `bay.toml` gives only a note.
- Fixes from two blind-reader reports of the 2.1 docs. One install path (`bootstrap.sh`)
  in `install.md`, the README and scenario 14. The vault password file, the rule for the three
  fleet names and one ordered fleet pick list (the fleet directory you stand in included) are
  in `install.md`. The README body describes the v2 model. `plan.md` now covers the `--remote`
  default and its effect on the plan id, the dirty check, the full flag list, how to create a
  project with no repo, the first image, which projects `bay up` pins, one `rollback --to`
  rule, undoing a bad push in branch mode, pin-mode rollback, `frozen_commit`, and
  what `<env>` means in `bay up --plan-id` for a removal. It also lists the features the
  compiler cannot deploy yet. `layout-scenarios.md` defines deploy env, box env and group,
  says that `access.mode` has no default and that a resource maps to boxes by `kind` and `box`, and
  has an ordered "second env on a new box" list. `bay-toml.md` states the container name rule
  and the `from` fallback. `build-pipeline.md` gives the order of the push guards.
  `tailnet-ingress.md` is 2.1 first: routes in `[tailnet.routes.*]`, `bay route add` never edits the ACL,
  one prerequisites list, one list of the two ACL edits, the upstream host form and the
  `allowlist` key. `deploy-receipt.md` takes the WANTED definition of `plan.md`.

### Fixed

- **A file moved beside the toml no longer blocks the plan of every other project.** A
  project in the fleet is read at its pin, but the deploy reads config files from the
  fleet HEAD. After `git mv files/<name>/<from> projects/<name>/<from>`, the pin still
  held the old place, so every `bay plan` was `blocked` with `from = '<from>' cannot be
  listed` while `bay plan --project <name>` was fine. A `from` of a project in the fleet
  is now read first beside the toml at the fleet HEAD, then beside the toml at the pin,
  then at the old place `files/<name>/<from>`. The box path does not change.
- **A plan saved before the lock migration no longer goes stale.** `bay up --plan-id`
  moves the locks into project folders first, and that fleet commit made the recheck
  report `stale` (exit 30, reason `fleet`). When the commits between the saved plan
  and HEAD only rename `projects/<name>.lock` to `projects/<name>/bay.lock` and change
  the `format` line of `bay.fleet.toml`, the plan stays fresh and says
  `fleet commit moved by the layout migration only`. Any other fleet change still
  makes it stale.
- **`bay route import --json`.** The verb now takes `--json` like the other route
  verbs and prints one document: `fleet_commit`, `routes` (name, domain, upstream),
  `deleted` (the old file) and `cert_domain`.
- **`bay remove` takes `--remote` and `--no-remote` like `bay plan`.** It always saved
  `box_checked: false` and an empty `box_prediction`, and had no flag to change that.
  The default is the one of `bay plan`: the receipt is read, the box is not asked.
  With `--remote`, Bay runs the check mode of the deploy on each box against the compile
  without the project. Each remove step takes the box's prediction into its reason
  (`source: box`), and a step the box does not predict is named in a note. A saved
  plan keeps its box check when it is checked again.
- **The remove step reason has the right verb.** It read `volume <name> stay`. It now reads
  `volume <name> stays`, `volumes <a>, <b> stay`, and the same for databases.

### Upgrade notes

- The lock migration no longer makes a saved plan stale; a plan saved before the first 2.1 writing run can be applied with `bay up --plan-id`.
- `bay remove` takes `--remote` like `bay plan`.
- After moving a mounted file beside its `bay.toml`, no other project's plan is blocked any more; the moved file is read from the fleet HEAD.

## [2.1.0] - 2026-10-07

### Changed

- **Lock version 2, one folder per project.** A project's lock moves from
  `projects/<name>.lock` to `projects/<name>/bay.lock`. The lock holds `repo`,
  `toml_path`, `commit` and `envs`. It no longer holds `local_path`: a lock never
  names a path on a machine. A version 1 lock is read as version 2, and the next
  write stores version 2.
- **Bay finds an app repo by its URL.** It reads the git checkout you stand in when
  its `origin` is the lock's `repo`. Otherwise it reads a mirror clone in
  `<fleet>/.bay-cache/repos/<slug>`, which it clones on first use and fetches before
  each plan. Two projects in one repo share one cache.
- In the repo cache, WANTED is the head of the branch `[deploy.<env>].branch`
  names, not the mirror's HEAD (the remote default branch). With no branch
  declared it stays HEAD. `bay show` uses the branch when every environment
  names the same one.
- **A missing commit is an error.** A pinned commit that is in neither the checkout
  nor the cache stops the plan and names the project. Before, a lock with a repo and
  no local path could drop out of the compile without a word.
- **`from =` is relative to the directory of the `bay.toml`**, in an app repo and in
  the fleet. A project that lives in the fleet keeps its mounted files beside its
  toml: `projects/<name>/config.yaml`. The box path stays `config/<name>/<from>`, so
  no container is recreated.
- An in-fleet project's WANTED commit no longer counts `projects/<name>/bay.lock`.
  So the lock commit that `bay up` makes does not make the project look changed.
- `bay import` writes the new layout: `format = 2`, the lock in the project folder,
  and the files a project owns beside its `bay.toml`.
- **A step of another project keeps its own risk.** In a plan for project A, a
  change to project B's container carries `project: B` and B's own risk class. A
  safe change stays safe, so two safe changes give the verdict `auto`. Before,
  every such step was `shared`. `shared` now means a fleet-wide thing only: a
  `[resources.*]` entry, the webhook, the tailnet allowlist, or a container that no
  project owns.
- `bay plan` prints a `PROJECT` column in its step table.
- A lock's `previous` records the `plan_id` that deployed the replaced pin.

### Added

- **`bay remove <project> [--env <env>]`.** It plans taking a project, or one
  environment of it, out of the fleet: one `remove` step per container, risk
  `destructive`, so the verdict is `approve`. The plan record has a `remove`
  block with the containers per environment, every volume by its name on the
  box, and the database and its role. `bay approve`, then
  `bay up <env> --plan-id <id>` applies it: compile without the project,
  commit, deploy, read the receipt. Only a receipt that confirms the containers
  are gone deletes the environment from the lock; with none left,
  `projects/<name>/` leaves the fleet (`git rm`). Otherwise the lock keeps the
  environment with `result: failed` and the command exits 1. Bay never deletes
  data: it prints the `docker volume rm` and `DROP DATABASE ...; DROP ROLE ...;`
  lines to run by hand. `--env` is blocked while `bay.toml` still has
  `[deploy.<env>]`. A remove is blocked when another project needs the project,
  or when the compile would change anything else. A `[resources.*]` entry and
  the containers Bay runs on every box are refused. See `docs/plan.md`,
  "bay remove".
- **`bay doctor` answers the pre-deploy questions.** New lines: the fleet Bay
  picked and why, with its real path; the fleet `format`; the CLI version and
  install path; whether the vault opens; whether each box answers; whether each
  app repo's pinned commit can be read; whether the fleet clone is behind its
  remote; plan files that are not committed. Bay 1 leftovers stay a warning.
  Each line is `ok`, `warn` or `fail`; a `fail` exits 1. `--json` prints the
  lines, `--no-remote` skips the boxes and the fetch. The environment argument
  now defaults to the fleet's primary environment. See `docs/install.md`.
- **`bay adopt <name>`.** Run it in a checkout of the app repo, with the fleet
  named by `--fleet`, `BAY_FLEET` or `BAY_FLEET_NAME`. It moves an in-fleet
  project's `bay.toml` and its files (including a mount still read from the old
  place `files/<name>/<from>`) into the repo, at `--toml-path` (default
  `bay.toml`). It makes one local app commit,
  `chore: add bay.toml (adopted from fleet <fleet>)`. The lock
  takes the repo form (`repo`, `toml_path`, `commit`) with
  `adopted.from_fleet_commit` and `adopted.app_commit` per environment. Every
  other adopted name stays. The fleet loses the folder contents except the
  lock, in one commit, `bay: adopt <name> into <repo>`. Nothing is pushed. The
  order is adopt, `bay up`, then `git push`: `bay up` accepts the unpushed
  adopt commit and moves no code for it, so the box gets the new `rebuild.sh`
  first, and the push is then config only. Pushed first, the old script would
  build and recreate the app. The compiled output stays
  the same, so the next `bay plan` shows 0 steps. `--check` prints the files and
  the lock diff and changes nothing. Bay refuses a dirty app repo or project
  folder, an existing `bay.toml`, a name mismatch, a lock that names another
  repo, and a project whose fleet folder changed after its last `bay up`. See
  `docs/plan.md`, "bay adopt".
- `bay rollback` straight after `bay adopt` is refused: "the previous pin is a
  fleet commit; use `bay up --at <commit>`".
- **Config-only push.** A push that changes only the `bay.toml` and the files
  its mounts read builds nothing and deploys nothing. `rebuild.sh` logs
  `config-only push <commit12>: run bay up` and ends the run: no image build, no
  `:latest` move, no alert, no circuit-breaker change. The previous commit's
  image gets the new commit tag. `bay compile` writes `build.bay_toml_files`
  (the mounted paths, relative to the repo root) next to `build.bay_toml_hash`.
  The same rule runs on the build server for remote builds. So the adopt commit,
  pushed after its `bay up`, is a no-op on the box, and "edit bay.toml, push,
  bay up" is the clean flow.
  See `docs/build-pipeline.md`, "Config-only push".
- `bay plan` shows no step when a container entry differs only in
  `build.bay_toml_hash`, `build.bay_toml_path` or `build.bay_toml_files`. The
  container hash leaves the `build` table out, so nothing is recreated; `bay up`
  still writes the keys to the box. A build project gains these keys when
  `bay adopt` moves its `bay.toml` into the app repo.
- **Box move step.** When `deploy.<env>.box` in `bay.toml` (or the fleet default)
  differs from the box in the lock, the plan has a step `move`. Its risk is
  `destructive` when the project has a named volume or a database, `shared`
  otherwise. The plan also lists a `remove` on the old box and a `create` on the
  new box per container, and a `moves` record. A destructive move is `blocked`
  until `--data keep`: the new box starts empty, the data stays untouched on the
  old box, and the notes give the `docker volume rm` and `DROP DATABASE` lines for
  later. `--data move` is refused as deferred. `bay up` writes the new box into the
  lock. The compiler still uses the lock's box, so an edit alone moves nothing.
- **Whole-environment plan.** `bay plan <env>` with no `--project`, run in a fleet
  directory or with `--fleet`, `BAY_FLEET` or `BAY_FLEET_NAME` and no `bay.toml`
  here, plans every project that has `[deploy.<env>]`, each at its WANTED commit.
  The record has `project: null` and a `projects` list. `bay up <env>` there
  applies it; `bay up --plan-id <id>` takes such a plan. In an app repo, `bay plan`
  keeps its one-project meaning.
- **`plans/` prune.** After its receipt commit, `bay up` keeps the 50 newest plan
  records plus every record a lock names (`plan_id`, `previous.plan_id`) and
  removes the rest with `git rm` in one commit, `bay: prune plans (<n> files)`. An
  approval file goes with its record. An untracked plan file is never touched.
- **Fleet line.** Every verb that writes to a fleet repo or acts on a box prints
  `fleet: <name> (<path>)` as its first stderr line, also with `--json`. Readers
  (`show`, `status`, `toml validate`, `self version`, `fleet ls` and others) do
  not. A test walks the CLI, so a new verb must be sorted into one of the two lists.

- `from = "fleet:<path>"` mounts the shared fleet file `files/<path>`.
- `bay init --toml-path services/api/bay.toml` for a repo with several apps. The
  lock records it as `toml_path`.
- `format = 2` in `bay.fleet.toml`. A CLI refuses a fleet whose format is newer
  than it knows.
- `bay up` refuses a repo project's commit that is on no branch of the remote:
  "push first". `bay plan` says so in a note.
- `bay up` and the box check of `bay plan --remote` deploy config files from the
  plan's scratch copy of the fleet at its commit (`bay_config_files_root`), with each
  file a `bay.toml` mounts from beside it mapped to `files/<target>`. So the deploy
  ships what the plan compiled. An uncommitted file is not deployed; the plan names
  it in a note. A plain `bay deploy` still copies from the fleet's `files/`.
- `bay doctor` warns when the fleet still has the Bay 1 leftovers `bin/`, `.bay/`
  or `.bay-version`.
- `[deploy.<env>] track = "branch" | "pin"` in `bay.toml`. `branch` (the
  default) lets a push deploy new code under the pinned config. `pin` makes a
  push only build; `bay up` deploys. `pin` needs the `bay.toml` in the app repo.
- Every build is tagged `<image>:<commit12>` and labelled `com.bay.commit`.
  `:latest` moves only when the push may deploy.
- Hold guard. `bay compile` writes `build.bay_toml_hash` (the SHA-256 of the
  parsed `bay.toml` as sorted JSON) for a project whose `bay.toml` lives in the
  app repo. When a push changes that hash, the build is held: the image keeps
  its commit tag, the container keeps running, the circuit breaker is not
  touched, and the new alert `build.held` (warn) says "config changed, run bay
  up". A comment edit does not hold. `track = "pin"` and a frozen environment
  hold every push. So `build.held` fires on every push of a `track = "pin"`
  project, by design. `bay up` releases a hold.
- `bay compile` also writes `build.bay_build_hash`: the canonical hash of the
  `[build]` keys (top level, `[deploy.<env>]` and `[services.<name>]`), from
  `python -m bay_reconcile.tomlhash --section build`. A push is config-only only
  when this hash matches: a `[build]` edit (a new Dockerfile, a build arg) always
  builds.
- The receipt names the `commit` and the `image` of every container, plus
  `image_ref` (the reference the deploy asked for). A webhook build stamps the
  new commit into the receipt. Additive: `receipt_version` 1, `status_version` 2.
- `bay plan` prints `code at <commit>, config pinned at <commit>` in branch
  mode. In pin mode, code that is not the pin is a step of kind `image`, risk
  safe.
- In branch mode `bay up` applies config at the pin and moves code only
  forward. When the box runs a commit newer than the pin, `:latest` stays and
  only the config changes (plan field `code.keep`). So a `bay up` from a stale
  checkout never moves the running code to an older image. The
  whole-environment plan (`bay plan <env>`, `bay up <env>`) runs the same check
  per project and carries the union in `code.keep`. When Bay cannot
  order the two commits, `bay up` refuses: "cannot order <pin> and <running>;
  fetch the repo or pass --force-code". `--force-code` (on `bay plan` and
  `bay up`) moves the code anyway, as a destructive `image` step. Pin mode and
  `bay rollback` still move code backwards.
- `bay rollback` restores code as well as config: the box points `:latest` at
  the image of the previous receipt. A code move the box skipped is reported:
  `code_kept` in the JSON and `code: kept <container> (<reason>)` in the output
  (from the new receipt field `code_moves`). A previous receipt from before 2.1
  names no commit, so the first rollback after the upgrade rolls back config
  only and says so. It also freezes the environment
  (`frozen = true` in the lock): a push builds but does not deploy until a
  `bay up` to a newer commit. `bay rollback --to <commit>` rolls back to a
  commit whose image is on the box, and lists the commit tags when it is not.
- **Tailnet routes are a table in `bay.fleet.toml`.** `[tailnet] ingress_box` and
  `cert_domain`, and one `[tailnet.routes.<name>]` per route: `domain`, `upstream`,
  `host` (`client` or `upstream`), `identity`, `aliases`, `entrypoint`. `bay compile`
  writes them as `tailnet_proxies:` into `services.yml`, in their order and with
  defaults left out, so the box renders the same bytes as before. The compile refuses
  a domain outside `cert_domain`, a domain used twice on the fleet, an upstream that
  is not a tailnet name or address with a port, and routes in both
  `[tailnet.routes]` and a hand file. `bay validate` warns when no ACL rule has the
  ingress box as the only `src` for a route's upstream port. See
  `docs/tailnet-ingress.md`, "Routes in bay.fleet.toml (2.1)".
- `bay route add`, `bay route ls`, `bay route rm` and `bay route import`. They edit
  `bay.fleet.toml` with a small text edit (comments stay), refuse what the compile
  would refuse, and commit the fleet repo (`--no-commit` only edits).
- A route change is a plan step of kind `route` (`route_added`, `route_changed`,
  `route_removed`) at risk `shared`. A domain change says "Headscale restarts". `bay
  up` then runs the tags `deploy_stack,headscale,traefik`. A plan for another box env
  than the ingress box's is blocked while a route change is pending.
- `bay show --routes`: WANTED, PINNED and RUNNING per route. RUNNING needs a receipt
  that lists routes; until the receipt writes them, it says `unknown`.
- `bay import --out` converts `tailnet_proxies` into `[tailnet.routes]` when one
  group sets `tailnet_ingress_cert_domain`.

### Fixed

- A webhook build now keeps the `com.bay.config-hash` label of the container it
  replaces, so the next `bay up` no longer recreates it for `no config-hash
  label` (M116/03).
- `bay up` of several boxes (a box move) stops at the first failed deploy. The
  old box is never deployed after the new one failed, so its container stays.
- After a failed health check, `rebuild.sh` removes the failed `<image>:<commit12>`
  tag and records the commit in `/var/lib/bay/failed-commits/<svc>`. `bay up` and
  the code pin never promote that commit again ("failed its health check on this
  box, so it is never deployed again; push a fix"). A later healthy build of the commit clears the record.
- `_promote_latest` keeps `:previous` when `:latest` already is the candidate image
  (a rebuild of the running commit), so the health-check rollback keeps the last
  good image. The pull path does the same.
- On a build server, the previous commit of a config-only check falls back to the
  checkout's last build only when `<image>:<commit12>` is in the registry.
- The box-move notes name volumes with the stack prefix (`<stack_name>_<volume>`),
  the name Docker uses on the box.
- A tailnet route upstream is never `localhost` or a loopback address.
- `plans/` prune refuses, with a warning, when a lock cannot be read.
- `:previous` rotates at promote time, before the health check: it is the image
  that ran before the push.
- `bay compile --working-tree` reads only the checkout you stand in. Another repo
  project is not read, not from the repo cache either, and the compile names it.
- `bay status` does not show routes yet. `bay show --routes` prints RUNNING as
  `unknown` until a receipt lists routes.

### Upgrade notes

- **Move the tailnet routes once per fleet** that has
  `group_vars/all/tailnet_proxies.yml`: run `bay route import` (it reads the file,
  writes the routes into `bay.fleet.toml` with their names, deletes the file and makes
  one fleet commit), then `bay plan <env>` for a project on the ingress box's env,
  `bay approve`, and `bay up <env>`. The first `up` shows one `route_added` step per
  route; the route file and the split-DNS records on the box keep their bytes, so no
  container is recreated. Until you move them, the old file keeps working; a fleet
  with routes in both places does not compile.
- `bay validate` may warn about the ACL of each route's upstream port. A warning does
  not stop a deploy.

- **The first 2.1 writing run moves the locks.** The first `bay up`, `bay rollback`,
  `bay compile`, `bay adopt` or `bay init` on a fleet moves each
  `projects/<name>.lock` to `projects/<name>/bay.lock` with `git mv`, adds
  `format = 2` after the `name` line of `bay.fleet.toml`, and makes one fleet commit:
  `bay: move locks into project folders`. It refuses on a fleet that is behind its
  remote: pull the fleet first. `bay show` and `bay plan` never move them: they read
  either form and print `layout: migration to project folders pending (bay up or bay
  compile does it)`. Commit any edit to
  `bay.fleet.toml` first: Bay refuses to run while it has uncommitted changes. Bay
  also refuses when one project has a lock in both places.
- A 2.0 CLI refuses a format 2 fleet (`format: unknown key`), so update every
  machine that works on the fleet with `bay self update`.
- Both places work in 2.1: beside the `bay.toml` and the old
  `files/<name>/<from>`. The old place prints a note that names each file. Move each
  file with `git mv`, for example
  `git mv files/gatus/config.yaml projects/gatus/config.yaml`. The box path does not
  change, so no container is recreated.
- `bay up` no longer deploys an uncommitted config file. Commit a file before you
  run `bay up`; the plan names each uncommitted one in a note.
- `bay init` now refuses a repo with no `origin` remote.
- Remove the Bay 1 leftovers `bin/`, `.bay/` and `.bay-version` from each fleet
  and drop the shell alias `bay='bin/bay'`. `bay doctor` lists what is left.
- Deploy the boxes once (`bay up`, or `bay deploy <env> --tags deploy_stack`)
  so `rebuild.sh`, the reconciler package and the group-writable receipts
  directory (`/var/lib/bay/receipts`, group `docker`, mode `0775`) reach them.
  Also run `bay deploy <env> --tags git_deploy` on build servers.
- The hold guard hashes `bay.toml` with Python's `tomllib` (3.11+) or `tomli`.
  A box or build server with neither holds every config-checked push. Check
  with `python3 -c 'import tomllib'`.
- Images built before this release have no commit tag. `bay rollback --to`
  works only for commits built after the upgrade.
- A config-only push needs `build.bay_build_hash` on the box. Until the next
  `bay up` writes it, every push of the project builds as before. The same deploy
  creates `/var/lib/bay/failed-commits` (group `docker`, mode `0775`).
- The config-only rule lives in `rebuild.sh`. Run `bay up` for an adopted
  project before you push its adopt commit (plus `bay deploy <env> --tags
  git_deploy` on build servers). A box with the old script builds and deploys
  that push as a normal one.

## [2.0.2] - 2026-10-07

### Fixed

- `bay up --plan-id` no longer calls a plan stale when the plan was made with
  `--remote`. The re-check now runs the same check on the box, so the two plans
  match. Before, the re-check skipped the box check, and a plan that you had
  approved came back stale.
- The stale reason now names the plan field that changed. It used to print `?`
  when the change was outside the five fields it knew.
- Plan `notes` are no longer part of the plan hash. They are text for the
  reader, such as a hint to pass `--remote`, and they never change a deploy.

### Upgrade notes

- Plans saved by an earlier version have a different hash. Run `bay plan` again
  for any plan that you have not applied yet, and approve the new plan id.

## [2.0.1] - 2026-10-07

### Fixed

- `bay up` now re-renders `rebuild.sh`. It kept a frozen copy of each
  container's labels, ports and mounts from before the `up`, so the next webhook
  build recreated the container with the old values. The `git_deploy` role now
  renders the script under the `deploy_stack` tag too, as it already did for
  `image-map.json`. `bay up` and `bay plan` still run `deploy_stack` only, so
  they do the same work, and nothing clones, builds or pulls.
- The plan no longer shows a false `volumes` reason when the box reports a
  mount with Docker's default `:rw` suffix. A missing mode now equals `rw`,
  and the order of mode flags does not matter.
- `bay compile` writes a loopback resource port as `127.0.0.1:5432:5432`, not
  `5432:5432`. The deploy role always bound loopback, so the container spec
  does not change. A `tailnet` resource port keeps `5432:5432` and its
  `expose` line.

### Upgrade notes

- The first `bay up` after this release rewrites `rebuild.sh` on each box that
  has webhook builds. Nothing else changes on the box.
- The next compile changes the `port` line of each loopback resource in
  `group_vars/all/services.yml`. Commit it. The containers do not recreate.

## [2.0.0] - 2026-10-07

Bay 2.0 is a machine-level tool. You install it once per machine, a fleet
repo holds your boxes, and each app repo holds a `bay.toml`. The consumer
repo, the `.bay/` clone and the `bin/bay` wrapper are gone.

### Added

- `bay self update [--to <tag>]` and `bay self version`. The first moves this
  machine to the newest release or to one tag. It fetches the tags, checks
  out the tag, syncs the dependencies, installs the command again and prints
  the old and the new version. It refuses to run when the checkout has edits.
- `bay fleet init <name> [--from <url>]` and `bay fleet ls`. They make or
  clone a fleet at `~/.config/bay/fleets/<name>` and list the fleets on this
  machine.
- `docs/install.md`: the one install path. `bootstrap.sh` is now the
  idempotent installer. It clones or updates the checkout, syncs the
  dependencies and installs the `bay` command.
- `--fleet <path>`, `BAY_FLEET` and `BAY_FLEET_NAME` pick the fleet, as does
  the `fleet = "<name>"` line of the `bay.toml` in the app repo you are in.
- `boxes.<b>.webhook_domain` in `bay.fleet.toml`. It gives one box its own
  deploy webhook domain. `[webhook] domain` stays the default. `bay import`
  writes it when the webhook domain differs per box.
- `bay deploy`, `bay provision` and `bay restore` run from any directory.
  They read the fleet from `--fleet` and the playbooks from the checkout.

### Changed

- `bay status --json` is `status_version` 2. `framework` holds `version`
  and `path`. `fleet` holds `root`, `source` (how Bay found the fleet),
  `commit` and `dirty`. The human output shows the same facts.
  `docs/deploy-receipt.md` and `status.schema.json` follow.
- With no fleet selected, a command stops and lists the three ways to pick
  one.
- The deploy version gate tells you to run `bay self update`.
- `memory = "X"` in `bay.toml` sets `mem_limit` and `memswap_limit` to X,
  so a container never uses swap. The compile now writes both keys, for a
  project, a service and a `[resources.*]` entry. `bay import` carries a
  `mem_limit` with no `memswap_limit`, or with one equal to it, and flags a
  `memswap_limit` that differs, because `bay.toml` cannot allow swap.
- The webhook receiver lists its build services in name order, so its
  `SERVICE_BRANCHES` value no longer depends on the order of `services.yml`.
- `bay plan --remote` names a memory difference with the reason `memory`
  (`mem_limit` or `memswap_limit` against what the container runs with). It
  no longer reports `env_order`: the old env file is not on the box, so the
  cause of a bare hash change is not observable and the reason stays
  `config_hash`. The `env_order_recreates` plan field is gone.
  `bay import --check` still names env files that only changed in line order.
- `bay plan --remote` now predicts env file changes. The check-mode deploy
  renders every env file into a scratch directory on the box, never over the
  live files, and hashes those. A container whose env file bytes change is a
  `recreate` with the reason `env_file`, which names the added, removed and
  changed keys, or says "same variables, different order". Before, the plan
  hashed the old env files and said `noop`, so a deploy could recreate more
  containers than the plan listed. A new service with env no longer fails the
  check for a missing env file.
- The reconciler's per-box reports go to a fresh temporary directory outside
  every working tree, for `bay up`, `bay deploy` and `bay plan --remote`, and
  are removed afterwards. Before, a deploy wrote `.reconcile-report/` into the
  framework checkout. `.reconcile-report/` is gitignored for a playbook run by
  hand.
- `bay up` pins every project it deployed. The deploy covers the whole box
  environment, so Bay now updates the lock of every project with a
  `[deploy.<env>]` on that box environment: a project in the fleet gets the
  commit the compile read, a pinned repo project keeps its commit, and each
  gets `result`, `deployed_at`, `plan_id`, `last_receipt_sha256` and
  `previous`. An unpinned repo project is left out and named in `notes`. One
  commit: `bay: receipt <box env> (<n> projects)`. `bay show` reports `ok`
  for all of them, not `unknown` for all but one.
- `bay up --json` reports what the deploy did: `applied` lists, per box,
  every container whose receipt action is not `noop` (`box`, `container`,
  `action`, `healthy`). `steps` stays the plan. A receipt from an earlier
  deploy is left out, with a note. The human output prints both counts.

### Removed

- The `bin/bay` wrapper and `scripts/bin-bay-wrapper.sh`. Use `bay`.
- The `.bay/` clone model: `bay setup`, `bay install`, `bay update`,
  `.bay-version`, `read_pinned_version` and the `group_vars` link into
  `.bay/`. Use `bay self update`, and `bay fleet init` for a new fleet.
- `bay dev-link`, `bay dev-unlink`, the `.bay-dev` sentinel file and its
  banner. Use `bay --fleet <path>` to try changes without a release, and
  `bay self update --to <tag>` to move between releases.
- `bay guide` and the interactive setup wizard. Use `bay init` in an app
  repo, `bay import` to adopt an existing fleet, and `docs/onboarding.md`.
- The `bay.mk` Makefile aliases and the `make bay:*` targets, and the
  `example/` consumer files (`Makefile`, `deploy.yml`, `provision.yml`,
  `restore.yml`, `webhook.yml`, `ansible.cfg`, `tests/test_infra.sh`). Run
  the `bay` commands directly.
- The legacy layer: the `argo` command alias, the `.argo` fallback paths,
  the `ARGO_*` variables and the old-layout warning. The box-side old names
  that tag themselves `kept-argo` (the `argo-admin` account, `argo-builder`,
  the `argo-` image tag and the migration role) stay.
- Discovery by walking up from the working directory (`find_bay_dir`,
  `consumer_root`). A fleet is picked as `docs/install.md` says.

### Upgrade notes

- Install once per machine, then point Bay at your fleet. See
  `docs/install.md`. Existing fleet repos keep their `hosts/`,
  `group_vars/` and `bay.fleet.toml`. Move or clone the repo to
  `~/.config/bay/fleets/<name>`, or pass `--fleet <path>`.
- Delete the `.bay/` clone, `.bay-version`, `bin/bay` and the `Makefile`
  includes from old consumer repos. Nothing on a box depends on them.
- CI jobs that ran `bin/bay ...` run `bay --fleet <path> ...` after the
  install step.
- A script that reads `bay status --json` must handle `status_version` 2:
  `framework.pinned`, `framework.checkout` and `framework.latest` are gone.
- `bay test` still runs `tests/test_infra.sh` from the fleet.
- The webhook receiver recreates once at the first deploy, because its
  `SERVICE_BRANCHES` value is now sorted by service name and its config hash
  changes.
- A container that has `mem_limit` and no `memswap_limit` gains the swap cap
  at its first deploy from a compiled file, so it recreates once.
- All three operator fleets were cut over on 2026-10-06 and 2026-10-07.
  Each cut-over used `bay import`, `bay compile --adopt` and `bay up`.
- The first `bay up` on a cut-over fleet recreates some containers. This
  happens when the env file order, the swap cap or the webhook env changed.
  The plan now predicts these recreations, so you can read them first.
- Install the CLI once per machine. See `docs/install.md`. The `bin/bay`
  wrapper is gone.

## [1.0.0] - 2026-10-06

### Changed

- This is the last release of the `services.yml` era: the consumer repo
  holds a clone of Bay in `.bay/`, you edit `group_vars/all/services.yml`
  by hand, and you run `bin/bay`. There is no functional change versus
  0.10.0.
- Fixes to this model ship as 1.0.x from the `v1` branch.
- Bay v2 replaces the consumer repo with a machine-level CLI, a fleet repo
  that only the CLI writes, and a `bay.toml` file in each project. v2 is
  developed on the `v2` branch.

### Upgrade notes

- You can pin v1.0.0 safely. There is nothing to do.

## [0.10.0] - 2026-10-05

### Added

- `memswap_limit` per service and accessory, next to `mem_limit`. Set it
  equal to `mem_limit` and Docker gives that container no swap, so its
  memory pages never reach disk. The host can keep its swap file for
  everything else. `bay validate` rejects `memswap_limit` without
  `mem_limit`, and a `memswap_limit` smaller than `mem_limit`. A service
  that does not set the key gets the same config hash as before, so
  upgrading recreates no container.

## [0.9.2] - 2026-10-03

### Fixed

- The revision check no longer raises a false alert for an image built
  before the revision label existed. The build server skips the build when
  the `:<sha>` tag is already in the registry, so the first pull after an
  upgrade can announce such an image. When the running image has no label,
  the check now compares the container's image ID with the image the pull
  fetched, and passes when they match. A stale container still fails,
  because its image ID differs.

## [0.9.1] - 2026-10-03

### Fixed

- `git_deploy` now brings the webhook receiver onto its current image. Before,
  it rebuilt `bay-webhook:latest` when `app.py` changed, but the running
  container kept the old image: the `Restart bay-webhook` handler only
  restarts, and only `--tags deploy_stack` recreated it. After a release that
  changed the receiver, `bin/bay deploy <env> --tags git_deploy` left every
  host on a stale receiver that silently dropped new pull-signal fields.
  Every `git_deploy` run now reconciles the receiver alone, through the same
  spec and reconciler as `deploy_stack`. The reconciler compares the running
  container's image ID with the local `bay-webhook:latest` on every run, so
  existing drift heals too, and a matching container is left alone (no
  restart). Hosts that have no receiver yet are skipped; `deploy_stack`
  creates it. The image is also rebuilt when it is missing and the files are
  unchanged. `--check` plans the change and writes nothing.
- The `Restart bay-webhook` handler is now `docker restart bay-webhook`. It no
  longer fails on a host with no receiver, and it is skipped when the
  receiver was just recreated in the same run.

**Upgrade notes**

- Run `bin/bay deploy <env> --tags git_deploy` once per host after updating.
  It now also brings a receiver left stale by earlier releases up to date. A
  receiver that is already current is not touched.

## [0.9.0] - 2026-10-03

### Fixed

- Traefik: the tailnet entrypoint (`websecure_tailnet`) now has a
  `readTimeout` of `600s`, set by the new `traefik_tailnet_read_timeout`
  role default. Traefik v3's 60 s default cut registry blob PUTs from the
  build host, which reach Zot through this entrypoint. The public
  entrypoint is unchanged. Set the variable to `""` to keep the Traefik
  default.
- `rebuild.sh` consumes the build trigger at the start of a run (moved to
  `<service>.trigger.running`) instead of `bay-build@.service` deleting it
  afterwards. A trigger written while a build runs is no longer lost: it
  fires the path unit again once the run ends. `ExecStartPost` is replaced by
  an `ExecStopPost` that removes only the `.running` leftover, and
  `_record_failure` and `bay-build-alert` no longer delete the live trigger.
  The trigger file may now carry two optional extra lines (revision,
  `built_at`); old triggers still work.
- Registry push failures: `rebuild.sh` retries a build that failed with a
  transient registry error (`blob upload invalid`, `unexpected EOF`, 499,
  502, 503, 504, `i/o timeout`, `connection reset`) up to two more times on
  the same builder, after 15 s and 45 s. Compile errors are not retried.
- Failure alerts are suppressed only when the commit, the stage and the
  error all match the previous failure. A different error on the same commit
  now notifies.

### Added

- Revision check. Builds set the `org.opencontainers.image.revision` label
  and the `BAY_GIT_SHA` build argument to the 12-char commit SHA. The pull
  signal body gains `revision` and `built_at`; the webhook writes them as
  optional trigger lines 3 and 4 (old triggers still work). After a pull
  deploy turns healthy, `rebuild.sh` compares the running container's label
  with the expected revision and raises a `Revision check` failure on a
  mismatch, or logs `live <sha> in <N>s` on a match.

  **Upgrade notes:** an image built before this release has no revision
  label. If the build server skips a build because that image already exists
  in the registry, the app host's first check reports it as missing. Rebuild
  once (push a commit) to clear it.

### Upgrade notes

- Run `bin/bay deploy production --tags git_deploy` on every host. `rebuild.sh`
  runs on both the build server and the app hosts, and the revision check needs
  the new script on both sides.
- The Traefik read timeout is static config. Deploy it with
  `--tags traefik` on the host that runs Zot. Traefik restarts.

## [0.8.0] - 2026-10-02

### Added

- Optional remote BuildKit builder over the tailnet. Set
  `git_deploy_remote_builder_endpoint` (for example `tcp://100.64.0.8:1234`)
  and the build server registers a second buildx builder, `bay-remote`, with
  the `remote` driver and mutual TLS. The PEM files come from
  `git_deploy_remote_builder_ca`, `_cert` and `_key` (vault, lowercase keys).
  They go to `<stack_dir>/.buildkit/` on the build server, mode `0600`. When
  the endpoint is set and a PEM value is empty, the role stops the deploy.
- `<stack_dir>/bin/select-builder.sh` picks the builder for each build. It
  probes the remote builder (`timeout 5 docker buildx inspect --bootstrap`)
  and prints `bay-remote` when it answers, or the local builder when it does
  not. Webhook auto-builds and `bin/bay deploy` remote builds both use it.
  Each webhook build logs one `builder=<name>` line.
- When a webhook build fails on the remote builder and the remote then does
  not answer, `rebuild.sh` retries the build one time on the local builder. It
  sends the new `build.remote_fallback` alert (`debug`, off by default). When
  the remote still answers, the failure is real and there is no retry. Only
  the last attempt counts for the circuit breaker.
- Docs: "Remote builder over the tailnet" in `docs/build-strategies.md`. It
  covers the remote BuildKit container, mTLS, GC, the ACL rule and how to
  verify.

### Changed

- The builder prune (`roles/cronjobs`) still prunes the local builders only.
  A guard comment and a test keep the remote builder out of the list. Its GC
  is configured on the remote host.

### Upgrade notes

- Nothing changes until you set `git_deploy_remote_builder_endpoint`. With
  the default (`""`), every build uses the local builder as before.
- The builder is registered by the `git_deploy` role. After you set the
  variables, run `bin/bay deploy production --tags git_deploy` one time. That
  writes the certificate files, registers `bay-remote` and installs
  `select-builder.sh` on the build server.
- Set the variables in `group_vars/all`. Other hosts install the helper on
  the build server during remote builds.
- Under a default-deny Headscale ACL, add a rule from the build server to the
  remote BuildKit port, for example
  `{ action: accept, src: [infra], dst: ["buildbox:1234"] }`, and deploy it
  with `--tags headscale`.

## [0.7.5] - 2026-10-01

### Fixed

- `bin/bay validate` now refuses a service name that collides with a name Bay
  derives. A service name must not end in `-vpn`, `-public`, `-health` or
  `-tailnet`. Bay builds its Traefik router names from these endings, and the
  v0.7.4 CrowdSec whitelist trusts routers ending in `-vpn@docker`. On a
  public service named `foo-vpn`, a 403 that a Traefik middleware answers
  (for example a ForwardAuth deny) would have been ignored. A
  service or accessory named `X-new` is also an error when a service `X`
  exists, because `X-new` is the canary container of `X` during a zero-downtime
  deploy.
- The CrowdSec YAML check now covers Bay's own parser files too. It matched
  only `custom-*.yaml`, so a bad render of
  `bay-vpn-allowlist-refusals.yaml` was not caught. It now matches
  `custom-*.yaml` and `bay-*.yaml`, and still skips Hub-managed files. The
  check runs after Bay deploys its parser.
- The reconciler tar is now packed atomically. With forks, two hosts could pack
  the same file on the controller at once, and a third host could then ship a
  half-written tar. Each host now packs into its own temporary file and renames
  it onto the final path. The temporary file is removed if the pack fails.

### Upgrade notes

- `bin/bay validate` may now reject a service name. Rename any service whose
  name ends in `-vpn`, `-public`, `-health` or `-tailnet`, or that is another
  service's name plus `-new`. Bay did not check `tailnet_proxies` names, which
  also get a `-tailnet` router, so keep those away from these endings too.
- The crowdsec role runs from `provision.yml`, so a deploy does not apply the
  parser check. Run `bin/bay provision <env> --tags crowdsec` on each
  environment.
- The tar fix needs no action.

## [0.7.4] - 2026-10-01

### Fixed

- CrowdSec no longer bans a client for requests that Bay's VPN allowlist
  refused. The `vpn-only` middleware answers 403 to a client outside the VPN,
  and no backend sees the request. The Hub HTTP scenarios could not tell that
  403 from an app's own 403. `crowdsecurity/http-admin-interface-probing`
  banned an operator for 24 hours after three requests to `/admin`, `/Admin`
  and `/ADMIN/x` from outside the VPN. The bouncer drops before the SSH rule,
  so SSH stopped too. The crowdsec role now installs a whitelist parser that
  drops an event before any scenario sees it, when all three are true: the
  status is 403, no backend was reached, and the router carries the VPN chain
  (`<svc>-vpn@docker` or `<proxy>-tailnet@file`). An app's own 403, any other
  status, and every public router are still detected. To turn it off, set
  `crowdsec_ignore_vpn_refusals: false`. See docs/crowdsec.md.
- `bin/bay deploy <env> -- --check --diff` now prints the container plan
  (GH#2). Check mode skipped the reconciler, so a dry run showed no plan for
  containers. The reconciler now runs in plan-only mode from a temporary
  directory on the host, with the new bundle and package, and the directory
  is removed afterwards. Plan-only mode makes no change to any container. The
  plan uses the images and env files on the host now, so a pending rebuild or
  secret change shows as NoOp. See "Check mode" in docs/reconciler.md.

### Upgrade notes

- The crowdsec role runs from `provision.yml`, so a deploy does not apply
  this fix. Run `bin/bay provision <env> --tags crowdsec` on each environment.
- A ban that already exists stays. Remove it with
  `sudo cscli decisions delete --ip <ip>` on the server.

## [0.7.3] - 2026-10-01

### Fixed

- A canary swap that falls back no longer force-kills the canary. If the
  swap failed after the old container was already stopped and removed (for
  example `rename` raised), the healthy canary was the only live copy. The
  fallback removed it with a force remove, and Docker sends SIGKILL for that.
  It now stops the canary first, like every other removal.
- The stop and healthcheck settings in the `container_lifecycle` defaults now
  reach the reconciler. Before, the reconciler ignored them and used its
  built-in values, so a consumer override in `group_vars` had no effect. The
  variables are `container_lifecycle_stop_timeout`,
  `container_lifecycle_healthcheck_timeout` and
  `container_lifecycle_healthcheck_poll`. The bundle now carries them in an
  optional `config` object. An unknown key or a value that is not a positive
  number stops the run before it touches any container.

### Upgrade notes

- Nothing to do. The defaults give the same values that deploys already used:
  stop timeout 30, healthcheck timeout 120, healthcheck poll 1.
- The poll default in the role file changed from 5 to 1. The old 5 was never
  applied. The new 1 matches what deploys already did, so live behaviour does
  not change.
- A consumer that set one of the three variables above in `group_vars` will now
  see it take effect. Check those values before you deploy.

## [0.7.2] - 2026-10-01

### Fixed

- A deploy no longer kills a running container without warning. To recreate
  a container, or to remove one that left `services.yml`, the reconciler
  force-removed it, and Docker sends SIGKILL to a force-removed container.
  The process got no chance to shut down. A recreated PostgreSQL accessory
  came back with "database system was not properly shut down; automatic
  recovery in progress". Postgres recovers from its write-ahead log, but
  Redis or Valkey loses every write since its last snapshot. The reconciler
  now stops the container first and removes it after. Docker sends the
  image's own stop signal (SIGINT for postgres) and waits up to 30 seconds
  before it kills the process. This applies to a recreate, to an orphan
  removal, and to the old container when a canary swap falls back to a
  recreate. A recreate still pulls the new image before it stops the old
  container, so the download adds no downtime.

### Upgrade notes

- Nothing to do. A recreate or an orphan removal can now take up to 30
  seconds longer while the container shuts down. A container that stops
  quickly costs no extra time.

## [0.7.1] - 2026-10-01

### Security

- `vpn_routes` now ignores letter case (GH#3). Each entry rendered as Traefik
  `PathPrefix`, which compares case-sensitively. Many backends (Express and
  React Router by default) route paths without regard to case. So `/Admin` or
  `/ADMIN/x` missed the VPN router, fell through to the public catch-all and
  reached the backend from the internet. Each entry now renders as
  ``PathRegexp(`(?i)^/admin`)``: a prefix match like before, with no case.
  The health router uses the same match, so a `healthcheck_path` such as
  `/Admin/health` under a VPN route keeps the VPN chain.
  `public_routes` stays case-sensitive: there a case variant falls through to
  the VPN catch-all, which fails closed. See "Traefik Label Generation" in
  docs/services.md.

### Fixed

- `bin/bay dev-link` refuses a target that is not a framework checkout. It
  only checked that the target was a git repository, and a consumer is one
  too. `Path.cwd()` gives the physical path, so from a consumer reached
  through a symlink the default `../bay` can resolve to the consumer itself.
  dev-link then deleted `.bay/` and linked the consumer into itself. The
  target must now hold `version.yml`, and it must be neither the consumer nor
  the consumer's own pinned `.bay/` clone (`bin/bay dev-link .bay` used to
  delete the clone and link `.bay` to itself).

### Upgrade notes

- Run `bin/bay deploy <env>`. The VPN router rule is a container label, so
  each service with `vpn_routes` is recreated once on the next deploy. Nothing
  changes for services without `vpn_routes`.
- A `vpn_routes` entry may hold `.`, `+` and `*` (the schema allows them).
  They still match literally. The services schema already refuses any other
  character with regex meaning.
- dev-link: nothing to do. A target that is a real framework checkout links
  the same as before.

## [0.7.0] - 2026-09-23

### Added

- `config_files_mode: public` on a service or accessory makes its
  `config_files` world-readable. Bay writes config files as 0640 and their
  directories as 0750, owner the deploy user, group `docker`, so a container
  that runs as another uid (a `node` image runs as uid 1000) could not read a
  folder mounted from there, and a service block has no `user` or `group_add`
  to fix it from the container side. With `public`, that definition's files
  become 0644, and every directory on the way to them, `<stack_dir>/config`
  included, becomes 0755. A file listed by a public and a private definition
  is public. A directory shared with private files exposes their names, never
  their contents. `bin/bay validate` and the deploy both refuse any value
  other than `private` and `public`. See "Config Files" in docs/services.md.
- The config file tasks moved from `roles/deploy_stack/tasks/main.yml` into
  `config_files.yml`, so a test can run them for real against a scratch
  directory.

### Upgrade notes

- Nothing to do. Without the key, modes stay 0640 and 0750, the same as
  before, on every host.

## [0.6.12] - 2026-09-19

### Fixed

- webhook receiver: alerts now reach `alert_recipients`. The receiver's
  `send_alert` read only the legacy pair (`docker_monitor_telegram_*` and
  `alert_webhook_url`). A consumer on `alert_recipients` has those empty, so
  `webhook.fanout_failed` (warn, on by default) reached nobody, while
  `bin/bay alerts list` showed it as delivered. The receiver now routes like
  `bay_notify` and docker-monitor, with the same shared helpers: mute first,
  then the legacy pair unchanged, then each recipient whose alert IDs contain
  the alert. deploy_stack writes the routing table (`BAY_ALERT_ROUTING`, no
  secrets) and the recipient credentials (`BAY_RC_<n>_TOKEN`,
  `BAY_RC_<n>_URL`, `BAY_RC_<n>_HEADERS`, same index as `/etc/bay/alert.env`)
  into the receiver's 0640 env file, and only when `alert_recipients` is set.
- webhook receiver: the operator mute file now applies to its alerts, for the
  legacy pair too. The directory that holds it is mounted read-only at
  `/etc/bay-alert-policy`, so a new mute reaches the running container.
- docker_monitor: crash rows no longer stay in the state file for ever. A row
  for a one-off `docker run` container that never starts again was kept
  indefinitely. Rows older than the new `docker_monitor_crash_state_max_age`
  (default 604800 seconds, 7 days, `0` keeps them) are dropped when the file
  is written and when the monitor starts. A row with an unparsable
  `crashed_at` is dropped too.
- docker_monitor: the first `container.health_check_failed` after a host boot
  is no longer suppressed. The cooldown compared `time.monotonic()`, which
  counts from boot, against a default of 0, so every unhealthy event in the
  first 300 seconds after a reboot looked like a repeat.
- docker_monitor, webhook receiver: `datetime.utcnow()`, which is deprecated,
  is replaced. The alert timestamp reads the same.

### Upgrade notes

- Rebuild and recreate the webhook receiver on every host that runs
  `bay-webhook`: `bin/bay deploy <env>`, or
  `bin/bay deploy <env> --tags git_deploy,deploy_stack`. `git_deploy`
  rebuilds the `bay-webhook:latest` image and `deploy_stack` writes the env
  file and recreates the container. `bin/bay webhook <env>` alone rebuilds the
  image but does not recreate the container. Every consumer gets one
  recreate, because the image and the mounts change.
- Re-render the monitor with `bin/bay deploy <env> --tags monitoring`.
- Consumers still on the legacy pair see no change in what the receiver sends,
  except that mutes now apply.
- A recipient that uses `token_env`, `chat_id_env` or `url_env` reads that
  variable from the receiver container's environment, which holds only what
  its env file and spec set. Such a recipient gets no webhook receiver alerts
  unless the named variable is one of those, for example
  `TELEGRAM_BOT_TOKEN`. Use a literal `bot_token` or `url` from the vault to
  get them.

## [0.6.11] - 2026-09-19

### Fixed

- docker_monitor: container alerts now reach `alert_recipients`. The monitor's
  `send_alert` threw the alert ID away and sent only through the legacy pair
  (`docker_monitor_telegram_*` and `alert_webhook_url`). A consumer that had
  moved to `alert_recipients` has those empty, so `container.crash`,
  `container.restart_loop` and `container.health_check_failed` were delivered
  to nobody, while `bin/bay alerts list` showed them as delivered. The monitor
  now routes like `bay_notify`: the per-recipient alert IDs are resolved at
  render time with the same filter, and credentials are read at run time from
  `/etc/bay/alert.env`. They are never written into the script.
- docker_monitor: the operator mute file (`bin/bay alerts disable`) now
  applies to container alerts, for the legacy pair too. Before, a mute did not
  reach the monitor at all.
- docker_monitor: `container.restart_loop` no longer re-alerts every detection
  window for the same container. A new per-container cooldown,
  `docker_monitor_restart_loop_cooldown` (default 1800 seconds, `0` turns it
  off), is stored in the monitor's state file, so restarting the monitor does
  not reset it. A looping container sent the same critical alert every minute
  before this.

### Upgrade notes

- Re-render the monitor with `bin/bay deploy <env> --tags monitoring`. Until
  you do, the old script keeps sending to the legacy pair only.
- Consumers still on the legacy pair see no change in what is sent, except
  that mutes now apply. The legacy pair still ignores `alerts_disabled` and
  `enabled_by_default`, the same as in the shell emitters, so it still gets
  `container.recovered`.

## [0.6.10] - 2026-09-19

### Fixed

- reconcile: canary swaps now run one at a time within a dependency phase. The
  executor used to run every action of a phase in one thread pool, so a plan
  with three zero-downtime services started three canaries at once. A canary
  keeps the old container running until the new one is healthy, so each one
  doubles its service's memory for the length of the health wait, and parallel
  canaries add those peaks together. On a 4 GB host with its swap already full
  that ended in a host-wide out-of-memory kill of one canary, whose rescue
  then recreated the service with downtime. The phase's other actions still
  run in parallel first. The report keeps plan order.

### Upgrade notes

- None. A deploy with several canaries takes longer, by roughly the sum of
  their health waits instead of the longest one.

## [0.6.9] - 2026-09-11

### Fixed

- headscale: 0.6.8 did not actually upgrade anything. `headscale_version` is
  declared twice, and 0.6.8 bumped the copy in `roles/headscale/defaults/`,
  which only that role's own tasks read. The pin that decides the running image
  is in `roles/access_gateway/defaults/`, because
  `container_lifecycle/tasks/build_specs.yml` builds the container spec from it
  and the reconciler is the only thing that creates the container. The deploy
  ran clean and reported no change, which is what a bump to a value nothing
  reads looks like. Both copies now pin 0.29.3.
- `tests/test_headscale_version_pin.py` asserts the two pins agree, that the
  reconciler still builds the image tag from the variable, and that neither
  pins a floating tag. A duplicated constant fails silently in one direction
  only, so a comment was not enough.

### Upgrade notes

- The 0.6.8 upgrade note named the wrong tag. `--tags headscale` renders
  headscale's config but not the container, so it cannot change the image. Use
  `bin/bay deploy <env> --tags deploy_stack`, which is what reconciles
  containers. Headscale restarts; existing sessions coast through it.
- Consumers that took 0.6.8 are still running headscale 0.29.2. Take 0.6.9 and
  deploy with the tag above. Confirm with
  `docker inspect headscale --format '{{.Config.Image}}'`, not with the deploy
  output.

## [0.6.8] - 2026-09-10

### Changed

- headscale: bump default `headscale_version` 0.29.2 → 0.29.3. Fixes
  re-registering a tagged node with a different pre-auth key (it now applies
  the new key's tags), plus other tag-related fixes.

### Upgrade notes

- `bin/bay deploy <env> --tags headscale` recreates the headscale container on
  the new image. Headscale restarts; existing WireGuard sessions coast
  through it.

## [0.6.7] - 2026-09-10

### Fixed

- `gateway enroll --help` led with `--user laptop`, which models a user per
  device. The user is an ownership principal, so one human's machines share
  one user; `docs/tailnet-naming.md` has said so since it was written, but
  the CLI example taught the opposite and that is what gets copied. The
  example now names a person and a device separately, and the docstring
  points at `gateway key <user>` for an existing owner.
- `make typecheck` failed on an unused `type: ignore` in
  `src/bay_reconcile/models.py`, which blocked every release. Removed.

## [0.6.6] - 2026-09-10

### Fixed

- **The healthcheck duration check ran too late to protect the container.**
  v0.6.5 converted the durations in `SdkDockerClient.create`. A `Recreate`
  removes the running container before it creates the replacement, so a
  `ValueError` raised at create time costs the same outage the daemon's own 400
  cost, and the docstring claiming it failed "at plan time" was wrong. The
  conversion now runs in `bundle.spec_from_dict`, as the JSON bundle is turned
  into ContainerSpecs, which is before the fleet is observed and before a single
  action is planned. A bundle carrying a duration that cannot be parsed fails
  the whole run there, with the service name, the key and the value in the
  message, and the reconciler exits non-zero without making one docker call.
  `create` still calls the converter, on values that are already integers, so a
  ContainerSpec built by hand rather than loaded from a bundle is still
  protected. **No container recreates because of this.** `config_hash` is
  computed by the `bay_spec_hash` filter over the raw inventory spec, on the
  control node, before the bundle is written, so converting a duration on the
  server cannot reach it. The hash of a spec with `interval: 5s` is frozen in a
  test against the value v0.6.5 produced.

## [0.6.5] - 2026-09-10

### Fixed

- **A healthcheck interval written as `5s` took postgres down for ten
  minutes.** The reconciler forwarded a spec's healthcheck block to the docker
  SDK unchanged, and the SDK's `Healthcheck` wants `interval`, `timeout` and
  `start_period` as integer nanoseconds. The daemon answered "cannot unmarshal
  string into Go struct field
  HealthcheckConfig.Config.Healthcheck.Interval of type time.Duration" and the
  create failed. A `Recreate` removes the old container first, so postgres was
  simply gone, and every service on that host that talks to it went down with
  it. The three duration fields are now converted from Go duration syntax
  (`5s`, `1m30s`, `500ms`, `1.5s`) to nanoseconds before the create, and a
  number is passed through as it was written. A duration that cannot be parsed
  raises at plan time, naming the key and the value, instead of reaching the
  daemon. Postgres was the only shipped service with a healthcheck, which is
  why no earlier deploy hit this. The remove-before-create ordering that turned
  a rejected create into an outage is not fixed; it is written up under
  **Known gaps** in `docs/reconciler.md`.

## [0.6.4] - 2026-09-09

### Fixed

- **A rebuilt image under an unchanged tag was reported as a no-op.** The
  reconciler decided no-op against the config hash alone, and that hash covers
  the config text. Rebuilding `bay-webhook:latest` on the host left every byte
  of that text identical, so `deploy_stack` planned a no-op and the container
  kept running the old layers. The only recovery was `docker rm -f bay-webhook`
  by hand, which cost about a minute of webhook downtime. The observed state of
  a container now carries two image ids: the one the running container was
  created from, and the one its image reference resolves to in the local image
  store. A mismatch is a reason to redeploy, on the same path a config change
  takes, so a zero-downtime service still swaps through a canary. This is not a
  `:latest` special case. A pinned tag whose local id changed after a pull
  redeploys under the same rule. An image that is not present locally reads as
  unknown, which is never on its own a reason to redeploy.

## [0.6.3] - 2026-09-09

### Fixed

- **A push to a repo with two hooks built only one of its services.** GitHub
  sends the SAME `X-GitHub-Delivery` GUID to every hook configured on one
  repository. The webhook receiver remembered accepted deliveries by that GUID
  alone, so the first service path to arrive consumed the GUID and the second
  path was answered "duplicate" and never built. Where the first service also
  skipped on its own path filter, the push built nothing at all. The replay key
  now carries the service path as well as the GUID, so each service gets an
  independent first look at a delivery. Replay protection per service is
  unchanged: the same GUID twice on one path is still ignored.
- **The Tests job on CI was red at the bootstrap stage.** `tests/test_bootstrap.sh`
  ran `bin/bay setup --no-interactive`, which refuses to scaffold without an
  admin SSH key. A CI runner has no `~/.ssh`, so the job failed on every push
  to main. The test now generates a throwaway key in its temp directory and
  passes it with `--ssh-key-file`, so it is hermetic. It no longer borrows the
  developer's personal key either.

### Upgrade notes

- The webhook fix is in `roles/git_deploy`. Run a deploy with the `git_deploy`
  tags to put the new receiver on the host. Until you do, a repo with two hooks
  keeps losing one of them.

## [0.6.2] - 2026-09-08

### Fixed

- **A custom scenario removed from `crowdsec_custom_scenarios` stayed on the
  host and kept banning.** The role rendered one file per list entry and never
  removed the file of an entry that left the list, so dropping or renaming a
  scenario left it live in `/etc/crowdsec/scenarios`. One renamed scenario
  survived three times on a production consumer, and an operator deleted the
  stale file by hand each time. The role now removes the orphaned files and
  flushes their decisions, so a provision converges. Only regular files
  carrying the custom-scenario template header are candidates, so hub content
  (a symlink into `/etc/crowdsec/hub`) and the built-in scenarios are never
  touched.

### Upgrade notes

- The prune lives in `provision.yml`, not `deploy.yml`. Run
  `bin/bay provision <env> --tags crowdsec` to pick it up. A deploy will not
  apply it.
- The first run removes every file this template rendered whose name is no
  longer in your `crowdsec_custom_scenarios`, and deletes that scenario's
  decisions. If you renamed a scenario and want its bans kept, re-add the old
  name to the list before you provision.

## [0.6.1] — 2026-09-08

### Fixed

- **A deploy that changed nothing showed no probe table at all.** Every service
  landed in the untouched group, the green-collapse rule fired, and the whole
  summary was two lines — less than an operator saw before grouping existed.
  The untouched group now collapses only when there is a touched group to
  contrast it against. Caught on a live deploy, not by the tests.

## [0.6.0] — 2026-09-08

### Added

- **The post-deploy summary now says which failures are this deploy's.**
  Results are split into what the deploy changed and everything else, and the
  headline count plus the "users may see outages" warning are driven by the
  first group only. Nothing is hidden: everything is still probed, and an
  untouched failure is still shown under its own heading, named as pre-existing
  or collateral. A fully green untouched group collapses to one line.

  Narrowing the probe set to the changed containers was the obvious design and
  it is wrong. A deploy can break a container it never touched — an accessory
  recreated under an unchanged app, a Traefik config change, a link moving —
  and hiding those would be a worse failure than the noise this replaces.

  The grouping comes from the reconciler, which already knew. It now writes its
  report to `.bay/.reconcile-report/<host>.json` on the control node, one file
  per host, because role-scoped facts do not cross into `hostvars`. The CLI
  empties that directory before each deploy, so a report present afterwards can
  only be the current run's.

  No report is a normal outcome, never an error: the summary prints one line
  saying so and falls back to the previous ungrouped output. That covers
  `--check`, a `--tags` deploy, and a server still on an older framework.

- `bin/bay healthcheck` is unchanged. It has no deploy to attribute to.

### Upgrade notes

- `.bay/.reconcile-report/` is created inside the framework clone, which is
  already gitignored in every consumer. Nothing to add.

## [0.5.2] — 2026-09-08

### Changed

- The `[gated]` reason is shorter, so it stops wrapping in an 80-column
  terminal: `gated -- basicauth, no healthcheck_path`.

### Fixed

- `docs/services.md` said a dead service behind the carve-out answers 502/503.
  That is the supervisor case. A fully stopped container answers 404, because
  Traefik's Docker provider drops the router with the container. Both fail; the
  docs now say which you will see.

## [0.5.1] — 2026-09-08

### Fixed

- **The health-probe carve-out shipped in 0.5.0 did not actually take effect.**
  The router was emitted correctly and still lost. A Traefik router with no
  explicit priority does not rank last — it ranks by the CHARACTER LENGTH of
  its rule. A plain two-domain public service renders a single router with no
  priority label, giving it an effective priority of 66, and the health
  router's `30` lost to it. The probe kept getting the password challenge.

  The health router's priority is now far above anything the length rule can
  produce. Anyone on 0.5.0 with a `basic_auth` + `healthcheck_path` service
  should move to 0.5.1; 0.5.0 behaves exactly like 0.4.0 for them.

  Only the two-router shape (`public_routes` / `vpn_routes`) sets explicit
  priorities, so the original test compared against the one shape that already
  worked. Two tests now pin the single-router shape and the constant itself.

## [0.5.0] — 2026-09-08

### Fixed

- **A password-protected service is no longer reported as an outage.**
  Traefik's basic-auth middleware answers before the request reaches the
  container, so an unauthenticated probe of a gated service returned 401
  whether the app was healthy or dead. `bin/bay deploy` reported those as
  failures on every single run, and `bin/bay healthcheck` exited non-zero for
  them.

  A service declaring both `middleware.basic_auth` and `healthcheck_path` now
  gets a dedicated Traefik router for that one exact path with basic auth
  removed from its chain, so the probe reaches the real backend. A healthy
  service answers 200; a dead one fails, with 502/503 when an inner process
  died behind a live supervisor and 404 when the container is gone. The
  carve-out is an exact `Path()` match, never a prefix, and every other
  middleware still applies to it.

  This deliberately does **not** treat 401 as a pass. That would have converted
  a false failure into a false success and defeated `healthcheck_path`, which
  exists to catch an inner process dying behind a live supervisor.

- **A gated service with no `healthcheck_path` now reports `[gated]`** rather
  than failing. There is nothing to carve out and the probe cannot see the
  backend, so it is counted separately: never a pass, never a failure, and it
  does not affect the `bin/bay healthcheck` exit code. The message names the
  field to declare.

- **The failure headline counts services, not domains.** A service with two
  domains reported as "2 service(s) failed". `summarize()` gains
  `failed_services` for the headline; `failed` keeps its per-target meaning
  because it is part of the `--json` payload.

### Changed

- **One renderer for both healthcheck outputs.** `bin/bay healthcheck` and the
  post-deploy summary kept separate copies of the same render loop and had
  already drifted apart on glyphs and on the wording of the headline. They now
  share `render_results()`. Every number in the totals line carries its unit.

### Upgrade notes

- **Expect one recreation per affected service on the first deploy after this
  version.** The new health router changes the container's config hash, so the
  reconciler recreates (or canary-swaps, where `zero_downtime: true`) each
  service that declares both `middleware.basic_auth` and `healthcheck_path`.
  No action needed; it settles after one deploy.
- **Check that your health route is safe to serve unauthenticated.** It becomes
  reachable without a password on services that declare both fields. It should
  return liveness only. If yours leaks build metadata, config or internal
  hostnames, fix the route before upgrading. See the `basic_auth` section in
  `docs/services.md`.
- **`--json` consumers:** `summarize()` gained `gated` and `failed_services`,
  and each result object gained a `gated` boolean. `failed` is unchanged and
  still counts probe targets, not services.

## [0.4.0] — 2026-09-02

### Changed

- **Zot tag-retention policy is now bounded.** The prior policy kept every
  tag pushed OR pulled within `zot_retention_keep_within` (720h) **in
  addition to** the N-most-recent rules, so a busy repo could accumulate
  dozens of tags — one repo hit 91. The policy is now: 10 most recently
  pushed tags (`zot_retention_keep_count`), 3 most recently pulled tags
  (new `zot_retention_keep_pulled_count`, so the image a host is
  currently running survives even if older than the last 10 pushes), and
  any tag matching `zot_retention_always_keep` (new, default `["^latest$"]`).
  `zot_retention_keep_within` is now empty/optional — set it only if you
  understand that a time window is unbounded in count.

### Upgrade notes

- Consumers that already override `zot_retention_keep_within` keep their
  window (it now layers on top of the bounded count rules instead of
  replacing them). Everyone else gets the new bounded policy after
  `bin/bay deploy <env> --tags zot` on the registry host. Zot applies the
  new policy on its **next GC pass**, not immediately — GC runs on
  `zot_gc_interval` (default `24h`).

## [0.3.4] — 2026-09-02

### Fixed

- **A git credential prompt could hang a deploy indefinitely on the
  SSH/no-token fetch path.** Every git invocation now disables terminal
  prompts and fails fast instead.

## [0.3.3] — 2026-09-02

### Fixed

- **Two more tasks reported "changed" on every deploy of an unchanged
  consumer.** The git-deploy-config timestamp sentinel was written with
  mode `0644` and no group, so `webhook.yml`'s state/ re-group task found
  it group-write-less and flipped it back on every following run. Fixed by
  writing the sentinel with group `git_deploy_build_group` and mode `0664`,
  same as the directory it lives in. Applied the same fix to the other
  writers under `state/`: `cb_state_migration.yml`'s migration script,
  `rebuild.sh`'s `_write_state`, and the stall watchdog's audit log and
  rate-limit file. The first deploy after this change flips the sentinel's
  mode once; every deploy after that is a no-op.
- **The git_deploy-side image pull reported "changed" on every deploy,
  even with nothing new to pull.** `roles/git_deploy/tasks/main.yml`'s
  "Pull freshly-pushed images on deployment server" task (the
  remote-build-strategy counterpart to `build_image`'s batched pull) had
  `changed_when: true` unconditionally. Fixed by keying `changed_when` on
  `docker pull`'s own "Downloaded newer image" marker, matching the
  batched task. No behaviour change beyond the reported status.

## [0.3.2] — 2026-09-02

### Fixed

- **`systemd.yml` reset the build state directory to 0755 on every deploy,
  blocking the webhook container from writing its own log.** Two tasks in
  `roles/git_deploy` disagreed about `{{ stack_dir }}/state`. `webhook.yml`
  sets it to owner `app_user`, group `git_deploy_build_group`, mode `2770`,
  so the webhook container (UID 10001, GID 2000) and `rebuild.sh` can both
  write into it. `systemd.yml` runs right after and re-created the same
  directory as mode `0755` with no group at all, undoing that on every
  single run. The webhook container could not write
  `telegram-failures.log` into `/state` on any 0.3.0 or 0.3.1 host, which
  is exactly the failure the `/state` mount was meant to fix. The mode
  flip also made three unrelated tasks report "changed" on every deploy
  for no functional reason. Fixed by making `systemd.yml` declare the same
  owner, group, mode and `become: true` / `become_user: root` as
  `webhook.yml`. The first 0.3.2 deploy changes the directory mode once,
  from `0755` back to `2770`; every deploy after that is a no-op.

## [0.3.1] — 2026-09-02

### Fixed

- **Webhook receiver rate-limit labels rendered as integers, breaking every
  deploy that enables it.** `build_specs.yml` set the
  `bay-webhook-ratelimit` Traefik labels (`ratelimit.average`,
  `ratelimit.burst`) from `"{{ webhook_rate_limit_average | default(10) }}"`.
  Ansible's native Jinja renders a pure `{{ ... }}` template as its native
  Python type, so with no override set the label came out as the int `10`,
  not the string `"10"`. The reconciler feeds specs straight to the Docker
  API, and Docker requires label values to be strings, so `docker create`
  for the webhook receiver failed on every 0.3.0 deploy with the webhook
  receiver enabled, and the container was removed. Fixed by adding
  `| string` to both labels, matching the existing pattern next to them
  (`zot_port | default(5000) | string`, `WEBHOOK_PEER_TIMEOUT`). The
  container is recreated on the next deploy with the fix in place.

## [0.3.0] — 2026-09-02

Three milestones land together: security hardening, onboarding repair and
performance. Read **Upgrade notes** before you bump an existing consumer, the
release adds validate failures that stop a deploy.

### Security

- **Headscale OIDC needs an allowlist.** `headscale_oidc_allowed_domains`,
  `headscale_oidc_allowed_users` and `headscale_oidc_allowed_groups` render
  into the OIDC block. `bin/bay validate` fails when OIDC is on with no
  allowlist, and warns when OIDC is on with no `headscale_acl_policy`.
- **`expose: host` needs `expose_host_ack: true`.** The flag records that you
  accept the port bypassing nftables and CrowdSec. Nothing about the rendered
  port changes.
- **A split entrypoint fails closed.** The deploy now stops when
  `traefik_split_entrypoints` is true and `traefik_public_bind_ip` is blank,
  instead of rendering a wildcard bind that collides with
  `websecure_tailnet`.
- **Traefik TLS floor and metrics bind.** `minVersion` is TLS 1.2 by default,
  and the metrics entrypoint binds `traefik_metrics_bind_ip` (`127.0.0.1`)
  instead of every interface. `sniStrict` stays off unless you set
  `traefik_tls_sni_strict: true`. `tls.options` is dynamic-only configuration,
  so the floor is rendered to `<stack_dir>/dynamic/tls-options.yml` and served
  by Traefik's file provider, which is now always enabled. The same block in
  the static `traefik.yml` is parsed and then ignored, so it enforced nothing.
- **The webhook receiver mounts `<stack_dir>/state`.** It writes its Telegram
  delivery-failure log there. The compose path already mounted it, the
  reconciler spec did not, so on the reconciler path those writes went to the
  container's writable layer and were lost on every recreate.
- **New nftables knobs, both default-compatible.**
  `nftables_forward_permissive` defaults to `true` and
  `nftables_container_host_ports` defaults to empty, so today's behaviour is
  unchanged until you tighten them.
- **Alert credentials leave world-readable files.** The Telegram token and
  alert webhook URL now live in `/etc/bay/alert.env` (0600 root), sourced by
  every notify snippet and read through `EnvironmentFile=` in nine systemd
  units. Emitter scripts drop to 0750, and recipient literals render as
  `BAY_RC_<n>_*` names.
- **PATs leave `argv` and `.git/config`.** `git_deploy` uses a `GIT_ASKPASS`
  helper in `clone_repos`, remote builds and `rebuild.sh`, build directories
  are 0700, and build args pass through the task environment rather than the
  command line.
- **Other credential handling.** `rebuild.sh` reads its HMAC key from a 0600
  file, `zot` takes the htpasswd password on stdin, webhook receiver and
  Watchtower secrets ship via `env_file`, and `no_log` covers the `cscli
  console enroll` and `alert_channel` URI tasks.
- **Basic-auth hashes move from APR1 to bcrypt**, with a deterministic
  secret-derived salt so the render stays stable for the reconciler. Passwords
  are unchanged. This adds a `bcrypt` Python dependency.
- **`bay-docker-ro inspect --format` is restricted** to an allowlist that
  cannot reach `Config.Env`, and the Headscale config file is 0640.
- **Every consumer value that reaches a shell or SQL is quoted.** The services
  schema gained name and env-key regexes plus route patterns, and
  `bin/bay validate` restates them in words. A bare `/` in `public_routes` and
  a backtick in a Traefik rule literal are now errors.
- **`bin/bay vault set` reads the value from stdin** when the positional
  argument is omitted, so a secret stops landing in shell history. The
  positional form still works for one transition release.
- **Build trigger and state directories are 2770**, owned by a fixed
  `bay-build` group (GID 2000) shared by `app_user` and the webhook container
  (UID 10001). They were 0777, so any local user could force a rebuild or
  rewrite the circuit breaker state.
- **Webhook receiver hardening.** A 1 MiB body cap is checked before any read,
  a 256-entry `X-GitHub-Delivery` cache drops repeat deliveries after the HMAC
  check, `/health` returns a service count instead of names, and the webhook
  router carries a Traefik rate-limit middleware
  (`webhook_rate_limit_average|burst|period`).
- **Supply chain pins.** restic is verified by sha256, Watchtower is pinned by
  index digest, the CrowdSec apt repository uses a `signed-by` keyring instead
  of the deprecated global key, and `github.com` host keys ship with the role
  so every `GIT_SSH_COMMAND` uses `StrictHostKeyChecking=yes`.
- **Push gates.** Gate A matches the public remote by root-commit identity,
  bypass environment variables print a loud warning, and `leak-scan` gained a
  lowercase-plus-digit entropy tier proven red by re-injection, plus an
  allowlist for RFC 2606 reserved TLDs.

### Onboarding

- **A scaffolded project now validates and deploys.** The generated Gatus
  service used `healthcheck.path` and the MariaDB accessory used
  `backup.method: mysqldump`, neither of which the schema accepts. They are now
  `healthcheck_path` and `mysql`.
- **Services that declare `config_files` now ship them.** `catalog/gatus/`
  gained `files/gatus/config.yaml`, and `bin/bay setup` copies catalog files
  into the consumer's `files/` through the same helper `bin/bay service add`
  uses.
- **Scaffolded secrets are generated**, by the same generator `bin/bay secret`
  uses, instead of written as empty strings.
- **The SSH-key step has no Skip.** At least one key is required for the admin
  account, because provisioning disables root and password login and a keyless
  admin locks you out. `--defaults` and `--no-interactive` take `--ssh-key` or
  `--ssh-key-file`, or fall back to `~/.ssh/*.pub`.
- **Four new validate hard failures**: a missing `config_files` entry, an admin
  user with no SSH keys, an empty referenced secret, and an empty or
  placeholder `letsencrypt_email`.
- **The wizard defaults the access gateway to none.** Headscale is still
  offered, and you can add it later with `bin/bay setup --gateway headscale`.
  It adds a DNS record, a Tailscale client install and four post-deploy steps
  to a first run, which is a lot to carry before anything works.
- **`bin/bay setup --defaults` requires `--server-ip` and `--domain`.** It
  previously scaffolded `0.0.0.0` and `example.com`, which can never deploy.
  `--defaults` also works without a TTY now.
- **`bin/bay setup` takes `--email`** (alias of `--letsencrypt-email`), honoured
  on every path. Without it, `admin@<domain>` is derived and announced.
- **The wizard scaffolds `group_vars/all/alerts.yml`** with an empty
  `alert_recipients` list. The legacy `docker_monitor_telegram_*` keys are no
  longer generated, they sit at env level and outrank
  `group_vars/all/alerts.yml` by Ansible precedence, which causes duplicate
  delivery once a real recipient is added.
- **One documented entry path**: clone over HTTPS into `.bay/`, run
  `.bay/bootstrap.sh`, then `bin/bay setup`. `README.md`, `SKILL.md`,
  `docs/onboarding.md` and `example/README.md` now agree, and a test fails the
  build if they drift.
- **`make bay:setup` delegates to `.bay/bootstrap.sh`.** It no longer carries
  its own copy of the pin, symlink, `uv sync` and Galaxy-install logic, which
  had drifted and never created `bin/bay`. `BAY_REPO` defaults to the HTTPS
  clone URL, override it for SSH.
- **`bin/bay setup` and `bootstrap.sh` write an identical `bin/bay` wrapper**,
  from the single source `scripts/bin-bay-wrapper.sh`. `bootstrap.sh` also
  snapshots the wrapper before checking out an older pinned tag, which used to
  leave a newer checkout with no wrapper to copy.
- **`bin/bay doctor` is trustworthy now.** The SSH check tries `root` then
  `admin_user` instead of your local username, the DNS check resolves a service
  domain instead of the apex a wildcard record does not cover, the webhook
  check reads `group_vars/all/services.yml` which the wizard actually writes,
  and a crashed probe counts as an issue instead of printing "All checks
  passed".
- **The next-steps panel lists DNS, secrets, validate, doctor, provision and
  deploy, in order.** DNS guidance prints for every gateway choice, not only
  for Headscale.
- **Error hints point at commands that exist.** "bay not found" names the clone
  and `.bay/bootstrap.sh` rather than `bin/bay setup`, and the version-drift
  guard names `.bay/version.yml` and `.bay-version` instead of the pre-1.0
  `.argo` paths.
- **Docs corrections.** `docs/features.md` dropped a non-existent `admin`
  access mode and the `validate` versus `doctor` mix-up, and `README.md`
  dropped a dangling link and the hand-written `git tag` advice.
  `CONTRIBUTING.md` documents the release process.
- **Repository metadata**: GitHub issue and pull request templates, a CI badge,
  and a README note to run `make install` before `make test`.
- **`docker_registry_org`, `docker_registry_username` and
  `docker_registry_token` are no longer scaffolded.** They stay readable and
  deprecated for existing consumers, move to the `docker_registries` list in
  `registry.yml`.

### Performance

- **The connection strategy is visible.** `run_playbook` prints one
  `strategy: mitogen_linear` line, or names the linear fallback. `--profile` on
  `deploy` and `provision` turns on `profile_tasks` and the timer. See
  `docs/performance.md`.
- **Pipelining and lighter facts.** The wizard `ansible.cfg` template and the
  example gain `pipelining = True`, and `provision.yml` and `restore.yml` now
  gather a reduced fact subset like `deploy.yml` already did.
- **Image pulls batch into one task** with `xargs -P 4`, reported changed only
  on a real download, retries kept.
- **Per-container task fans collapse.** `log_archive` renders one setup script
  instead of 16 looped tasks, `database_provision` runs one idempotent SQL
  script per accessory instead of six `docker exec` calls, and the log
  retention boundary uses one batched `docker inspect` per side.
  `deploy --list-tasks` drops from 269 to 255.
- **The reconciler ships as a tar**, gated on `<stack_dir>/.reconcile/.version`,
  and reads env digests in one batched call instead of one per container. The
  canary poll interval is 1 s.
- **Remote builds push straight from buildx.** One `--push` call carries both
  tags, so the `--load` export and import and the two separate `docker push`
  steps are gone, on the Ansible path and the webhook path alike.
- **Opt-in registry layer cache.** `git_deploy_registry_cache` (default
  `false`) adds `--cache-to`/`--cache-from type=registry` against a
  `:buildcache` tag, so a builder prune no longer costs a from-scratch rebuild.
- **`bin/bay validate` caches successful probes for an hour** in
  `<bay_dir>/.validate-probe-cache`, a gitignored JSON dotfile. Only successes
  are cached, credentials in a repo URL are stripped before writing, and
  `--no-probe-cache` forces a full re-probe. A success can go stale for up to
  an hour.
- **The CLI starts faster.** `requests` and `ruamel.yaml` load inside the
  functions that use them, taking `import bay_cli.cli` from about 200 ms to
  about 80 ms, pinned by a test.
- **`make test-python` runs under `pytest-xdist`** (`-n auto --dist loadfile`),
  about 125 s down to about 29 s.

### Upgrade notes

Work through these in order on an existing consumer.

- **Run `bin/bay validate` first, before you deploy.** This release adds hard
  failures that stop the pre-deploy gate, and each one is real breakage that
  used to be silent.
- **OIDC allowlist.** If `headscale_oidc_issuer` is set, add at least one of
  `headscale_oidc_allowed_domains`, `headscale_oidc_allowed_users` or
  `headscale_oidc_allowed_groups`. Until now the tailnet accepted any account
  your issuer authenticated, so audit `bin/bay gateway nodes` for unexpected
  entries and treat it as an incident.
- **`expose: host`.** Add `expose_host_ack: true` next to every `expose: host`
  on a service port or accessory, or validate fails.
- **Admin SSH keys.** Every user in the `ssh-access` group needs a non-empty
  `keys` list. An empty list was skipped silently and left the server with no
  way in.
- **Empty secrets.** A referenced vault key with an empty value now fails.
  Generate one with `bin/bay secret` and re-encrypt with
  `bin/bay vault encrypt production`.
- **`letsencrypt_email`.** It must be set and not a placeholder. There is no
  ACME opt-out, so an empty value always meant broken SSL.
- **`config_files`.** Every entry must have a real file behind it under the
  consumer's `files/`. A service that mounts a config it never received starts
  and dies.
- **Identifier regexes.** Service, accessory, database and database-user names
  must match `[a-z0-9_-]`, and env keys must be POSIX names. Renaming a service
  is not free, it renames the container and the database, so check before you
  rename.
- **`public_routes: ["/"]` is an error.** It made a `vpn` service entirely
  public. Set `access: public` deliberately instead.
- **Uppercase database names.** SQL identifiers are quoted now, and a quoted
  identifier is case-sensitive where an unquoted one was folded to lower case.
  Compare `psql -c '\l'` and `\du` against `services.yml` before upgrading.
- **Rotate the Telegram bot token and the alert webhook URL.** Both were
  readable by any local user on every Bay host, through 0755 scripts and
  `systemctl show`. Changing the file mode does not un-leak the old value.
- **Rotate the GitHub PAT.** The token is removed from `.git/config` only on
  the next clone, so existing build checkouts still hold the old one.
- **Delete `{{ git_deploy_build_dir }}` to force a fresh clone.** The role
  re-creates it at 0700 with the `GIT_ASKPASS` helper in place.
- **`/etc/bay/alert.env` needs a run to exist.** `roles/alert_channel` is in
  both `provision.yml` and `deploy.yml`, and alerts stop working on a host that
  has not been re-run since the upgrade. The roles that own the affected
  systemd units re-render them and reload systemd themselves, so no manual
  `daemon-reload` is needed, but a deploy alone does not cover the
  provision-only units.
- **Provision-only roles need a provision run.** Use
  `bin/bay provision <env> --tags crowdsec`, `--tags nftables`,
  `--tags outbound_monitor` and `--tags docker_monitor`, or
  `bin/bay deploy --rig <env>` for the rig roles. A plain deploy does not reach
  them.
- **Purge the old CrowdSec apt key by hand.** A host provisioned before this
  release still carries it in the global `/etc/apt/trusted.gpg`, where it can
  sign any repository. Delete it from `trusted.gpg` or `trusted.gpg.d`, the new
  `signed-by` keyring does not remove it.
- **The webhook container is recreated once.** The image gains a fixed UID
  (10001) and the build directories move to group `bay-build`, GID 2000. Change
  `git_deploy_build_gid` only if 2000 collides on your host. Local tooling that
  drops a `.trigger` file as another user stops working, use `bin/bay build`.
- **One extra container recreate from the basic-auth change.** The label value
  moves from APR1 to bcrypt, so each basic-auth-protected container is
  recreated on the first deploy. Passwords are unchanged and no consumer edit
  is needed.
- **`traefik.yml` and `nftables.conf` change once**, so Traefik is recreated
  and the ruleset reloads on the first run after the upgrade. The nftables
  defaults are compatible, so nothing is blocked that was allowed before.
- **TLS 1.2 floor and loopback metrics.** Clients below TLS 1.2 are refused.
  Any external Prometheus scraping Traefik's metrics port directly breaks, set
  `traefik_metrics_bind_ip` back or scrape over the tailnet.
- **Traefik gains a dynamic-config mount and is recreated once.**
  `<stack_dir>/dynamic` is now mounted on every host, not only hosts with
  `traefik_dns_challenge_enabled`, because the TLS floor lives there. The file
  provider is enabled unconditionally for the same reason. Nothing else in the
  directory changes, so no routing changes with it.
- **The webhook receiver is recreated once** to pick up the
  `<stack_dir>/state` mount.
- **Remote builds no longer leave a local image on the build server.** The
  build pushes directly, so anything expecting to `docker run` the image there
  will not find it, and registry credentials now fail inside `buildx build`
  rather than at a later push step.
- **`git_deploy_registry_cache` is opt-in.** Setting it writes an extra
  `:buildcache` tag to the image repository, expect that tag to grow to about
  one full layer set.
- **Add `pipelining = True`** under `[ssh_connection]` in your consumer
  `ansible.cfg`. The wizard template only renders at scaffold time, so an
  existing consumer keeps the old block. If a host was hardened outside Bay
  with `requiretty`, you will see `sudo: sorry, you must have a tty`, see
  `docs/performance.md`.
- **First deploy rewrites each `.retention` file and reships the reconciler.**
  Both are expected one-time changes. A host with a hand-edited `.reconcile/`
  tree is only repaired when the marker moves, delete
  `<stack_dir>/.reconcile/.version` to force a reship.
- **Watchtower and restic.** Watchtower is pinned by digest and recreated once.
  Overriding `backup_restic_version` now also requires
  `backup_restic_checksum`, the two move together.
- **Webhook behaviour changes.** Payloads above 1 MiB get 413, repeated
  deliveries are no-ops within the cache window, `/health` returns a `services`
  integer instead of an array, and bursty fan-out may see 429.
- **The wizard gateway default flipped to none.** This affects new projects
  only. An existing consumer with Headscale configured is untouched.
- **Legacy alert keys.** Consumers setting `docker_monitor_telegram_*` keep
  working. Migrate to `alert_recipients` and delete the legacy keys in the same
  change, or alerts arrive twice.
- **Your `Makefile` is a generated file.** Re-run `bin/bay setup --force` to
  pick up the new `bay:setup` target, it backs the old one up to
  `Makefile.bak`. The old target still works.
- **Framework developers: run `uv sync`.** This bump adds `bcrypt` and
  `pytest-xdist`. Without it, `make test` fails with
  `unrecognized arguments: -n`. Consumers get both through `bin/bay install`.

## [0.2.4] — 2026-08-25

### Fixed

- Restore notifications go through the alert channel. `restore.yml` POSTed
  directly to `api.telegram.org`, so a restore alert reached one hard-coded
  sink, ignored every recipient's `min_level`, never appeared in
  `bin/bay alerts list` and could not be muted. It now composes an alert and
  delegates delivery to `roles/alert_channel`, like every other alert.
- A failed restore now alerts. Previously only success notified, so the case
  that matters — the restore broke and the pre-restore backup is the way back
  — was silent. `restore.yml` runs inside a block with a rescue handler that
  emits `restore.failed` and re-raises the original failure.

### Added

- Two alert IDs: `restore.completed` (`info`, default **off**) and
  `restore.failed` (`critical`, default **on**).

### Changed

- The control-node alert fan-out moved from
  `roles/deploy_stack/tasks/send_deploy_alert.yml` to
  `roles/alert_channel/tasks/send_alert.yml`, and callers reach it with
  `include_role` + `tasks_from: send_alert`. Ansible has no cross-role task
  include path, so while it lived in `deploy_stack` no playbook could use it —
  which is exactly why `restore.yml` had its own sender. Behaviour for
  `deploy.complete` / `deploy.failed` is unchanged.
- `tests/test_alert_registry.py` scans the top-level playbooks as well as
  `roles/`. A new guard in `tests/test_alert_channel.py` fails the build on any
  Telegram request outside `roles/alert_channel`.

### Upgrade notes

- Restore alerts are now **routed**, not broadcast. `restore.failed` is
  `critical`, so it reaches every recipient. `restore.completed` is `info` and
  ships **default-off** like the other success notices — if you relied on the
  old unconditional "Restore complete" message, opt back in:

  ```yaml
  # group_vars/all/alerts.yml
  alerts_enabled:
    - restore.completed
  ```

  A recipient with `min_level: warn` or higher will not receive
  `restore.completed` unless it is named in `alerts_enabled`, which overrides
  `min_level`.
- No deploy is needed for this. Both alerts are sent from the control node by
  `restore.yml` itself, so `bin/bay update` is enough.

## [0.2.3] — 2026-08-25

### Changed

- Docs pass from a newcomer review: aligned `example/README.md`'s Setup
  section with the main README's Quick Start (wizard-first, manual `.vault_pass`
  path kept as a fallback); added `bin/bay validate` ahead of provision/deploy
  in the README and onboarding guide, with `doctor` (environment) vs
  `validate` (config, also runs automatically on every deploy) spelled out;
  fixed the "copied by bootstrap.sh" attribution to `bin/bay setup`; fixed a
  broken doc-server pointer in `docs/README.md` and added a Glossary; fixed
  the `docs/tailnet-ingress.md` ACL-policy anchor in `docs/access-gateways.md`;
  gave the public pre-push hook a runbook pointer that resolves for outside
  contributors; documented accessory `expose:` and a worked `database:`
  binding example in `docs/services.md`, plus a new "Rotating a secret"
  section covering the config-hash/env-digest recreate behavior; clarified
  how an operator alert mute reaches the hosts in `docs/alerting.md`; added
  CrowdSec lockout-recovery re-enable + verify steps; and documented the
  first-provision-as-root command form.

## [0.2.2] — 2026-08-25

### Changed

- Corrected the copyright holder named in LICENSE.

## [0.2.1] — 2026-08-25

### Fixed

- **`--check` no longer kills the play in three more places.** Ansible does not
  execute `command`/`shell` modules in check mode, but the registered result
  still carries `rc: 0` and an **empty** `stdout`. Anything that then parses
  that stdout dies with exit 2, taking the whole dry run with it. Same shape as
  the reconciler fix in 0.2.0.
  - `roles/tailscale_register` — "Set registration needed fact" piped
    `tailscale status --json` through `from_json`. Now guarded with
    `is not skipped`, and every consumer of the fact reads
    `_needs_registration | default(false)`, so the role cannot register a node
    in a dry run.
  - `roles/headscale` — "Install validated headscale ACL policy" copied
    `.policy.hujson.staged`, which does not exist in check mode, and aborted
    with "Source does not exist". Now skipped in check mode; the staging task
    still shows the rendered policy as a diff.
  - `restore.yml` — "Parse snapshot details" piped `restic snapshots --json`
    through `from_json`. Now guarded, and every task that dereferences `_snap`
    is gated on it being defined.

  None of these uses `rc is defined`: a command task skipped by check mode
  registers `rc: 0`, so that guard passes and the crash happens anyway.
- **A comment-only nftables change no longer restarts the Docker daemon.**
  Reloading nftables wipes the chains Docker installs at daemon start, so the
  reload handler has to restart Docker and every container on the host bounces.
  `Deploy nftables configuration` is a `template`, which reports "changed" for
  any byte difference — so an edit to a comment in `nftables.conf.j2` (or a
  re-rendered `ansible_managed` header) triggered that outage on every host.
  The live file is now fingerprinted with comments and trailing whitespace
  stripped, before and after the write, and the reload is notified only when
  the fingerprints differ. The file itself is still always written, so comments
  land on the host; a real ruleset change reloads exactly as before, including
  on first install.

### Upgrade notes

- The nftables fix is picked up by `bin/bay deploy --rig <env>` (the role runs
  under `_rig_mode`) or by `bin/bay provision <env> --tags nftables`. A plain
  non-rig `bin/bay deploy` does **not** run the role. Upgrading costs no
  container bounce: if your ruleset is semantically unchanged, the new
  fingerprint comparison matches and nothing is reloaded.

## [0.2.0] — 2026-08-24

### Added

- **`.githooks/pre-push` — two gates on every push out of this repo.**
  - *History gate.* If the remote is recognised as the public one, a ref that
    descends from the private root commit is refused. The public repo is a
    separate orphan history; publishing such a ref would expose everything
    behind it.
  - *Content gate.* `scripts/leak-scan.sh` now runs against **every commit
    being pushed**, not just the tip. A leak introduced in one commit and
    fixed in the next leaves a clean worktree and a dirty history — and
    history is what a push publishes.
- **`scripts/leak-scan.sh` takes an optional REF argument.**
  `bash scripts/leak-scan.sh <commit>` scans that commit's tree instead of the
  working tree. With no argument the behaviour is unchanged.
- **Environment variables read by the hook:**
  - `BAY_PUSH_SKIP_GUARDS=1` — skip both gates. Prints a loud multi-line
    warning to stderr. For a human who has read the runbook, not for scripts.
  - `BAY_PUBLIC_REMOTE_PATTERN` — extra extended regex that marks a remote URL
    as public, on top of the built-in match.
  - `BAY_PRIVATE_ROOTS` — space-separated root commit SHAs, overriding the
    built-in list. Used by the tests.
- `tests/test_pre_push_hook.py` proves both gates go red, including the
  leak-in-a-middle-commit case that a tip-only scan would wave through.

### Fixed

- **`--check` no longer kills the deploy play.** `container_lifecycle`'s
  `Reconciler report` task debug-printed `_reconcile_result.stdout |
  from_json`. In check mode the reconciler command task is skipped, so
  `stdout` is empty and `from_json` raised — the play died with exit 2 and
  `bin/bay deploy <env> -- --check --diff` was unusable as a dry run. The
  report is now guarded by `when: _reconcile_result is not skipped`. It is
  deliberately not an `rc`-based guard: a command task skipped by check mode
  still registers `rc: 0`.

### Upgrade notes

- Run `make hooks` (or `make install`) in every existing clone, **including the
  public one**. `core.hooksPath` is per-clone git config, so a clone that has
  never run it has no hooks at all and neither gate applies.

## [0.1.1] — 2026-08-24

Post-launch hygiene pass over the public tree.

### Security
- `debug_agent` no longer grants root by accident. The `docker` group is
  gone from the default groups (it is root-equivalent); opt back in with
  `debug_agent_docker_access: true`, documented as "this is root". The bare
  `cat`/`tail`/`head`/`grep`/`journalctl`/`systemctl` sudo entries — which
  allowed `sudo cat /etc/shadow` and a `!sh` root shell from the pager —
  are replaced by four argument-validated wrappers: `bay-readlog`
  (paths limited to `debug_agent_readable_paths`), `bay-journal`,
  `bay-systemctl-ro` and `bay-docker-ro` (all `--no-pager`, read-only
  verbs only). Sudo target narrowed from `(ALL)` to `(root)` with
  `env_reset`, `secure_path` and `!use_pty`.
- `files/hooks/validate-ssh.sh` is now deny-by-default: it parses the
  command, refuses chaining, `-J`, `-F`, `-e`, `ProxyCommand`/`ProxyJump`/
  `User` options, and checks every `user@host` token. It was previously
  bypassed by any command containing the string `debugbot@`. The docs now
  describe it as a client-side guard, not a security boundary.

### Changed
- `scripts/leak-scan.sh` closes five blind spots found by planting test
  leaks: case-insensitive and apex-domain hostname matching, base64 in the
  entropy check, an IPv6 section, a long-hex section, and a tracked-junk
  check; the `vendor/` exclusion now applies only to the entropy sections.
- Internal tracker IDs (`M85`-style) removed from docs, comments and tests.
- Small identity scrub: LICENSE holder, example usernames, one ADR incident
  narrative.

### Upgrade notes
- `debug_agent` lives in `provision.yml`: run
  `bin/bay provision <env> --tags users,agent-debug`.
- If your agent relied on `sudo cat`/`sudo tail`/`sudo docker ps`, switch
  it to `sudo bay-readlog`, `sudo bay-journal`, `sudo bay-docker-ro`.
  A consumer that overrides `debug_agent_sudoers_commands` or
  `debug_agent_groups` keeps its own list — and its own exposure.

## [0.1.0] — 2026-08-24

First public release.

Bay had a long private life before this tag — roughly 340 releases of it. The
version line restarts at 0.1.0 because this is the first release anyone
outside its original operator could actually use, and it would be dishonest to
present a 1.x number as a stability promise to a public that has never run it.
The code is mature; the public contract is new.

### What Bay does

Provisions and operates hardened Docker hosts with Ansible and a Python CLI:

- **Declarative service surface.** One `services.yml` describes every app and
  accessory — domains, ports, env, secrets, health checks, log retention.
- **Traefik ingress** in host-network mode, so services see real client IPs,
  with automatic TLS.
- **Access gateways.** `none` for a plain public deploy, `wireguard` for
  hand-configured peers, or self-hosted `headscale` for a managed tailnet with
  split-DNS, ACLs and per-device identity. All three sit behind one adapter
  contract; see `docs/access-gateways.md`.
- **CrowdSec and nftables** for intrusion detection and firewalling.
- **A server-side reconciler** that diffs desired against running container
  state instead of re-applying everything each deploy.
- **Build pipeline** with local, remote and registry strategies, a self-hosted
  Zot registry, and a webhook receiver for push-to-deploy.
- **Alerting** with a single registry of alert IDs and severities, fanned out
  to Telegram or any webhook adapter.
- **Restic backups**, multi-region deploys, and cross-region service links.

### Getting started

`README.md` for the tour, `docs/onboarding.md` for a walkthrough, and
`example/` for a complete runnable consumer to copy.

### Licence

MIT.
