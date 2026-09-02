---
name: Feature request
about: Propose a change to the framework
title: "[feature] "
labels: enhancement
---

## The problem

What are you trying to do, and where does Bay get in the way? Describe the
situation before the solution.

## Proposed behaviour

What should Bay do instead? Be concrete: the command, the variable, the
`services.yml` field.

## Alternatives considered

What did you try, and why was it not enough?

## Where does it belong?

- [ ] The framework (a role, the CLI, a playbook)
- [ ] A consumer's `services.yml` or `group_vars/`
- [ ] Not sure

Bay is deliberately small. Anything a consumer can already express in its own
`services.yml` usually belongs there, not here.

## Anything else

Links, prior art, a sketch of the config you want to write.
