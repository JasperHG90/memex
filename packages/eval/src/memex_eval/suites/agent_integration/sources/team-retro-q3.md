---
tags: [retro, alpha, team]
description: Q3 retrospective — Redis lessons, switching to in-process cache.
---

# Q3 2025 Team Retrospective

**Date:** October 8, 2025
**Facilitator:** Sarah Chen
**Attendees:** Project Alpha core team

## What went well

- Phase 1 delivered on schedule.
- Sarah Chen's incident response in August kept the outage to 2h18m.
- The new on-call rotation reduced alert fatigue measurably.

## What didn't go well

- **Redis was a mistake.** The added cache layer hurt more than it
  helped: it shifted complexity without absorbing the burst load we
  built it for. The August incident is on us.
- Phase 2 scope was vague at start-of-quarter; we burned a week
  redefining it.

## Lessons learned

The team agreed the Redis decision was driven by anxiety about read
load rather than measured need. We will:

1. **Switch to in-process caching** (LRU per service replica). The
   working set fits in 256MB per pod; we sized Redis for 8GB.
2. Sunset the Redis cluster by November 15.
3. Document the working-set measurement methodology so future cache
   decisions have data behind them.

## Action items

- Sarah Chen: own the in-process cache migration (ETA Nov 1).
- Platform team: tear down Redis cluster after migration is verified.
- Sarah Chen: write the working-set measurement runbook (ETA Nov 30).
