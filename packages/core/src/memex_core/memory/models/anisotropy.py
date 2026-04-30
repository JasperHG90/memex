"""Sliding-window Z-score normalizer for embedding anisotropy correction.

Modern high-dimensional embedding models suffer from representation anisotropy:
vectors cluster in a narrow cone, causing even semantically unrelated texts to
yield cosine similarities above 0.7.  D-MEM (arXiv:2603.14597v1) §4.1 proposes
maintaining a sliding window of recent similarity scores to compute a historical
mean and standard deviation, then applying Z-score normalization mapped through
a sigmoid function.

This module provides ``AnisotropyCorrector``, a stateful sliding-window normalizer
that can be wired into any site that produces cosine similarity scores.
"""

from __future__ import annotations

import math
from collections import deque
from threading import Lock

import structlog

logger = structlog.get_logger('memex.core.memory.models.anisotropy')

DEFAULT_WINDOW_SIZE = 1024

DEFAULT_EPSILON = 1e-8


class AnisotropyCorrector:
    """Sliding-window Z-score -> sigmoid normalizer for cosine similarity scores.

    Usage::

        corrector = AnisotropyCorrector()
        # Feed raw similarity scores and get corrected values back
        corrected = corrector.normalize(raw_similarity)

    Cold-start: returns the raw score unchanged until the window has at least
    ``min_samples`` observations (default 32), at which point Z-score
    normalization kicks in.

    Set ``window_size=0`` to disable anisotropy correction (normalize returns
    the raw score unchanged for all inputs).
    """

    def __init__(
        self,
        window_size: int = DEFAULT_WINDOW_SIZE,
        epsilon: float = DEFAULT_EPSILON,
        min_samples: int = 32,
    ) -> None:
        if window_size < 0:
            raise ValueError(f'window_size must be >= 0, got {window_size}')
        self._window_size = window_size
        self._epsilon = epsilon
        self._min_samples = min_samples
        self._disabled = window_size == 0
        # deque(maxlen=0) crashes, so use maxlen=1 as a no-op buffer when
        # disabled — normalize() returns early before appending anyway.
        self._window: deque[float] = deque(maxlen=max(1, window_size))
        self._lock = Lock()
        self._count: int = 0
        self._mean: float = 0.0
        self._m2: float = 0.0

    @property
    def window_size(self) -> int:
        return self._window_size

    @property
    def min_samples(self) -> int:
        return self._min_samples

    @property
    def count(self) -> int:
        """Number of observations currently in the window."""
        return self._count

    @property
    def mean(self) -> float:
        """Current running mean of similarity scores in the window."""
        return self._mean

    @property
    def std(self) -> float:
        """Current running standard deviation (population) of similarity scores."""
        if self._count < 2:
            return 0.0
        variance = self._m2 / self._count
        return math.sqrt(max(0.0, variance))

    def _update_stats_add(self, value: float) -> None:
        """Add a value to running stats (Welford online algorithm)."""
        self._count += 1
        delta = value - self._mean
        self._mean += delta / self._count
        delta2 = value - self._mean
        self._m2 += delta * delta2

    def _update_stats_remove(self, value: float) -> None:
        """Remove a value from running stats (Welford removal)."""
        if self._count <= 1:
            self._count = 0
            self._mean = 0.0
            self._m2 = 0.0
            return
        self._count -= 1
        delta = value - self._mean
        self._mean -= delta / self._count
        delta2 = value - self._mean
        self._m2 -= delta * delta2
        # Clamp floating-point drift
        self._m2 = max(0.0, self._m2)

    def normalize(self, raw_similarity: float) -> float:
        """Normalize a raw cosine similarity score using Z-score -> sigmoid.

        If fewer than ``min_samples`` observations have been seen, returns
        the raw score unchanged (cold-start passthrough).

        If ``window_size=0`` was passed at init, returns the raw score unchanged
        for all inputs (disabled mode).

        Args:
            raw_similarity: Cosine similarity in [-1, 1] (typically [0, 1]).

        Returns:
            Normalized similarity in (0, 1).
        """
        if self._disabled:
            return raw_similarity

        with self._lock:
            if len(self._window) == self._window_size:
                evicted = self._window[0]
                self._window.append(raw_similarity)
                self._update_stats_add(raw_similarity)
                self._update_stats_remove(evicted)
            else:
                self._window.append(raw_similarity)
                self._update_stats_add(raw_similarity)

            if self._count < self._min_samples:
                return raw_similarity

            std = self.std
            z_score = (raw_similarity - self._mean) / (std + self._epsilon)

            # Sigmoid mapping: compresses Z-scores back to (0, 1)
            # centered at 0.5 (neutral) with steepness controlled by the
            # natural scale of Z-scores.
            return 1.0 / (1.0 + math.exp(-z_score))

    def reset(self) -> None:
        """Clear all state."""
        with self._lock:
            self._window.clear()
            self._count = 0
            self._mean = 0.0
            self._m2 = 0.0


class AnisotropyCorrectorGroup:
    """A collection of named ``AnisotropyCorrector`` instances.

    Different similarity call sites may have different score distributions,
    so each site should maintain its own corrector.  This group provides
    convenient access by name (e.g. 'retrieval', 'contradiction', 'dedup').
    """

    def __init__(
        self,
        names: list[str] | None = None,
        window_size: int = DEFAULT_WINDOW_SIZE,
        epsilon: float = DEFAULT_EPSILON,
        min_samples: int = 32,
    ) -> None:
        self._correctors: dict[str, AnisotropyCorrector] = {}
        self._window_size = window_size
        self._epsilon = epsilon
        self._min_samples = min_samples
        self._lock = Lock()
        for name in names or []:
            self._correctors[name] = AnisotropyCorrector(
                window_size=window_size,
                epsilon=epsilon,
                min_samples=min_samples,
            )

    def get(self, name: str) -> AnisotropyCorrector:
        """Get or create a corrector for the given site name."""
        with self._lock:
            if name not in self._correctors:
                self._correctors[name] = AnisotropyCorrector(
                    window_size=self._window_size,
                    epsilon=self._epsilon,
                    min_samples=self._min_samples,
                )
            return self._correctors[name]

    def reset(self) -> None:
        """Reset all correctors."""
        with self._lock:
            for c in self._correctors.values():
                c.reset()
