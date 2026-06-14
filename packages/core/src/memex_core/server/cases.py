"""Case submission endpoint (§5.1 / §18.3 / §18.9.0).

One route: ``POST /api/v1/cases``. A case is a NOTE (``role='case'``)
filed into the hidden ``procedural`` system vault — the caller never
names the vault. The response carries the assignment outcome
(explicit / auto_assigned / new_procedure_draft / escalated) so the
submitting agent knows whether a lint finding needs its attention
(file-then-lint, decision #5 — the agent may resolve it via the lint
tools or leave it for human review).
"""

from typing import Annotated

from fastapi import APIRouter, Body, Depends

from memex_common.exceptions import MemexError
from memex_common.procedural_schemas import CaseSubmit, CaseSubmitResult
from memex_core.api import MemexAPI
from memex_core.server.auth import (
    AuthContext,
    Permission,
    check_vault_access,
    get_auth_context,
    require_write,
)
from memex_core.server.common import _handle_error, get_api
from memex_core.tracing import trace_span

router = APIRouter(prefix='/api/v1')


@router.post(
    '/cases',
    response_model=CaseSubmitResult,
    dependencies=[Depends(require_write)],
)
async def case_submit(
    request: Annotated[CaseSubmit, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
):
    """Submit a worked episode as a case.

    The note is composed from the §5.1 episode template and filed into
    the hidden system vault with ``role='case'``. Assignment runs
    synchronously: explicit ``case_of`` wins; otherwise the judge
    auto-assigns on clean separation and escalates everything else to
    the lint queue (the response's ``assignment.finding_id``).
    """
    # An explicit ``case_of`` links this case to an existing procedure and
    # MUTATES it (provenance edge + outcome counter + derivation enqueue). A
    # vault-restricted key must own the referenced procedure's vault, else it
    # could nudge another tenant's procedure by citing its UUID.
    if request.case_of is not None:
        try:
            target = await api.procedural.get(request.case_of, vault_id=None)
        except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
            raise _handle_error(e, 'Failed to resolve case_of procedure')
        await check_vault_access(auth, [target.vault_id], api, permission=Permission.WRITE)
    with trace_span(
        'memex_core.procedural',
        'cases.submit',
        {'case.outcome': request.outcome},
    ):
        try:
            return await api.cases.submit(request)
        except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
            raise _handle_error(e, 'Failed to submit case')
