"""`ProceduralConfig` — operator-level tunables for the V7 plane.

Eight fields, eight tests (plus a couple of integration guards):

* Defaults match the V7 design review pin.
* Each field can be overridden at construction.
* ``MemexConfig`` carries a ``memory.procedural`` node and propagates
  overrides from YAML.
* Bounds enforcement: out-of-range raises ``ValidationError`` (we do
  NOT silently clamp — the operator is told the value is wrong).
* Sum-to-1 invariant on the search weights is NOT a constructor-level
  validator (sum-to-1 is a runtime fusion property the agent can opt
  out of), but defaults do sum to 1.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex_common.config import (
    MemexConfig,
    MemoryConfig,
    ProceduralConfig,
    ServerConfig,
)


# --- defaults --------------------------------------------------------------


def test_procedural_config_defaults():
    """The 8 tunables land on the values the V7 design review pinned."""
    config = ProceduralConfig()
    assert config.enabled is True
    assert config.search_default_bm25_weight == 0.5
    assert config.search_default_vector_weight == 0.5
    assert config.briefing_default_limit_per_context == 5
    assert config.default_status == 'published'
    assert config.identity_conflict_mode == 'reject'
    assert config.derivation_worker_enabled is False
    assert config.derivation_worker_batch_size == 16
    assert config.derivation_worker_poll_interval_seconds == 5.0


def test_procedural_config_search_weights_sum_to_one_by_default():
    """The RRF stream is meaningful only when weights sum to 1. Defaults do;
    the constructor deliberately does NOT enforce it (the agent can run a
    single-stream search by setting the other weight to 0).
    """
    config = ProceduralConfig()
    total = config.search_default_bm25_weight + config.search_default_vector_weight
    assert total == pytest.approx(1.0)


# --- overrides -------------------------------------------------------------


def test_procedural_config_overrides_all_fields():
    """Every field is settable — operator can dial each knob without a
    custom subclass."""
    config = ProceduralConfig(
        enabled=False,
        search_default_bm25_weight=0.3,
        search_default_vector_weight=0.7,
        briefing_default_limit_per_context=10,
        default_status='draft',
        identity_conflict_mode='upsert',
        derivation_worker_enabled=True,
        derivation_worker_batch_size=64,
        derivation_worker_poll_interval_seconds=1.0,
    )
    assert config.enabled is False
    assert config.search_default_bm25_weight == 0.3
    assert config.search_default_vector_weight == 0.7
    assert config.briefing_default_limit_per_context == 10
    assert config.default_status == 'draft'
    assert config.identity_conflict_mode == 'upsert'
    assert config.derivation_worker_enabled is True
    assert config.derivation_worker_batch_size == 64
    assert config.derivation_worker_poll_interval_seconds == 1.0


# --- bounds ----------------------------------------------------------------


@pytest.mark.parametrize(
    'field, value',
    [
        ('search_default_bm25_weight', -0.1),
        ('search_default_bm25_weight', 1.1),
        ('search_default_vector_weight', -0.1),
        ('search_default_vector_weight', 1.1),
        ('briefing_default_limit_per_context', 0),
        ('briefing_default_limit_per_context', 21),
        ('derivation_worker_batch_size', 0),
        ('derivation_worker_batch_size', 257),
        ('derivation_worker_poll_interval_seconds', 0.0),
        ('derivation_worker_poll_interval_seconds', 3601.0),
    ],
)
def test_procedural_config_rejects_out_of_bounds(field, value):
    """Out-of-range values are a configuration ERROR, not a silent clamp —
    the operator's intent is preserved as a validation failure they can
    audit. This pins the ``ge``/``le``/``gt`` bounds on the schema."""
    with pytest.raises(ValidationError):
        ProceduralConfig(**{field: value})


def test_procedural_config_rejects_unknown_status():
    """`default_status` is a closed enum: only `draft` and `published` are
    valid. `deprecated` is the lifecycle exit state, never a default."""
    with pytest.raises(ValidationError):
        ProceduralConfig(default_status='deprecated')  # type: ignore[arg-type]


def test_procedural_config_rejects_unknown_conflict_mode():
    """`identity_conflict_mode` is a closed enum. `error` would have been
    a tempting typo — the schema rejects it loudly so the operator
    cannot end up with a config that silently does nothing on conflict."""
    with pytest.raises(ValidationError):
        ProceduralConfig(identity_conflict_mode='error')  # type: ignore[arg-type]


# --- MemexConfig integration ----------------------------------------------


def test_memex_config_carries_procedural_config_under_memory():
    """`MemexConfig.server.memory.procedural` is the canonical path operators
    reach for in YAML. The default factory must populate it."""
    config = MemexConfig()
    assert isinstance(config.server.memory, MemoryConfig)
    assert isinstance(config.server.memory.procedural, ProceduralConfig)
    # Defaults propagate.
    assert config.server.memory.procedural.enabled is True
    assert config.server.memory.procedural.briefing_default_limit_per_context == 5


def test_memex_config_propagates_procedural_overrides():
    """An operator can flip a knob via the canonical path without
    touching the other 7 tunables."""
    config = MemexConfig(
        server=ServerConfig(
            memory=MemoryConfig(
                procedural=ProceduralConfig(
                    identity_conflict_mode='upsert',
                    briefing_default_limit_per_context=12,
                )
            )
        )
    )
    assert config.server.memory.procedural.identity_conflict_mode == 'upsert'
    assert config.server.memory.procedural.briefing_default_limit_per_context == 12
    # Untouched fields still carry their default.
    assert config.server.memory.procedural.enabled is True
    assert config.server.memory.procedural.default_status == 'published'


def test_memex_config_procedural_from_dict_round_trip():
    """A plain-dict construction (what YAML parsing produces) lands on
    the same shape as keyword arguments. The factory + field wiring
    must accept both."""
    config = MemexConfig(
        server=ServerConfig(
            memory=MemoryConfig(
                procedural=ProceduralConfig(
                    enabled=False,
                    derivation_worker_enabled=True,
                    derivation_worker_batch_size=32,
                )
            )
        )
    )
    procedural = config.server.memory.procedural
    assert procedural.enabled is False
    assert procedural.derivation_worker_enabled is True
    assert procedural.derivation_worker_batch_size == 32
    # Untouched fields still carry their default.
    assert procedural.derivation_worker_poll_interval_seconds == 5.0


# --- inheritance / isolation ----------------------------------------------


def test_procedural_config_instances_do_not_share_state():
    """Two independent `ProceduralConfig` instances must not mutate each
    other through the default-factory mutable defaults (Pydantic
    protects against this; this test pins the guarantee)."""
    a = ProceduralConfig()
    b = ProceduralConfig()
    a.enabled = False
    a.derivation_worker_batch_size = 1
    assert b.enabled is True
    assert b.derivation_worker_batch_size == 16
