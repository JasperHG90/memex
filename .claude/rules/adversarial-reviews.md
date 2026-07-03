---
title: adversarial-reviews
description: Requires an adversarial sub-agent review after completing a ticket or feature.
---

<constraint name="adversarial-reviews">
When you finish a ticket or feature, run an adversarial review before reporting it as done. Delegate to a sub-agent — a specialized agent if available, otherwise a general-purpose sub-agent briefed to review skeptically. Review happens in a separate agent, not inline, so the work gets a fresh and independent perspective.

Hand the reviewer the full context: what was asked, what you changed, and where to look. Act on the verdict and fix every issue it confirms. The work is done once the review passes or the user explicitly accepts the remaining findings.
</constraint>
