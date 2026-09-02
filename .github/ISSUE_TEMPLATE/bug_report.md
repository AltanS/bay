---
name: Bug report
about: Something in Bay does not do what it says it does
title: "[bug] "
labels: bug
---

## Before you file

**Redact your infrastructure.** Do not paste real domains, IP addresses, API
tokens, SSH keys or vault contents into this issue. Replace them with
placeholder values (`example.com`, `192.0.2.10`, `<token>`). This repository is
scrubbed of that data on purpose and a leak scan runs in CI.

## Version

Output of `bin/bay status` (from your consumer repo), or the framework tag you
are on:

```
```

## Where it happens

- [ ] Framework (this repo)
- [ ] Consumer repo (`.bay/` clone, `bin/bay` wrapper)
- [ ] Not sure

## The exact command

```bash
```

## What happened

Describe the failure. Paste the relevant output, redacted.

```
```

## What you expected

## Diagnostics

Output of `bin/bay validate` and/or `bin/bay doctor`, redacted:

```
```

## Environment

- Control machine OS:
- Target host OS:
- Python / `uv` version:
