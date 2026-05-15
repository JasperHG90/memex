"""Rank-Biased Overlap (RBO) for comparing two ranked lists.

Webber, Moffat, Zobel (2010) — "A Similarity Measure for Indefinite Rankings".
ACM TOIS 28(4). The metric is:

  RBO(S, T; p) = (1 - p) * Σ_{d=1..∞} p^(d-1) * |S_{:d} ∩ T_{:d}| / d

where ``S_{:d}`` is the prefix of length d. For finite lists this is
approximated by the extrapolated variant ("RBO_ext") that accounts
for the unseen tail.

We implement the full extrapolated form from Webber equation 32,
including the agreement-correction term over depths beyond the shorter
list. The shorter implementation that omits this correction undershoots
the published RBO_ext value (a prefix-of-longer would score ~0.94
instead of 1.0).

``p`` is the persistence parameter; common choices are 0.9 (top-heavy)
and 0.98 (more weight on the tail). 0.9 is the default — top-3
positions dominate the score, which matches how a reranker is judged.
"""

from __future__ import annotations

__all__ = ['rank_biased_overlap']

from typing import Hashable, Sequence


def rank_biased_overlap(
    list_a: Sequence[Hashable],
    list_b: Sequence[Hashable],
    p: float = 0.9,
) -> float:
    """Return the RBO similarity of two ranked lists.

    Args:
        list_a: First ranked list (most-relevant first). Items must be hashable.
        list_b: Second ranked list, same ordering convention.
        p: Persistence parameter in (0, 1). Higher p weights deeper
            ranks more heavily; 0.9 is top-heavy (P(reach rank 10) ≈ 0.39),
            0.98 is more uniform.

    Returns:
        RBO in [0.0, 1.0]. 1.0 iff the lists are identical up to
        ``min(len(a), len(b))`` and the shorter list is a prefix of
        the longer. 0.0 iff the lists share no element in any prefix.

    Raises:
        ValueError: if ``p`` is not in (0, 1).
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f'p must be in (0, 1); got {p!r}')

    # Empty-list edge cases: two empties match perfectly; one empty
    # against any non-empty has nothing in common.
    if not list_a and not list_b:
        return 1.0
    if not list_a or not list_b:
        return 0.0

    s = len(list_a)
    t = len(list_b)
    short_len = min(s, t)
    long_len = max(s, t)

    # Compute the overlap at each rank d ∈ [1, long_len] using growing sets.
    # Recomputing intersection size from set membership at each step is O(k)
    # per step; for k = 10–20 (typical top-k) the total cost is negligible
    # and avoids the foot-guns of incremental-counter maintenance.
    seen_a: set[Hashable] = set()
    seen_b: set[Hashable] = set()
    overlap_at: list[int] = []  # |S_{:d} ∩ T_{:d}| for d = 1..long_len
    for d in range(long_len):
        if d < s:
            seen_a.add(list_a[d])
        if d < t:
            seen_b.add(list_b[d])
        overlap_at.append(len(seen_a & seen_b))

    # Observed-agreement sum: (1 - p) * Σ_{d=1..k} p^{d-1} * (X_d / d)
    # where X_d is the observed overlap and k = long_len.
    weighted = 0.0
    for d_zero in range(long_len):
        d = d_zero + 1
        weighted += (p**d_zero) * (overlap_at[d_zero] / d)
    rbo_min = (1.0 - p) * weighted

    if short_len > 0:
        x_at_short = overlap_at[short_len - 1]
        agreement_at_short = x_at_short / short_len
    else:
        x_at_short = 0
        agreement_at_short = 0.0

    # Webber/Moffat/Zobel 2010, equation 32: when long_len > short_len, the
    # naive sum above understates agreement past d = short_len because
    # observed A_d = X_short_len / d falls off, even though the shorter
    # list's contribution to the unobserved tail is bounded by
    # X_short_len / short_len. Add back the difference per depth d ∈
    # (short_len, long_len]: (X_short_len / short_len) − (X_short_len / d)
    # = X_short_len * (d − short_len) / (short_len * d), weighted by
    # (1 - p) * p^{d-1}. Without this term, a prefix-of-longer scores
    # ~0.94 instead of the published RBO_ext value of 1.0.
    extrapolation_correction = 0.0
    if long_len > short_len and short_len > 0:
        for d_zero in range(short_len, long_len):
            d = d_zero + 1
            extrapolation_correction += (p**d_zero) * x_at_short * (d - short_len) / (short_len * d)
        extrapolation_correction *= 1.0 - p

    # Tail weight beyond long_len assumes agreement stays at the
    # last-observed value of A_short_len:
    # Σ_{d=long_len+1..∞} (1-p) * p^{d-1} = p^long_len, multiplied by the
    # extrapolated agreement.
    tail_weight = p**long_len
    rbo_ext = rbo_min + extrapolation_correction + agreement_at_short * tail_weight

    # Numerical guard: floating-point drift can push RBO microscopically
    # past 1.0 on identical lists; clamp.
    return max(0.0, min(1.0, rbo_ext))
