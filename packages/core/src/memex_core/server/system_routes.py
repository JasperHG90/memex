"""System endpoints — admin-only inspection of the resolved server config.

Used by the eval suite to capture a reproducible snapshot of every knob
that influences a benchmark run. The snapshot is run through
``memex_common.redaction.redact`` before serialization so secret-bearing
fields cannot leak via this surface.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from memex_common.redaction import redact

from memex_core.api import MemexAPI
from memex_core.server.auth import require_admin_auth
from memex_core.server.common import get_api

logger = logging.getLogger('memex.core.server')

router = APIRouter(
    prefix='/api/v1/system',
    dependencies=[Depends(require_admin_auth)],
)


@router.get('/config')
async def get_system_config(api: Annotated[MemexAPI, Depends(get_api)]) -> dict[str, Any]:
    """Return the resolved server config with secrets redacted.

    The shape mirrors ``MemexConfig.model_dump(mode='json')`` — every leaf is
    JSON-friendly. Pydantic v2 already serializes ``SecretStr`` to
    ``'**********'`` in JSON mode; ``redact`` is layered on top as
    defense-in-depth and to add ``<key>_set`` siblings for "configured?"
    introspection.

    The redacted shape is intentionally untyped (returned as ``dict``) so
    we can extend ``MemexConfig`` without coordinating an API model bump
    every release.
    """
    raw = api.config.model_dump(mode='json')
    return redact(raw)
