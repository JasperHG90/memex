---
title: uv-installer
description: Use `uv add` to install dependencies, not `uv pip`.
---

<constraint name="uv-installer">
Install dependencies with `uv add` so they land in `pyproject.toml`, not with `uv pip`, which installs into the environment without recording the dependency. The user can override this with an explicit instruction to do otherwise.
</constraint>
