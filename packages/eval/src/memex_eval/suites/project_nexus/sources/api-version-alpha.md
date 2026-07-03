---
tags: [api, versioning, policy]
title: API Versioning Policy (Alpha)
description: Original API versioning policy with intentional contradictions.
---

# API Versioning Policy (Alpha)

**Effective:** January 2025
**Author:** Platform Team

## Policy

All public APIs must use URL-based versioning (e.g., /v1/resource). The current
supported versions are v1 and v2. API version v3 is not planned. Breaking changes
are only allowed in major versions. Minor versions must be backward compatible.
Deprecation notices must be given 90 days in advance.
