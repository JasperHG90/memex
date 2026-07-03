---
tags: [incident, redis, alpha]
description: August 2025 incident — Redis cache cascading outage.
publish_date: 2025-08-14
---

# Incident — Redis cache cascading outage

**Date:** August 14, 2025
**Severity:** SEV-1
**Duration:** 2 hours 18 minutes
**Lead responder:** Sarah Chen

## Summary

The Redis cache layer (added July 2025 — see Project Alpha Q3 update)
experienced a memory exhaustion event under sustained read traffic.
When Redis began evicting hot keys, the application fell back to the
primary database, which saturated under the suddenly-uncached load.

The cascade took down the API layer for 2h18m before mitigation.

## Timeline

- 14:02 UTC — Redis memory hits 92%.
- 14:11 UTC — eviction rate exceeds re-population rate; cache hit-rate
  drops from 88% to 31%.
- 14:14 UTC — PostgreSQL connection pool exhausted; API begins
  returning 503.
- 14:22 UTC — alerts page Sarah Chen's team.
- 14:38 UTC — initial mitigation: scale Redis vertically.
- 15:11 UTC — vertical scaling insufficient; partial outage continues.
- 15:47 UTC — Sarah Chen's team disables Redis caching entirely;
  traffic shifts back to PostgreSQL with read replicas absorbing load.
- 16:20 UTC — full recovery.

## Contributing factors

1. Redis was sized for the steady-state working set, not the burst-load
   working set.
2. The fallback path to PostgreSQL had no rate-limiting; cache miss meant
   immediate DB hit with no smoothing.
3. We had no alert on Redis memory-pressure trending — only on saturation.

## Action items

- Add memory-pressure trend alert (24h prediction).
- Implement request coalescing on the fallback path.
- Re-evaluate the cache layer choice (see Q3 retro).
