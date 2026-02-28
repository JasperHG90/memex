"""Webhook delivery service with HMAC-SHA256 signing and retry."""

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select

from memex_core.memory.sql_models import (
    DeliveryStatus,
    WebhookDelivery,
    WebhookRegistration,
)
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.webhook')

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 2.0
DELIVERY_TIMEOUT_SECONDS = 10.0


def compute_signature(payload_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 hex digest for a payload."""
    return hmac.new(
        secret.encode('utf-8'),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()


class WebhookService:
    """Manages webhook CRUD and async delivery with retry + exponential backoff."""

    def __init__(self, metastore: AsyncBaseMetaStoreEngine) -> None:
        self._metastore = metastore

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        url: str,
        secret: str,
        events: list[str],
        active: bool = True,
    ) -> WebhookRegistration:
        """Register a new webhook."""
        webhook = WebhookRegistration(
            url=url,
            secret=secret,
            events=events,
            active=active,
        )
        async with self._metastore.session() as session:
            session.add(webhook)
            await session.commit()
            await session.refresh(webhook)
        return webhook

    async def get(self, webhook_id: UUID) -> WebhookRegistration | None:
        """Retrieve a webhook by ID."""
        async with self._metastore.session() as session:
            result = await session.exec(
                select(WebhookRegistration).where(WebhookRegistration.id == webhook_id)
            )
            return result.first()

    async def list_all(self, *, active_only: bool = False) -> list[WebhookRegistration]:
        """List all registered webhooks."""
        stmt = select(WebhookRegistration)
        if active_only:
            stmt = stmt.where(WebhookRegistration.active.is_(True))  # type: ignore[union-attr]
        async with self._metastore.session() as session:
            result = await session.exec(stmt)
            return list(result.all())

    async def update(
        self,
        webhook_id: UUID,
        *,
        url: str | None = None,
        secret: str | None = None,
        events: list[str] | None = None,
        active: bool | None = None,
    ) -> WebhookRegistration | None:
        """Update an existing webhook. Returns None if not found."""
        async with self._metastore.session() as session:
            result = await session.exec(
                select(WebhookRegistration).where(WebhookRegistration.id == webhook_id)
            )
            webhook = result.first()
            if webhook is None:
                return None
            if url is not None:
                webhook.url = url
            if secret is not None:
                webhook.secret = secret
            if events is not None:
                webhook.events = events
            if active is not None:
                webhook.active = active
            session.add(webhook)
            await session.commit()
            await session.refresh(webhook)
            return webhook

    async def delete(self, webhook_id: UUID) -> bool:
        """Delete a webhook. Returns True if it existed."""
        async with self._metastore.session() as session:
            result = await session.exec(
                select(WebhookRegistration).where(WebhookRegistration.id == webhook_id)
            )
            webhook = result.first()
            if webhook is None:
                return False
            await session.delete(webhook)
            await session.commit()
            return True

    async def list_deliveries(
        self,
        webhook_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WebhookDelivery]:
        """List delivery records for a webhook."""
        stmt = (
            select(WebhookDelivery)
            .where(WebhookDelivery.webhook_id == webhook_id)
            .order_by(WebhookDelivery.created_at.desc())  # type: ignore[union-attr]
            .limit(limit)
            .offset(offset)
        )
        async with self._metastore.session() as session:
            result = await session.exec(stmt)
            return list(result.all())

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def fire_event(self, event: str, payload: dict[str, Any]) -> None:
        """Fire an event to all active webhooks subscribed to it.

        Delivery happens in background tasks so the caller is never blocked.
        """
        webhooks = await self._get_subscribers(event)
        for webhook in webhooks:
            asyncio.create_task(self._deliver(webhook, event, payload))

    async def _get_subscribers(self, event: str) -> list[WebhookRegistration]:
        """Find active webhooks that subscribe to the given event."""
        all_active = await self.list_all(active_only=True)
        return [w for w in all_active if event in w.events]

    async def _deliver(
        self,
        webhook: WebhookRegistration,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        """Deliver a payload to a webhook with retry and exponential backoff."""
        delivery = WebhookDelivery(
            webhook_id=webhook.id,
            event=event,
            payload=payload,
            status=DeliveryStatus.PENDING,
        )
        async with self._metastore.session() as session:
            session.add(delivery)
            await session.commit()
            await session.refresh(delivery)

        payload_bytes = json.dumps(payload, default=str).encode('utf-8')
        signature = compute_signature(payload_bytes, webhook.secret)

        headers = {
            'Content-Type': 'application/json',
            'X-Memex-Event': event,
            'X-Memex-Signature': f'sha256={signature}',
        }

        last_error: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
                    resp = await client.post(
                        webhook.url,
                        content=payload_bytes,
                        headers=headers,
                    )
                    resp.raise_for_status()

                # Success
                await self._update_delivery(
                    delivery.id,
                    status=DeliveryStatus.SUCCESS,
                    attempts=attempt,
                    last_error=None,
                )
                logger.info(
                    'Webhook delivered: webhook=%s event=%s attempt=%d',
                    webhook.id,
                    event,
                    attempt,
                )
                return

            except Exception as exc:
                last_error = f'{type(exc).__name__}: {exc}'
                logger.warning(
                    'Webhook delivery failed: webhook=%s event=%s attempt=%d error=%s',
                    webhook.id,
                    event,
                    attempt,
                    last_error,
                )
                if attempt < MAX_ATTEMPTS:
                    backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    await asyncio.sleep(backoff)

        # All attempts exhausted
        await self._update_delivery(
            delivery.id,
            status=DeliveryStatus.FAILED,
            attempts=MAX_ATTEMPTS,
            last_error=last_error,
        )
        logger.error(
            'Webhook delivery exhausted retries: webhook=%s event=%s',
            webhook.id,
            event,
        )

    async def _update_delivery(
        self,
        delivery_id: UUID,
        *,
        status: str,
        attempts: int,
        last_error: str | None,
    ) -> None:
        """Update a delivery record after an attempt."""
        async with self._metastore.session() as session:
            result = await session.exec(
                select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
            )
            delivery = result.one()
            delivery.status = status
            delivery.attempts = attempts
            delivery.last_error = last_error
            session.add(delivery)
            await session.commit()
