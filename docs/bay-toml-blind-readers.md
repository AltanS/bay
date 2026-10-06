# bay.toml blind-reader test

This test checks that the bay.toml schema has one reading. Three readers get the same
two inputs and nothing else. Each one writes down what the file deploys. The three
descriptions must match.

## Inputs

- `docs/bay-toml.md`, the schema reference.
- `tests/fixtures/bay_toml/corrected-example.toml`, the corrected example.

The readers get no source code, no fleet repo, no chat history and no other docs.

## Procedure

1. Start three independent readers. They do not see each other's answers.
2. Give each reader the two inputs and this question: "For each environment in the
   file, list every container that runs. For each container, give its name, image or
   build, port, routing (domain and path), access mode, password, limits, mounts, env
   values, secret names, needs and health check. Then list the scheduled jobs, the
   backups and anything you could not decide from the inputs."
3. Collect the three answers without edits.
4. Compare them field by field. A field converges when all three answers agree.
5. Record each difference below. A difference is a defect in the schema or the docs,
   not in the reader. Fix the docs or the schema, then run the test again.

## Pass condition

All fields converge, and the lists of open questions are empty or name the same items.

## Runs

Result: converged, after sixteen rulings.

2026-10-06. Three readers got only `docs/bay-toml.md` and `corrected-example.toml`. They
converged on the containers, the routes, the env vars and the data for both environments.

The readers differed only where the doc was silent. There were sixteen open points:

1. Whether `[needs.postgres]` names a database and user.
2. What the health path does when a route prefix covers it.
3. Who chooses the box port for `expose`, and the bind address.
4. Which sibling URL variables each service receives.
5. Whether a `locked` path needs the password.
6. How jobs are named, and what a job inherits.
7. What a service inherits for `update`, `logs`, `replicas` and `zero_downtime`.
8. Whether `[access.identity]` works in `public` mode.
9. The status code and the certificates of an alias redirect.
10. The name of a volume.
11. What a failed health check does during a deploy.
12. Whether `fleet_secrets` values differ per environment.
13. Whether an `open` path stays open in every environment.
14. What a service with an inline `build` inherits.
15. When `release` runs and what a failure does.
16. The default `health` of a service with a port.

Each point now has one plain sentence in `docs/bay-toml.md`, under Behavior. The example
changed in two more places: the web `health` is now `/healthz`, because `/api/health`
sat under the `/api` route prefix, and `[needs.postgres]` no longer names a database or
user.
