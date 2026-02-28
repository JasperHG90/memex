"""Unit tests for the webhook service and endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from memex_core.memory.sql_models import (
    DeliveryStatus,
    WebhookDelivery,
    WebhookEvent,
    WebhookRegistration,
)
from memex_core.services.webhook_service import (
    MAX_ATTEMPTS,
    WebhookService,
    compute_signature,
)


# ---------------------------------------------------------------------------
# compute_signature
# ---------------------------------------------------------------------------


class TestComputeSignature:
    def test_basic_signature(self) -> None:
        payload = b'{"event": "test"}'
        secret = 'test-secret-key-1234'
        sig = compute_signature(payload, secret)
        expected = hmac.new(secret.encode('utf-8'), payload, hashlib.sha256).hexdigest()
        assert sig == expected

    def test_different_secrets_produce_different_signatures(self) -> None:
        payload = b'same payload'
        sig1 = compute_signature(payload, 'secret-one-abcdef')
        sig2 = compute_signature(payload, 'secret-two-ghijkl')
        assert sig1 != sig2

    def test_different_payloads_produce_different_signatures(self) -> None:
        secret = 'same-secret-1234567'
        sig1 = compute_signature(b'payload-one', secret)
        sig2 = compute_signature(b'payload-two', secret)
        assert sig1 != sig2

    def test_deterministic(self) -> None:
        payload = b'deterministic test'
        secret = 'deterministic-secret'
        assert compute_signature(payload, secret) == compute_signature(payload, secret)


# ---------------------------------------------------------------------------
# WebhookRegistration model
# ---------------------------------------------------------------------------


class TestWebhookRegistrationModel:
    def test_defaults(self) -> None:
        webhook = WebhookRegistration(
            url='https://example.com/hook',
            secret='my-secret-key-12345',
            events=['ingestion.completed'],
        )
        assert webhook.active is True
        assert webhook.id is not None

    def test_event_enum_values(self) -> None:
        assert WebhookEvent.INGESTION_COMPLETED == 'ingestion.completed'
        assert WebhookEvent.REFLECTION_COMPLETED == 'reflection.completed'


class TestDeliveryStatusEnum:
    def test_values(self) -> None:
        assert DeliveryStatus.PENDING == 'pending'
        assert DeliveryStatus.SUCCESS == 'success'
        assert DeliveryStatus.FAILED == 'failed'


# ---------------------------------------------------------------------------
# WebhookService — CRUD (mocked metastore)
# ---------------------------------------------------------------------------


def _make_mock_metastore() -> MagicMock:
    """Create a mock metastore with a session context manager."""
    metastore = MagicMock()
    session = AsyncMock()
    session.add = MagicMock()
    session.delete = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    # Make session a proper async context manager
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore.session.return_value = ctx
    return metastore


class TestWebhookServiceCreate:
    @pytest.mark.asyncio
    async def test_create_webhook(self) -> None:
        metastore = _make_mock_metastore()
        svc = WebhookService(metastore)

        async with metastore.session() as session:
            # refresh will populate fields
            async def fake_refresh(obj: WebhookRegistration) -> None:
                obj.id = uuid4()
                obj.created_at = datetime.now(timezone.utc)

            session.refresh = AsyncMock(side_effect=fake_refresh)

        webhook = await svc.create(
            url='https://example.com/hook',
            secret='test-secret-key-1234',
            events=['ingestion.completed'],
        )
        assert webhook.url == 'https://example.com/hook'
        assert webhook.events == ['ingestion.completed']
        assert webhook.active is True


class TestWebhookServiceList:
    @pytest.mark.asyncio
    async def test_list_all(self) -> None:
        metastore = _make_mock_metastore()
        svc = WebhookService(metastore)

        w1 = WebhookRegistration(
            url='https://a.com/hook',
            secret='secret-aaaa-1234',
            events=['ingestion.completed'],
        )
        w2 = WebhookRegistration(
            url='https://b.com/hook',
            secret='secret-bbbb-5678',
            events=['reflection.completed'],
            active=False,
        )

        async with metastore.session() as session:
            mock_result = MagicMock()
            mock_result.all.return_value = [w1, w2]
            session.exec = AsyncMock(return_value=mock_result)

        result = await svc.list_all()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_active_only(self) -> None:
        metastore = _make_mock_metastore()
        svc = WebhookService(metastore)

        w1 = WebhookRegistration(
            url='https://a.com/hook',
            secret='secret-aaaa-1234',
            events=['ingestion.completed'],
        )

        async with metastore.session() as session:
            mock_result = MagicMock()
            mock_result.all.return_value = [w1]
            session.exec = AsyncMock(return_value=mock_result)

        result = await svc.list_all(active_only=True)
        assert len(result) == 1


class TestWebhookServiceDelete:
    @pytest.mark.asyncio
    async def test_delete_existing(self) -> None:
        metastore = _make_mock_metastore()
        svc = WebhookService(metastore)

        webhook = WebhookRegistration(
            url='https://a.com/hook',
            secret='secret-aaaa-1234',
            events=['ingestion.completed'],
        )

        async with metastore.session() as session:
            mock_result = MagicMock()
            mock_result.first.return_value = webhook
            session.exec = AsyncMock(return_value=mock_result)

        result = await svc.delete(webhook.id)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self) -> None:
        metastore = _make_mock_metastore()
        svc = WebhookService(metastore)

        async with metastore.session() as session:
            mock_result = MagicMock()
            mock_result.first.return_value = None
            session.exec = AsyncMock(return_value=mock_result)

        result = await svc.delete(uuid4())
        assert result is False


# ---------------------------------------------------------------------------
# WebhookService — delivery
# ---------------------------------------------------------------------------


class TestWebhookServiceDelivery:
    @pytest.mark.asyncio
    async def test_successful_delivery(self) -> None:
        metastore = _make_mock_metastore()
        svc = WebhookService(metastore)

        webhook = WebhookRegistration(
            id=uuid4(),
            url='https://example.com/hook',
            secret='test-secret-key-1234',
            events=['ingestion.completed'],
        )
        payload = {'note_id': str(uuid4()), 'event': 'ingestion.completed'}

        delivery_id = uuid4()

        async with metastore.session() as session:

            async def fake_refresh(obj: object) -> None:
                if isinstance(obj, WebhookDelivery):
                    obj.id = delivery_id  # type: ignore[attr-defined]

            session.refresh = AsyncMock(side_effect=fake_refresh)

            # For _update_delivery
            mock_delivery = WebhookDelivery(
                id=delivery_id,
                webhook_id=webhook.id,
                event='ingestion.completed',
                payload=payload,
            )
            mock_result = MagicMock()
            mock_result.one.return_value = mock_delivery
            session.exec = AsyncMock(return_value=mock_result)

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch('memex_core.services.webhook_service.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await svc._deliver(webhook, 'ingestion.completed', payload)

            # Verify the POST was called with correct headers
            call_kwargs = mock_client.post.call_args
            assert call_kwargs.kwargs['headers']['X-Memex-Event'] == 'ingestion.completed'
            assert call_kwargs.kwargs['headers']['X-Memex-Signature'].startswith('sha256=')

    @pytest.mark.asyncio
    async def test_delivery_retries_on_failure(self) -> None:
        metastore = _make_mock_metastore()
        svc = WebhookService(metastore)

        webhook = WebhookRegistration(
            id=uuid4(),
            url='https://example.com/hook',
            secret='test-secret-key-1234',
            events=['ingestion.completed'],
        )
        payload = {'test': 'data'}
        delivery_id = uuid4()

        async with metastore.session() as session:

            async def fake_refresh(obj: object) -> None:
                if isinstance(obj, WebhookDelivery):
                    obj.id = delivery_id  # type: ignore[attr-defined]

            session.refresh = AsyncMock(side_effect=fake_refresh)

            mock_delivery = WebhookDelivery(
                id=delivery_id,
                webhook_id=webhook.id,
                event='ingestion.completed',
                payload=payload,
            )
            mock_result = MagicMock()
            mock_result.one.return_value = mock_delivery
            session.exec = AsyncMock(return_value=mock_result)

        with (
            patch('memex_core.services.webhook_service.httpx.AsyncClient') as mock_client_cls,
            patch('memex_core.services.webhook_service.asyncio.sleep', new_callable=AsyncMock),
        ):
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError('connection refused'))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await svc._deliver(webhook, 'ingestion.completed', payload)

            # Should have attempted MAX_ATTEMPTS times
            assert mock_client.post.call_count == MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_fire_event_filters_subscribers(self) -> None:
        metastore = _make_mock_metastore()
        svc = WebhookService(metastore)

        w_ingestion = WebhookRegistration(
            id=uuid4(),
            url='https://a.com/hook',
            secret='secret-aaaa-1234',
            events=['ingestion.completed'],
            active=True,
        )
        w_reflection = WebhookRegistration(
            id=uuid4(),
            url='https://b.com/hook',
            secret='secret-bbbb-5678',
            events=['reflection.completed'],
            active=True,
        )

        svc.list_all = AsyncMock(return_value=[w_ingestion, w_reflection])  # type: ignore[method-assign]
        svc._deliver = AsyncMock()  # type: ignore[method-assign]

        with patch('memex_core.services.webhook_service.asyncio.create_task') as mock_task:
            await svc.fire_event('ingestion.completed', {'note_id': 'abc'})
            # Only w_ingestion should be delivered to
            assert mock_task.call_count == 1


# ---------------------------------------------------------------------------
# Webhook HMAC verification
# ---------------------------------------------------------------------------


class TestHMACVerification:
    def test_signature_matches_expected(self) -> None:
        """Simulate what a receiver would do to verify the signature."""
        secret = 'webhook-verification-secret'
        payload = {'event': 'ingestion.completed', 'note_id': str(uuid4())}
        payload_bytes = json.dumps(payload, default=str).encode('utf-8')

        signature = compute_signature(payload_bytes, secret)

        # Receiver-side verification
        expected = hmac.new(secret.encode('utf-8'), payload_bytes, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(signature, expected)


# ---------------------------------------------------------------------------
# Webhook endpoints (FastAPI TestClient)
# ---------------------------------------------------------------------------


class TestWebhookEndpoints:
    def _make_app(self, webhook_service: WebhookService) -> TestClient:
        from fastapi import FastAPI

        from memex_core.server.webhooks import router

        app = FastAPI()
        app.include_router(router)
        app.state.webhook_service = webhook_service
        return TestClient(app)

    def test_create_webhook_endpoint(self) -> None:
        svc = MagicMock(spec=WebhookService)
        webhook = WebhookRegistration(
            id=uuid4(),
            url='https://example.com/hook',
            secret='test-secret-key-1234',
            events=['ingestion.completed'],
            created_at=datetime.now(timezone.utc),
        )
        svc.create = AsyncMock(return_value=webhook)

        client = self._make_app(svc)
        resp = client.post(
            '/api/v1/webhooks',
            json={
                'url': 'https://example.com/hook',
                'secret': 'test-secret-key-1234',
                'events': ['ingestion.completed'],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['url'] == 'https://example.com/hook'
        assert data['events'] == ['ingestion.completed']
        assert data['active'] is True

    def test_create_webhook_invalid_event(self) -> None:
        svc = MagicMock(spec=WebhookService)
        client = self._make_app(svc)
        resp = client.post(
            '/api/v1/webhooks',
            json={
                'url': 'https://example.com/hook',
                'secret': 'test-secret-key-1234',
                'events': ['invalid.event'],
            },
        )
        assert resp.status_code == 400
        assert 'Invalid event types' in resp.json()['detail']

    def test_create_webhook_short_secret(self) -> None:
        svc = MagicMock(spec=WebhookService)
        client = self._make_app(svc)
        resp = client.post(
            '/api/v1/webhooks',
            json={
                'url': 'https://example.com/hook',
                'secret': 'short',
                'events': ['ingestion.completed'],
            },
        )
        assert resp.status_code == 422  # Pydantic validation

    def test_list_webhooks_endpoint(self) -> None:
        svc = MagicMock(spec=WebhookService)
        now = datetime.now(timezone.utc)
        svc.list_all = AsyncMock(
            return_value=[
                WebhookRegistration(
                    id=uuid4(),
                    url='https://a.com/hook',
                    secret='secret-a-1234567',
                    events=['ingestion.completed'],
                    created_at=now,
                ),
            ]
        )
        client = self._make_app(svc)
        resp = client.get('/api/v1/webhooks')
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_get_webhook_not_found(self) -> None:
        svc = MagicMock(spec=WebhookService)
        svc.get = AsyncMock(return_value=None)
        client = self._make_app(svc)
        resp = client.get(f'/api/v1/webhooks/{uuid4()}')
        assert resp.status_code == 404

    def test_delete_webhook_endpoint(self) -> None:
        svc = MagicMock(spec=WebhookService)
        svc.delete = AsyncMock(return_value=True)
        client = self._make_app(svc)
        resp = client.delete(f'/api/v1/webhooks/{uuid4()}')
        assert resp.status_code == 204

    def test_delete_webhook_not_found(self) -> None:
        svc = MagicMock(spec=WebhookService)
        svc.delete = AsyncMock(return_value=False)
        client = self._make_app(svc)
        resp = client.delete(f'/api/v1/webhooks/{uuid4()}')
        assert resp.status_code == 404

    def test_update_webhook_endpoint(self) -> None:
        svc = MagicMock(spec=WebhookService)
        now = datetime.now(timezone.utc)
        updated = WebhookRegistration(
            id=uuid4(),
            url='https://new.com/hook',
            secret='new-secret-key-1234',
            events=['reflection.completed'],
            active=False,
            created_at=now,
        )
        svc.update = AsyncMock(return_value=updated)
        client = self._make_app(svc)
        resp = client.patch(
            f'/api/v1/webhooks/{updated.id}',
            json={
                'url': 'https://new.com/hook',
                'active': False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()['active'] is False

    def test_list_deliveries_endpoint(self) -> None:
        svc = MagicMock(spec=WebhookService)
        webhook_id = uuid4()
        now = datetime.now(timezone.utc)
        svc.list_deliveries = AsyncMock(
            return_value=[
                WebhookDelivery(
                    id=uuid4(),
                    webhook_id=webhook_id,
                    event='ingestion.completed',
                    payload={'test': True},
                    status='success',
                    attempts=1,
                    last_error=None,
                    created_at=now,
                ),
            ]
        )
        client = self._make_app(svc)
        resp = client.get(f'/api/v1/webhooks/{webhook_id}/deliveries')
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]['status'] == 'success'
