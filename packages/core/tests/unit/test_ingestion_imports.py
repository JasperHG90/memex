"""Defense-in-depth import-correctness tests for `server.ingestion`.

The 409 response body for the batch endpoint must conform to the Pydantic
``BatchJobStatus`` model in ``memex_common.schemas`` — NOT to the SQLModel
``(str, Enum)`` of the same name in ``memex_core.memory.sql_models``.

The two share a name but live in different modules and represent different
things:

- ``memex_common.schemas.BatchJobStatus`` (`schemas.py:778`) — the Pydantic
  ``BaseModel`` used as the route's `response_model`.
- ``memex_core.memory.sql_models.BatchJobStatus`` (`sql_models.py:964`) —
  the SQL row-status enum that's stored on the ``BatchJob.status`` column.

A wrong import would make 409 responses serialize as just an enum value
(``"processing"``) instead of the full ``{job_id, status, ...}`` body the
spec promises. RFC-002 §"HTTP layer (AC-021)" calls this out explicitly;
the test here statically guards against the regression.
"""

from __future__ import annotations

import memex_common.schemas as schemas_module
import memex_core.memory.sql_models as sql_models_module
from memex_core.server import ingestion as ingestion_module


def test_server_ingestion_uses_pydantic_batch_job_status():
    """The endpoint's `BatchJobStatus` must be the Pydantic schema."""
    assert ingestion_module.BatchJobStatus is schemas_module.BatchJobStatus


def test_server_ingestion_does_not_use_sql_enum_batch_job_status():
    """And it must NOT be the SQLModel enum (defense-in-depth: catches a
    future wrong-import regression)."""
    assert ingestion_module.BatchJobStatus is not sql_models_module.BatchJobStatus


def test_pydantic_and_sql_batch_job_status_are_distinct_types():
    """Sanity: the two `BatchJobStatus` symbols are genuinely different. If a
    refactor ever unified them this test would fail loud and force the author
    to revisit AC-021's distinction."""
    assert schemas_module.BatchJobStatus is not sql_models_module.BatchJobStatus
