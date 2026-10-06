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

Result: pending
