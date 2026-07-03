"""Case endpoints (§5.1 / §18.3 / §18.9.0).

* ``POST /api/v1/cases`` — submit a worked episode as a NOTE (``role='case'``)
  filed into the hidden ``procedural`` system vault. The response carries the
  assignment outcome (explicit / auto_assigned / new_procedure_draft /
  escalated) so the submitting agent knows whether a lint finding needs its
  attention (file-then-lint, decision #5).
* ``GET /api/v1/cases`` — list case notes in the procedural system vault,
  with filters on the provenance stamped at submission time.
* ``GET /api/v1/cases/{note_id}`` — get a single case note by ID.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query
from fastapi.responses import JSONResponse

from memex_common.exceptions import MemexError
from memex_common.procedural_schemas import CaseSubmit, CaseSubmitResult
from memex_common.schemas import BatchJobStatus, NoteDTO, NoteListItemDTO
from memex_core.api import MemexAPI
from memex_core.server.auth import (
    AuthContext,
    Permission,
    check_vault_access,
    get_auth_context,
    require_read,
    require_write,
)
from memex_core.server.common import (
    _handle_error,
    build_note_dto,
    build_note_list_item_dto,
    get_api,
)
from memex_core.tracing import trace_span

router = APIRouter(prefix='/api/v1')


@router.post(
    '/cases',
    response_model=None,
    responses={
        200: {'model': CaseSubmitResult},
        202: {'model': BatchJobStatus},
    },
    dependencies=[Depends(require_write)],
)
async def case_submit(
    request: Annotated[CaseSubmit, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    background_tasks: BackgroundTasks,
    background: Annotated[bool, Query()] = False,
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> CaseSubmitResult | JSONResponse:
    """Submit a worked episode as a case.

    The note is composed from the §5.1 episode template and filed into
    the hidden system vault with ``role='case'``. Assignment runs
    synchronously: explicit ``case_of`` wins; otherwise the judge
    auto-assigns on clean separation and escalates everything else to
    the lint queue (the response's ``assignment.finding_id``). The
    response carries the assignment outcome so the submitting agent
    knows whether a lint finding needs its attention.

    ``background=true`` runs the whole ingest+stamp+assign flow off the
    request path as a tracked job and returns ``202`` with a
    ``BatchJobStatus.job_id`` you can poll at ``GET /api/v1/ingestions/
    {job_id}`` (the case note id lands in the job's ``note_ids`` on
    completion; a failure is recorded on the job, not swallowed). The
    assignment outcome is NOT part of the job — observe escalations and
    new-procedure drafts via the lint queue. Validation that must fail
    fast (a bad ``case_of``) still runs synchronously below.
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

    if background:
        # Durable tracked job (not fire-and-forget): the full submit runs under
        # a BatchJob, pollable at GET /api/v1/ingestions/{job_id}; failures land
        # on the job row. submit_job returns {'note_id': ...} so the job records
        # the filed case note.
        job_id = await api.batch_manager.create_single_job(
            api.cases.submit_job,
            vault_id=None,
            background_tasks=background_tasks,
            request=request,
        )
        return JSONResponse(
            status_code=202,
            content=BatchJobStatus(job_id=job_id, status='pending').model_dump(mode='json'),
        )

    with trace_span(
        'memex_core.procedural',
        'cases.submit',
        {'case.outcome': request.outcome},
    ):
        try:
            return await api.cases.submit(request)
        except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
            raise _handle_error(e, 'Failed to submit case')


@router.get(
    '/cases',
    response_model=list[NoteListItemDTO],
    dependencies=[Depends(require_read)],
)
async def case_list(
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    outcome: Annotated[
        str | None,
        Query(description='Filter by case outcome: success | failure | mixed.'),
    ] = None,
    tags: Annotated[
        list[str] | None,
        Query(description='Filter by tags (AND semantics).'),
    ] = None,
    project_id: Annotated[
        str | None,
        Query(description='Filter by the project_id recorded in provenance.'),
    ] = None,
    case_of: Annotated[
        UUID | None,
        Query(description='Filter by the procedural entry UUID this case instantiates.'),
    ] = None,
    submitted_by: Annotated[
        str | None,
        Query(description='Filter by the submitting agent identity.'),
    ] = None,
    slim: Annotated[
        bool,
        Query(description='Drop per-note summaries to keep responses under hook caps.'),
    ] = False,
    sort: Annotated[
        Literal['-created_at', 'created_at'] | None,
        Query(description='Sort by created_at. Use -created_at for newest first.'),
    ] = None,
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> list[NoteListItemDTO]:
    """List case notes (``role='case'``) in the hidden procedural system vault.

    The procedural system vault is implicit — the caller never names it.
    Vault-restricted API keys are checked against it; keys with no vault
    restriction see all cases.

    Query params:
    - sort: Optional sort. -created_at for newest first (default), created_at
      for oldest first. When omitted, the default is COALESCE(publish_date,
      created_at) DESC for backward compatibility with note-list behavior.
    """
    try:
        vault_id = await api.cases._resolve_case_vault()
    except Exception as e:
        raise _handle_error(e, 'Failed to resolve case vault')

    await check_vault_access(auth, [vault_id], api, permission=Permission.READ)

    try:
        notes = await api.cases.list_cases(
            limit=limit,
            offset=offset,
            outcome=outcome,
            tags=tags,
            project_id=project_id,
            case_of=case_of,
            submitted_by=submitted_by,
            slim=slim,
            sort=sort,
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to list cases')

    return [build_note_list_item_dto(note) for note in notes]


@router.get(
    '/cases/{note_id}',
    response_model=NoteDTO,
    dependencies=[Depends(require_read)],
)
async def case_get(
    note_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> NoteDTO:
    """Get a single case note by ID.

    The note must live in the hidden procedural system vault and have
    ``role='case'``; otherwise the endpoint returns 404 so the case surface
    does not leak arbitrary note IDs.
    """
    try:
        vault_id = await api.cases._resolve_case_vault()
    except Exception as e:
        raise _handle_error(e, 'Failed to resolve case vault')

    await check_vault_access(auth, [vault_id], api, permission=Permission.READ)

    try:
        note = await api.cases.get_case(note_id)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get case')

    return build_note_dto(note)
