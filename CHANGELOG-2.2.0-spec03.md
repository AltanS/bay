Spec 03: init, fleet init and the removed write verbs

- `bay fleet init` makes the first commit, `bay: fleet init`, so the README quick start works with no manual `git commit`. When the commit fails (for example, git has no user name), the fleet stays and Bay prints the `git commit` line to run. `--from` makes no commit.
- `bay fleet init` puts `.vault_pass` in the new `.gitignore`. After `--from`, Bay warns when the clone does not ignore `.vault_pass`.
- `bay init` stops in a fleet folder, or below one, before it writes anything. The hint says to write `projects/<name>/bay.toml`.
- `bay init` looks for the `Dockerfile`, `package.json` and `pyproject.toml` at the repo root, the default build context. With `--toml-path services/api/bay.toml` it no longer warns "no Dockerfile" for a root `Dockerfile`.
- `[build] repo` is a new key in `bay.toml`. An app that lives in the fleet and builds from source names its repo there. The compile takes the build repo from `[build] repo`, then from the lock `repo`. With neither, the error names both places. An app in its own repo builds from its lock `repo`, and a different `[build] repo` there is a compile error. `bay validate` reports an in-fleet build with no repo before a deploy.
- `bay adopt` takes `[build] repo` as the lock `repo` and leaves the key out of the `bay.toml` it writes into the app repo. The compiled `build.repo` does not change.
- `bay self update --help` shows the version from `version.yml` in its example, not a fixed tag.
- Removed `bay service add`, `bay service edit` and `bay service remove`. They wrote the generated `services.yml` by hand, and the next compile refused the file. `bay service list|show|catalog|prune-webhooks` and `bay server add|remove` stay. The helpers that only the removed verbs used are gone too: `StackConfig` write methods, `catalog.resolve_dependencies` and `BayError.dependency`.

### Upgrade notes

`bay service add|edit|remove` are removed, write `bay.toml` instead. An in-fleet app that builds from source may set `[build] repo`; without it, the compile still uses the lock `repo`.
