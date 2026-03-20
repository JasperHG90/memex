"""Tests for the webhook ingestion endpoint (P2-09)."""

import hashlib
import hmac
import json

import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from memex_common.schemas import WebhookPayload
from memex_core.server.ingestion import (
    _generate_webhook_note_key,
    _verify_webhook_signature,
)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestGenerateWebhookNoteKey:
    """Tests for _generate_webhook_note_key."""

    def test_deterministic_for_same_inputs(self):
        key1 = _generate_webhook_note_key('slack', 'hello world')
        key2 = _generate_webhook_note_key('slack', 'hello world')
        assert key1 == key2

    def test_different_source_produces_different_key(self):
        key1 = _generate_webhook_note_key('slack', 'hello world')
        key2 = _generate_webhook_note_key('github', 'hello world')
        assert key1 != key2

    def test_different_content_produces_different_key(self):
        key1 = _generate_webhook_note_key('slack', 'hello world')
        key2 = _generate_webhook_note_key('slack', 'goodbye world')
        assert key1 != key2

    def test_key_has_webhook_prefix(self):
        key = _generate_webhook_note_key('my-source', 'content')
        assert key.startswith('webhook:my-source:')

    def test_key_contains_sha256_hex(self):
        key = _generate_webhook_note_key('src', 'data')
        # Format: webhook:<source>:<sha256_hex>
        parts = key.split(':')
        assert len(parts) == 3
        assert len(parts[2]) == 64  # SHA-256 hex digest length


class TestVerifyWebhookSignature:
    """Tests for _verify_webhook_signature."""

    def test_valid_signature_accepted(self):
        secret = 'test-secret-key'
        body = b'{"title": "test"}'
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_webhook_signature(body, signature, secret) is True

    def test_invalid_signature_rejected(self):
        secret = 'test-secret-key'
        body = b'{"title": "test"}'
        assert _verify_webhook_signature(body, 'deadbeef', secret) is False

    def test_wrong_secret_rejected(self):
        secret = 'correct-secret'
        wrong_secret = 'wrong-secret'
        body = b'{"title": "test"}'
        signature = hmac.new(wrong_secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_webhook_signature(body, signature, secret) is False

    def test_tampered_body_rejected(self):
        secret = 'test-secret'
        original_body = b'{"title": "original"}'
        tampered_body = b'{"title": "tampered"}'
        signature = hmac.new(secret.encode(), original_body, hashlib.sha256).hexdigest()
        assert _verify_webhook_signature(tampered_body, signature, secret) is False


# ---------------------------------------------------------------------------
# WebhookPayload schema tests
# ---------------------------------------------------------------------------


class TestWebhookPayload:
    """Tests for the WebhookPayload schema."""

    def test_minimal_payload(self):
        payload = WebhookPayload(
            title='Test Note',
            content='Some content',
            source='test-source',
        )
        assert payload.title == 'Test Note'
        assert payload.content == 'Some content'
        assert payload.source == 'test-source'
        assert payload.description is None
        assert payload.tags == []
        assert payload.vault_id is None
        assert payload.metadata == {}

    def test_full_payload(self):
        vid = uuid4()
        payload = WebhookPayload(
            title='Full Note',
            content='# Heading\nBody text.',
            source='slack-bot',
            description='A full note',
            tags=['meeting', 'standup'],
            vault_id=vid,
            metadata={'channel': '#general'},
        )
        assert payload.title == 'Full Note'
        assert payload.tags == ['meeting', 'standup']
        assert payload.vault_id == vid
        assert payload.metadata == {'channel': '#general'}

    def test_missing_required_fields_raises(self):
        with pytest.raises(Exception):
            WebhookPayload(title='No content')  # type: ignore[call-arg]

    def test_json_round_trip(self):
        payload = WebhookPayload(
            title='Test',
            content='body',
            source='src',
        )
        raw = payload.model_dump_json()
        restored = WebhookPayload.model_validate_json(raw)
        assert restored.title == 'Test'
        assert restored.source == 'src'


# ---------------------------------------------------------------------------
# Endpoint integration tests (mocked API)
# ---------------------------------------------------------------------------


@pytest.fixture
def webhook_client():
    """Create a TestClient with mocked dependencies for webhook tests."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from memex_core.server.ingestion import router
    from memex_core.server.common import get_api

    test_app = FastAPI()
    test_app.include_router(router)

    mock_api = MagicMock()
    mock_api.ingest = AsyncMock(
        return_value={
            'status': 'success',
            'note_id': str(uuid4()),
            'unit_ids': [],
        }
    )
    mock_api.batch_manager.create_single_job = AsyncMock(return_value=uuid4())

    test_app.dependency_overrides[get_api] = lambda: mock_api
    # No auth config on app state = signature validation skipped
    client = TestClient(test_app)
    return client, mock_api


@pytest.fixture
def webhook_client_with_secret():
    """Create a TestClient with webhook secret configured."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from memex_core.server.ingestion import router
    from memex_core.server.common import get_api

    test_app = FastAPI()
    test_app.include_router(router)

    mock_api = MagicMock()
    mock_api.ingest = AsyncMock(
        return_value={
            'status': 'success',
            'note_id': str(uuid4()),
            'unit_ids': [],
        }
    )
    mock_api.batch_manager.create_single_job = AsyncMock(return_value=uuid4())

    # Set up auth config with webhook secret
    auth_config = MagicMock()
    auth_config.webhook_secret = SecretStr('test-webhook-secret')
    test_app.state.auth_config = auth_config

    test_app.dependency_overrides[get_api] = lambda: mock_api
    client = TestClient(test_app)
    return client, mock_api, 'test-webhook-secret'


class TestWebhookEndpoint:
    """Tests for POST /api/v1/ingestions/webhook."""

    def test_successful_ingestion_without_secret(self, webhook_client):
        client, mock_api = webhook_client
        payload = {
            'title': 'Test Webhook Note',
            'content': '## Hello\nWorld',
            'source': 'test-harness',
        }
        response = client.post('/api/v1/ingestions/webhook', json=payload)
        assert response.status_code == 202
        body = response.json()
        assert body['status'] == 'pending'
        assert body['job_id'] is not None

        # Verify create_single_job was called with api.ingest and correct note
        mock_api.batch_manager.create_single_job.assert_called_once()
        call_kwargs = mock_api.batch_manager.create_single_job.call_args.kwargs
        note_arg = call_kwargs['note']
        assert note_arg._metadata.name == 'Test Webhook Note'
        assert note_arg._content == b'## Hello\nWorld'

    def test_auto_generated_note_key(self, webhook_client):
        client, mock_api = webhook_client
        payload = {
            'title': 'Key Test',
            'content': 'some content',
            'source': 'my-source',
        }
        client.post('/api/v1/ingestions/webhook', json=payload)

        call_kwargs = mock_api.batch_manager.create_single_job.call_args.kwargs
        note_arg = call_kwargs['note']
        expected_key = _generate_webhook_note_key('my-source', 'some content')
        assert note_arg._explicit_key == expected_key

    def test_tags_passed_through(self, webhook_client):
        client, mock_api = webhook_client
        payload = {
            'title': 'Tagged Note',
            'content': 'body',
            'source': 'src',
            'tags': ['a', 'b'],
        }
        client.post('/api/v1/ingestions/webhook', json=payload)
        call_kwargs = mock_api.batch_manager.create_single_job.call_args.kwargs
        note_arg = call_kwargs['note']
        metadata = json.loads(note_arg.metadata)
        assert 'a' in metadata.get('tags', [])
        assert 'b' in metadata.get('tags', [])

    def test_vault_id_passed_through(self, webhook_client):
        client, mock_api = webhook_client
        vid = str(uuid4())
        payload = {
            'title': 'Vaulted Note',
            'content': 'body',
            'source': 'src',
            'vault_id': vid,
        }
        client.post('/api/v1/ingestions/webhook', json=payload)
        mock_api.batch_manager.create_single_job.assert_called_once()
        call_kwargs = mock_api.batch_manager.create_single_job.call_args.kwargs
        assert str(call_kwargs['vault_id']) == vid

    def test_invalid_payload_returns_400(self, webhook_client):
        client, _ = webhook_client
        response = client.post(
            '/api/v1/ingestions/webhook',
            content=b'not valid json',
            headers={'Content-Type': 'application/json'},
        )
        assert response.status_code == 400

    def test_missing_required_field_returns_400(self, webhook_client):
        client, _ = webhook_client
        payload = {'title': 'Missing content and source'}
        response = client.post('/api/v1/ingestions/webhook', json=payload)
        assert response.status_code == 400


class TestWebhookSignatureValidation:
    """Tests for HMAC-SHA256 signature validation on the webhook endpoint."""

    def _sign(self, body: bytes, secret: str) -> str:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_valid_signature_accepted(self, webhook_client_with_secret):
        client, mock_api, secret = webhook_client_with_secret
        payload = json.dumps(
            {
                'title': 'Signed Note',
                'content': 'secure content',
                'source': 'secure-source',
            }
        ).encode()
        sig = self._sign(payload, secret)

        response = client.post(
            '/api/v1/ingestions/webhook',
            content=payload,
            headers={
                'Content-Type': 'application/json',
                'X-Webhook-Signature': sig,
            },
        )
        assert response.status_code == 202
        mock_api.batch_manager.create_single_job.assert_called_once()

    def test_missing_signature_returns_401(self, webhook_client_with_secret):
        client, _, _ = webhook_client_with_secret
        payload = {
            'title': 'No Sig',
            'content': 'body',
            'source': 'src',
        }
        response = client.post('/api/v1/ingestions/webhook', json=payload)
        assert response.status_code == 401
        assert 'X-Webhook-Signature' in response.json()['detail']

    def test_invalid_signature_returns_403(self, webhook_client_with_secret):
        client, _, _ = webhook_client_with_secret
        payload = json.dumps(
            {
                'title': 'Bad Sig',
                'content': 'body',
                'source': 'src',
            }
        ).encode()
        response = client.post(
            '/api/v1/ingestions/webhook',
            content=payload,
            headers={
                'Content-Type': 'application/json',
                'X-Webhook-Signature': 'invalid-signature-hex',
            },
        )
        assert response.status_code == 403

    def test_tampered_body_returns_403(self, webhook_client_with_secret):
        client, _, secret = webhook_client_with_secret
        original = json.dumps(
            {
                'title': 'Original',
                'content': 'original body',
                'source': 'src',
            }
        ).encode()
        sig = self._sign(original, secret)

        tampered = json.dumps(
            {
                'title': 'Tampered',
                'content': 'tampered body',
                'source': 'src',
            }
        ).encode()
        response = client.post(
            '/api/v1/ingestions/webhook',
            content=tampered,
            headers={
                'Content-Type': 'application/json',
                'X-Webhook-Signature': sig,
            },
        )
        assert response.status_code == 403
