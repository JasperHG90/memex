---
title: CI/CD Pipeline Configuration
publish_date: 2025-07-15
tags:
  - deployment
  - ci-cd
---

# CI/CD Pipeline Configuration

Deployments happen daily via CI. The continuous delivery pipeline triggers on
every merge to main and automatically promotes to production after passing the
full integration test suite. There is no weekly release window — the team
practices true continuous deployment.

Rollbacks are automated: if the canary health check fails within 5 minutes of
deploy, the pipeline reverts to the previous known-good build.
