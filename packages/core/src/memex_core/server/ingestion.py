"""Ingestion endpoints."""

import base64
import binascii
import json
import os
import pathlib as plb
import shutil
import tempfile
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile

from memex_common.exceptions import ResourceNotFoundError
from memex_common.schemas import (
    BatchIngestRequest,
    BatchIngestResponse,
    BatchJobStatus,
    IngestFileRequest,
    IngestResponse,
    IngestURLRequest,
    NoteDTO,
)

from memex_core.api import MemexAPI, Note
from memex_core.server.common import _handle_error, get_api

router = APIRouter(prefix='/api/v1')


@router.post('/ingest', response_model=IngestResponse)
async def ingest_note(
    request: Annotated[NoteDTO, Body()], api: Annotated[MemexAPI, Depends(get_api)]
):
    """Ingest a note artifact."""
    try:
        # NoteDTO now uses Base64 encoded bytes for content and files.
        # We MUST decode these to raw bytes for the internal Note object
        # so that they are stored correctly (e.g., as raw images) in the filestore.

        decoded_content = base64.b64decode(request.content)
        decoded_files = {name: base64.b64decode(content) for name, content in request.files.items()}

        note = Note(
            name=request.name,
            description=request.description,
            content=decoded_content,
            files=decoded_files,
            tags=request.tags,
            document_key=request.document_key,
        )
        result = await api.ingest(note, vault_id=request.vault_id)
        return IngestResponse(**result)

    except Exception as e:
        raise _handle_error(e, 'Note ingestion failed')


@router.post('/ingest/url', response_model=IngestResponse)
async def ingest_url(
    request: Annotated[IngestURLRequest, Body()], api: Annotated[MemexAPI, Depends(get_api)]
):
    try:
        # Decode assets if present
        assets_bytes = {}
        if request.assets:
            try:
                for name, content in request.assets.items():
                    assets_bytes[name] = base64.b64decode(content)
            except binascii.Error:
                raise HTTPException(status_code=400, detail='Invalid Base64 encoding in assets')

        result = await api.ingest_from_url(
            url=request.url,
            vault_id=request.vault_id,
            reflect_after=request.reflect_after,
            assets=assets_bytes,
        )
        return IngestResponse(**result)
    except Exception as e:
        raise _handle_error(e, 'URL ingestion failed')


@router.post('/ingest/file', response_model=IngestResponse)
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
    except Exception as e:
        raise _handle_error(e, 'File ingestion failed')


@router.post('/ingest/upload', response_model=IngestResponse)
async def ingest_upload(
    api: Annotated[MemexAPI, Depends(get_api)],
    files: list[UploadFile] = File(...),
    metadata: str | None = Body(None),
):
    """
    Upload and ingest files.
    - If it's a single non-markdown file, use MarkItDown.
    - If it's multiple files or a markdown file, treat as a native Note.
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

            try:
                result = await api.ingest_from_file(tmp_path)
                return IngestResponse(**result)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        # Handle Native Note construction
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

        note = Note(
            name=parsed_metadata.get('name') or plb.Path(main_filename).stem,
            description=parsed_metadata.get('description') or 'Uploaded Note',
            content=main_content,
            files=aux_files,
            tags=parsed_metadata.get('tags', []),
        )

        result = await api.ingest(note, vault_id=parsed_metadata.get('vault_id'))
        return IngestResponse(**result)

    except Exception as e:
        raise _handle_error(e, 'File upload failed')


@router.post('/ingest/batch', response_model=BatchJobStatus, status_code=202)
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
    except Exception as e:
        raise _handle_error(e, 'Batch ingestion job creation failed')


@router.get('/ingest/batch/{job_id}', response_model=BatchJobStatus)
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
                document_ids=job.document_ids,
                errors=job.error_info or [],
            )

        return BatchJobStatus(
            job_id=job.id, status=job.status, progress=job.progress, result=result_dto
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to retrieve batch job status')
