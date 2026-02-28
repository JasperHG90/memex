"""Ingestion endpoints."""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import pathlib as plb
import shutil
import tempfile
from typing import Annotated, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Header,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse

from memex_common.exceptions import MemexError, ResourceNotFoundError
from memex_common.schemas import (
    BatchIngestRequest,
    BatchIngestResponse,
    BatchJobStatus,
    IngestFileRequest,
    IngestResponse,
    IngestURLRequest,
    NoteCreateDTO,
    WebhookPayload,
)

from memex_core.api import MemexAPI, NoteInput
from memex_core.server.common import _handle_error, get_api

logger = logging.getLogger('memex.core.server.ingestion')

router = APIRouter(prefix='/api/v1')


@router.post(
    '/ingestions',
    response_model=None,
    responses={200: {'model': IngestResponse}, 202: {'model': BatchJobStatus}},
)
async def ingest_note(
    request: Annotated[NoteCreateDTO, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    background: Annotated[bool, Query()] = False,
) -> IngestResponse | JSONResponse:
    """Ingest a note artifact."""
    try:
        # NoteCreateDTO now uses Base64 encoded bytes for content and files.
        # We MUST decode these to raw bytes for the internal NoteInput object
        # so that they are stored correctly (e.g., as raw images) in the filestore.

        if background:
            job_id = await api.batch_manager.create_job(notes=[request], vault_id=request.vault_id)
            return JSONResponse(
                status_code=202,
                content=BatchJobStatus(job_id=job_id, status='pending').model_dump(mode='json'),
            )

        try:
            decoded_content = base64.b64decode(request.content)
            decoded_files = {
                name: base64.b64decode(content) for name, content in request.files.items()
            }
        except binascii.Error:
            raise HTTPException(status_code=400, detail='Invalid Base64 encoding in note content')

        note = NoteInput(
            name=request.name,
            description=request.description,
            content=decoded_content,
            files=decoded_files,
            tags=request.tags,
            note_key=request.note_key,
        )
        result = await api.ingest(note, vault_id=request.vault_id)
        return IngestResponse(**result)

    except HTTPException:
        raise
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'NoteInput ingestion failed')


@router.post(
    '/ingestions/url',
    response_model=None,
    responses={200: {'model': IngestResponse}, 202: {'model': dict}},
)
async def ingest_url(
    request: Annotated[IngestURLRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    background: Annotated[bool, Query()] = False,
) -> IngestResponse | JSONResponse:
    try:
        # Decode assets if present
        assets_bytes = {}
        if request.assets:
            try:
                for name, content in request.assets.items():
                    assets_bytes[name] = base64.b64decode(content)
            except binascii.Error:
                raise HTTPException(status_code=400, detail='Invalid Base64 encoding in assets')

        if background:
            asyncio.create_task(
                api.ingest_from_url(
                    url=request.url,
                    vault_id=request.vault_id,
                    reflect_after=request.reflect_after,
                    assets=assets_bytes,
                )
            )
            return JSONResponse(status_code=202, content={'status': 'accepted'})

        result = await api.ingest_from_url(
            url=request.url,
            vault_id=request.vault_id,
            reflect_after=request.reflect_after,
            assets=assets_bytes,
        )
        return IngestResponse(**result)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'URL ingestion failed')


@router.post('/ingestions/file', response_model=IngestResponse)
async def ingest_file(
    request: Annotated[IngestFileRequest, Body()], api: Annotated[MemexAPI, Depends(get_api)]
):
    """Ingest content from a local file on the server."""
    try:
        result = await api.ingest_from_file(
            file_path=request.file_path,
            vault_id=request.vault_id,
            reflect_after=request.reflect_after,
        )
        return IngestResponse(**result)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'File ingestion failed')


@router.post(
    '/ingestions/upload',
    response_model=None,
    responses={200: {'model': IngestResponse}, 202: {'model': dict}},
)
async def ingest_upload(
    api: Annotated[MemexAPI, Depends(get_api)],
    files: list[UploadFile] = File(...),
    metadata: str | None = Body(None),
    background: bool = Query(False),
) -> IngestResponse | JSONResponse:
    """
    Upload and ingest files.
    - If it's a single non-markdown file, use MarkItDown.
    - If it's multiple files or a markdown file, treat as a native NoteInput.
    """
    try:
        parsed_metadata = json.loads(metadata) if metadata else {}
        # We look for NOTE.md, README.md, or the single file if it's .md
        main_content = b''
        aux_files = {}
        main_filename = ''
        # Priority: NOTE.md > README.md > index.md > first .md file
        md_files = [f for f in files if f.filename and f.filename.lower().endswith('.md')]

        if len(files) == 1 and not cast(str, files[0].filename).lower().endswith('.md'):
            uploaded_file = files[0]
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=plb.Path(cast(str, uploaded_file.filename)).suffix
            ) as tmp:
                shutil.copyfileobj(uploaded_file.file, tmp)
                tmp_path = tmp.name

            if background:

                async def _ingest_and_cleanup(path: str) -> None:
                    try:
                        await api.ingest_from_file(path)
                    finally:
                        if os.path.exists(path):
                            os.remove(path)

                asyncio.create_task(_ingest_and_cleanup(tmp_path))
                return JSONResponse(status_code=202, content={'status': 'accepted'})

            try:
                result = await api.ingest_from_file(tmp_path)
                return IngestResponse(**result)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        # Handle Native NoteInput construction
        priority_names = ['note.md', 'readme.md', 'index.md']
        best_candidate = None

        for name in priority_names:
            for f in files:
                if f.filename and f.filename.lower() == name:
                    best_candidate = f
                    break
            if best_candidate:
                break

        if not best_candidate and md_files:
            best_candidate = md_files[0]

        if not best_candidate and len(files) == 1:
            best_candidate = files[0]

        if not best_candidate:
            raise HTTPException(status_code=400, detail='Could not identify main content file.')

        for f in files:
            content = await f.read()
            if f == best_candidate:
                main_content = content
                if f.filename is not None:
                    main_filename = f.filename
            else:
                aux_files[f.filename] = content

        note = NoteInput(
            name=parsed_metadata.get('name') or plb.Path(main_filename).stem,
            description=parsed_metadata.get('description') or 'Uploaded NoteInput',
            content=main_content,
            files=aux_files,
            tags=parsed_metadata.get('tags', []),
        )

        if background:
            asyncio.create_task(api.ingest(note, vault_id=parsed_metadata.get('vault_id')))
            return JSONResponse(status_code=202, content={'status': 'accepted'})

        result = await api.ingest(note, vault_id=parsed_metadata.get('vault_id'))
        return IngestResponse(**result)

    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'File upload failed')


def _generate_webhook_note_key(source: str, content: str) -> str:
    """Generate an idempotent note_key from source + content hash."""
    digest = hashlib.sha256(f'{source}:{content}'.encode('utf-8')).hexdigest()
    return f'webhook:{source}:{digest}'


def _verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature of the raw request body."""
    expected = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post(
    '/ingestions/webhook',
    status_code=202,
    response_model=IngestResponse,
)
async def ingest_webhook(
    request: Request,
    api: Annotated[MemexAPI, Depends(get_api)],
    x_webhook_signature: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Ingest a note from an external webhook.

    Accepts a plain JSON payload (no Base64 encoding required).
    When a webhook secret is configured, the ``X-Webhook-Signature`` header
    must contain ``hex(HMAC-SHA256(secret, raw_body))``.

    Returns 202 Accepted with the ingestion result.
    """
    # Read raw body for HMAC validation (must happen before Pydantic parsing)
    raw_body = await request.body()

    # Validate HMAC signature if webhook secret is configured
    auth_config = getattr(request.app.state, 'auth_config', None)
    webhook_secret = (
        auth_config.webhook_secret.get_secret_value()
        if auth_config and auth_config.webhook_secret
        else None
    )

    if webhook_secret:
        if not x_webhook_signature:
            raise HTTPException(
                status_code=401,
                detail='Missing X-Webhook-Signature header.',
            )
        if not _verify_webhook_signature(raw_body, x_webhook_signature, webhook_secret):
            raise HTTPException(
                status_code=403,
                detail='Invalid webhook signature.',
            )
    else:
        logger.debug('Webhook secret not configured; skipping signature validation.')

    # Parse and validate payload
    try:
        payload = WebhookPayload.model_validate_json(raw_body)
    except (ValueError, KeyError):
        raise HTTPException(status_code=400, detail='Invalid webhook payload.')

    # Build NoteInput with auto-generated idempotent note_key
    note_key = _generate_webhook_note_key(payload.source, payload.content)
    note = NoteInput(
        name=payload.title,
        description=payload.description or payload.title,
        content=payload.content.encode('utf-8'),
        tags=payload.tags,
        note_key=note_key,
    )

    try:
        result = await api.ingest(note, vault_id=payload.vault_id)
        return JSONResponse(
            status_code=202,
            content=IngestResponse(**result).model_dump(mode='json'),
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Webhook ingestion failed')


@router.post('/ingestions/batch', response_model=BatchJobStatus, status_code=202)
async def ingest_batch(
    request: Annotated[BatchIngestRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """
    Asynchronously ingest a batch of notes.
    Returns 202 Accepted with a job_id for status tracking.
    """
    try:
        job_id = await api.batch_manager.create_job(
            notes=request.notes, vault_id=request.vault_id, batch_size=request.batch_size
        )
        return BatchJobStatus(job_id=job_id, status='pending')
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Batch ingestion job creation failed')


@router.get('/ingestions/{job_id}', response_model=BatchJobStatus)
async def get_batch_job_status(job_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Retrieve the current status and results of a batch ingestion job."""
    try:
        job = await api.batch_manager.get_job_status(job_id)
        if not job:
            raise ResourceNotFoundError(f'Batch job {job_id} not found.')

        result_dto = None
        if job.status == 'completed':
            result_dto = BatchIngestResponse(
                processed_count=job.processed_count,
                skipped_count=job.skipped_count,
                failed_count=job.failed_count,
                note_ids=job.note_ids,
                errors=job.error_info or [],
            )

        return BatchJobStatus(
            job_id=job.id, status=job.status, progress=job.progress, result=result_dto
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to retrieve batch job status')
