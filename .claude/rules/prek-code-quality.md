---
name: prek-code-quality
description: This file describes python code quality rules
---

- Prefer using `prek` with a `.pre-commit-config.yaml` file to enforce code
  quality rules.
- The user's pre-commit configuration is leading. You don't need to enforce
  rules other than those in the user's pre-commit configuration.

## Running the gates

Prefer the project's task runner when one exists: look for a `justfile`
(`just`), a `Makefile` (`make`), or equivalent and use its recipes, since
they encode the blessed invocation. Otherwise run hooks through `prek`: a
single hook with `prek run <hook-id>`, all of them with `prek run
--all-files`.

Go through the task runner or `prek` rather than calling the underlying
tools directly. The bare tool may be absent from the environment, and you
would miss the pinned version and configured arguments.

## Before declaring work done

A change is not done until the relevant gates pass. Discover which checks
apply from `.pre-commit-config.yaml` and the task runner rather than
assuming a fixed set. If a gate reports a pre-existing failure, fix the
cause rather than working around it. Never silence a check (for example
with `# type: ignore` or `--no-verify`) to make it pass.
