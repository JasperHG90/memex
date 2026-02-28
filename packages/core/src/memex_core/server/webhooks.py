"""Webhook management CRUD endpoints."""

import logging
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, HttpUrl

from memex_core.memory.sql_models import WebhookEvent
from memex_core.services.webhook_service import WebhookService

logger = logging.getLogger('memex.core.server.webhooks')

router = APIRouter(prefix='/api/v1/webhooks')

VALID_EVENTS = {e.value for e in WebhookEvent}


# ------------------------------------------------------------------
# DTOs
# ------------------------------------------------------------------


class WebhookCreateRequest(BaseModel):
    url: HttpUrl = Field(description='The HTTPS URL to deliver events to.')
    secret: str = Field(min_length=16, max_length=255, description='Shared secret for signing.')
    events: list[str] = Field(min_length=1, description='Event types to subscribe to.')
    active: bool = Field(default=True, description='Whether the webhook starts enabled.')


class WebhookUpdateRequest(BaseModel):
    url: HttpUrl | None = Field(default=None, description='New URL.')
    secret: str | None = Field(
        default=None, min_length=16, max_length=255, description='New secret.'
    )
    events: list[str] | None = Field(default=None, min_length=1, description='New event list.')
    active: bool | None = Field(default=None, description='Enable or disable.')


class WebhookDTO(BaseModel):
    id: UUID
    url: str
    events: list[str]
    active: bool
    created_at: datetime


class WebhookDeliveryDTO(BaseModel):
    id: UUID
    webhook_id: UUID
    event: str
    payload: dict[str, Any]
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _get_webhook_service(request: Request) -> WebhookService:
    return request.app.state.webhook_service


def _validate_events(events: list[str]) -> None:
    invalid = set(events) - VALID_EVENTS
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f'Invalid event types: {sorted(invalid)}. Valid: {sorted(VALID_EVENTS)}',
        )


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@router.post('', response_model=WebhookDTO, status_code=201)
async def create_webhook(body: WebhookCreateRequest, request: Request) -> WebhookDTO:
    """Register a new webhook endpoint."""
    _validate_events(body.events)
    svc = _get_webhook_service(request)
    webhook = await svc.create(
        url=str(body.url),
        secret=body.secret,
        events=body.events,
        active=body.active,
    )
    return WebhookDTO(
        id=webhook.id,
        url=webhook.url,
        events=webhook.events,
        active=webhook.active,
        created_at=webhook.created_at,
    )


@router.get('', response_model=list[WebhookDTO])
async def list_webhooks(
    request: Request,
    active_only: Annotated[bool, Query(description='Only return active webhooks.')] = False,
) -> list[WebhookDTO]:
    """List all registered webhooks."""
    svc = _get_webhook_service(request)
    webhooks = await svc.list_all(active_only=active_only)
    return [
        WebhookDTO(
            id=w.id,
            url=w.url,
            events=w.events,
            active=w.active,
            created_at=w.created_at,
        )
        for w in webhooks
    ]


@router.get('/{webhook_id}', response_model=WebhookDTO)
async def get_webhook(webhook_id: UUID, request: Request) -> WebhookDTO:
    """Get a webhook by ID."""
    svc = _get_webhook_service(request)
    webhook = await svc.get(webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail='Webhook not found.')
    return WebhookDTO(
        id=webhook.id,
        url=webhook.url,
        events=webhook.events,
        active=webhook.active,
        created_at=webhook.created_at,
    )


@router.patch('/{webhook_id}', response_model=WebhookDTO)
async def update_webhook(
    webhook_id: UUID,
    body: WebhookUpdateRequest,
    request: Request,
) -> WebhookDTO:
    """Update an existing webhook."""
    if body.events is not None:
        _validate_events(body.events)
    svc = _get_webhook_service(request)
    webhook = await svc.update(
        webhook_id,
        url=str(body.url) if body.url is not None else None,
        secret=body.secret,
        events=body.events,
        active=body.active,
    )
    if webhook is None:
        raise HTTPException(status_code=404, detail='Webhook not found.')
    return WebhookDTO(
        id=webhook.id,
        url=webhook.url,
        events=webhook.events,
        active=webhook.active,
        created_at=webhook.created_at,
    )


@router.delete('/{webhook_id}', status_code=204)
async def delete_webhook(webhook_id: UUID, request: Request) -> None:
    """Delete a webhook registration."""
    svc = _get_webhook_service(request)
    deleted = await svc.delete(webhook_id)
    if not deleted:
        raise HTTPException(status_code=404, detail='Webhook not found.')


@router.get('/{webhook_id}/deliveries', response_model=list[WebhookDeliveryDTO])
async def list_deliveries(
    webhook_id: UUID,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500, description='Max deliveries.')] = 50,
    offset: Annotated[int, Query(ge=0, description='Pagination offset.')] = 0,
) -> list[WebhookDeliveryDTO]:
    """List delivery records for a webhook."""
    svc = _get_webhook_service(request)
    deliveries = await svc.list_deliveries(webhook_id, limit=limit, offset=offset)
    return [
        WebhookDeliveryDTO(
            id=d.id,
            webhook_id=d.webhook_id,
            event=d.event,
            payload=d.payload,
            status=d.status,
            attempts=d.attempts,
            last_error=d.last_error,
            created_at=d.created_at,
        )
        for d in deliveries
    ]
