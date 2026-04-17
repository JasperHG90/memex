"""Tests that temporal decay exponents are clamped to prevent Postgres NUMERIC underflow.

Historic event dates (e.g. 1970-01-01, 1945-05-08) produce very large negative exponents
in power(base, -(days_diff / decay_days)). Without clamping, this causes:

    NumericValueOutOfRangeError: value out of range: underflow

The fix uses GREATEST(..., -996) to clamp the exponent. power(2, -996) ~ 1e-300 which is
near the minimum representable Postgres NUMERIC value.
"""

from memex_core.memory.retrieval.strategies import (
    EntityCooccurrenceGraphStrategy,
    EntityCooccurrenceNoteGraphStrategy,
    CausalGraphStrategy,
    CausalNoteGraphStrategy,
    _MAX_DECAY_EXPONENT,
)


def _compile_sql(stmt) -> str:
    """Compile a SQLAlchemy statement to its string representation."""
    return str(stmt.compile(compile_kwargs={'literal_binds': True}))


def test_max_decay_exponent_value():
    """Verify the constant is set to prevent underflow for base=2."""
    # power(2, -996) ~ 1e-300, which is representable in Postgres NUMERIC.
    # power(2, -997) would underflow on some Postgres versions.
    assert _MAX_DECAY_EXPONENT == -996


class TestEntityCooccurrenceGraphStrategyClamp:
    def test_temporal_decay_uses_greatest_clamp(self):
        strategy = EntityCooccurrenceGraphStrategy()
        stmt = strategy.get_statement('test query', None)
        sql = _compile_sql(stmt)
        assert 'greatest' in sql.lower(), (
            'EntityCooccurrenceGraphStrategy must clamp temporal decay exponent with GREATEST'
        )

    def test_clamp_value_in_sql(self):
        strategy = EntityCooccurrenceGraphStrategy()
        stmt = strategy.get_statement('test query', None)
        sql = _compile_sql(stmt)
        assert str(_MAX_DECAY_EXPONENT) in sql, (
            f'SQL must contain the clamp value {_MAX_DECAY_EXPONENT}'
        )


class TestEntityCooccurrenceNoteGraphStrategyClamp:
    def test_temporal_decay_uses_greatest_clamp(self):
        strategy = EntityCooccurrenceNoteGraphStrategy()
        stmt = strategy.get_statement('test query', None)
        sql = _compile_sql(stmt)
        assert 'greatest' in sql.lower(), (
            'EntityCooccurrenceNoteGraphStrategy must clamp temporal decay exponent with GREATEST'
        )


class TestCausalGraphStrategyClamp:
    def test_temporal_decay_uses_greatest_clamp(self):
        strategy = CausalGraphStrategy()
        stmt = strategy.get_statement('test query', None)
        sql = _compile_sql(stmt)
        assert 'greatest' in sql.lower(), (
            'CausalGraphStrategy must clamp temporal decay exponent with GREATEST'
        )


class TestCausalNoteGraphStrategyClamp:
    def test_temporal_decay_uses_greatest_clamp(self):
        strategy = CausalNoteGraphStrategy()
        stmt = strategy.get_statement('test query', None)
        sql = _compile_sql(stmt)
        assert 'greatest' in sql.lower(), (
            'CausalNoteGraphStrategy must clamp temporal decay exponent with GREATEST'
        )
