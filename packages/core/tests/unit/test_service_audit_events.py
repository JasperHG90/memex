"""Unit tests for service-layer domain audit events (AC-013 through AC-018)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_audit_service() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# NoteService domain events (AC-013)
# ---------------------------------------------------------------------------


class TestNoteServiceAuditEvents:
    """AC-013: Note mutations emit domain events."""

    @pytest.fixture
    def note_service(self, mock_metastore, mock_filestore, mock_config):
        from memex_core.services.notes import NoteService
        from memex_core.services.vaults import VaultService

        vaults = MagicMock(spec=VaultService)
        svc = NoteService(mock_metastore, mock_filestore, mock_config, vaults)
        svc._audit_service = _mock_audit_service()
        return svc

    @pytest.mark.asyncio
    async def test_delete_note_emits_event(self, note_service, mock_session):
        """delete_note emits note.deleted after successful deletion."""
        from memex_core.memory.sql_models import Note

        note_id = uuid4()
        mock_note = MagicMock(spec=Note)
        mock_note.id = note_id
        mock_note.vault_id = uuid4()
        mock_note.assets = None
        mock_note.filestore_path = None

        # Make session methods async-compatible
        mock_session.get = AsyncMock(return_value=mock_note)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        # Mock the unit query for entity cleanup
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.exec = AsyncMock(return_value=mock_result)

        # Patch AsyncTransaction to avoid real DB
        with patch('memex_core.services.notes.AsyncTransaction') as mock_txn_cls:
            ctx = AsyncMock()
            ctx.db_session = mock_session
            mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
            mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await note_service.delete_note(note_id)

        assert result is True
        note_service._audit_service.log.assert_called_once()
        call_kwargs = note_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'note.deleted'
        assert call_kwargs['resource_type'] == 'note'
        assert call_kwargs['resource_id'] == str(note_id)

    @pytest.mark.asyncio
    async def test_set_note_status_emits_event(self, note_service, mock_session):
        """set_note_status emits note.status_changed."""
        from memex_core.memory.sql_models import Note

        note_id = uuid4()
        mock_note = MagicMock(spec=Note)
        mock_note.id = note_id
        mock_session.get.return_value = mock_note

        result = await note_service.set_note_status(note_id, 'archived')

        assert result['status'] == 'archived'
        note_service._audit_service.log.assert_called_once()
        call_kwargs = note_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'note.status_changed'
        assert call_kwargs['resource_type'] == 'note'
        assert call_kwargs['resource_id'] == str(note_id)
        assert call_kwargs['details']['status'] == 'archived'

    @pytest.mark.asyncio
    async def test_update_note_date_emits_event(self, note_service, mock_session):
        """update_note_date emits note.date_changed when date actually changes."""
        from memex_core.memory.sql_models import Note

        note_id = uuid4()
        old_date = datetime(2025, 1, 1, tzinfo=timezone.utc)
        new_date = datetime(2025, 6, 1, tzinfo=timezone.utc)

        mock_note = MagicMock(spec=Note)
        mock_note.publish_date = old_date
        mock_note.created_at = old_date
        mock_note.doc_metadata = {}
        mock_note.page_index = None
        mock_session.get.return_value = mock_note

        # Mock bulk update result
        mock_exec_result = MagicMock()
        mock_exec_result.rowcount = 2
        mock_session.exec.return_value = mock_exec_result

        result = await note_service.update_note_date(note_id, new_date)

        assert result['new_date'] == new_date.isoformat()
        note_service._audit_service.log.assert_called_once()
        call_kwargs = note_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'note.date_changed'
        assert call_kwargs['details']['new_date'] == new_date.isoformat()

    @pytest.mark.asyncio
    async def test_update_note_date_no_event_on_no_change(self, note_service, mock_session):
        """update_note_date does NOT emit event when date is unchanged (early return)."""
        from memex_core.memory.sql_models import Note

        note_id = uuid4()
        the_date = datetime(2025, 1, 1, tzinfo=timezone.utc)

        mock_note = MagicMock(spec=Note)
        mock_note.publish_date = the_date
        mock_note.created_at = the_date
        mock_session.get.return_value = mock_note

        result = await note_service.update_note_date(note_id, the_date)

        assert result['units_updated'] == 0
        note_service._audit_service.log.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_note_title_emits_event(self, note_service, mock_session):
        """update_note_title emits note.renamed."""
        from memex_core.memory.sql_models import Note

        note_id = uuid4()
        mock_note = MagicMock(spec=Note)
        mock_note.doc_metadata = {}
        mock_note.page_index = None
        mock_note.model_dump.return_value = {'id': str(note_id), 'title': 'New Title'}
        mock_session.get.return_value = mock_note

        await note_service.update_note_title(note_id, 'New Title')

        note_service._audit_service.log.assert_called_once()
        call_kwargs = note_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'note.renamed'
        assert call_kwargs['resource_id'] == str(note_id)
        assert call_kwargs['details']['new_title'] == 'New Title'

    @pytest.mark.asyncio
    async def test_migrate_note_emits_event(self, note_service, mock_session):
        """migrate_note emits note.migrated."""

        note_id = uuid4()
        source_vault = uuid4()
        target_vault = uuid4()

        mock_note = MagicMock()
        mock_note.id = note_id
        mock_note.vault_id = source_vault
        mock_note.filestore_path = None
        mock_note.assets = None

        mock_target_vault = MagicMock()
        mock_target_vault.name = 'target'

        mock_source_vault = MagicMock()
        mock_source_vault.name = 'source'

        # session.get returns different objects for Note, target Vault, source Vault
        mock_session.get = AsyncMock(side_effect=[mock_note, mock_target_vault, mock_source_vault])

        # Mock various exec calls (unit_ids, entity_ids, updates)
        mock_exec_result = MagicMock()
        mock_exec_result.all.return_value = []
        mock_exec_result.rowcount = 0
        mock_session.exec = AsyncMock(return_value=mock_exec_result)

        # Mock filestore
        note_service.filestore.exists = AsyncMock(return_value=False)

        with patch('memex_core.services.notes.AsyncTransaction') as mock_txn_cls:
            ctx = AsyncMock()
            ctx.db_session = mock_session
            mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
            mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await note_service.migrate_note(note_id, target_vault)

        assert result['status'] == 'success'
        note_service._audit_service.log.assert_called_once()
        call_kwargs = note_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'note.migrated'
        assert call_kwargs['resource_id'] == str(note_id)
        assert call_kwargs['details']['target_vault'] == str(target_vault)

    @pytest.mark.asyncio
    async def test_add_note_assets_emits_event(self, note_service, mock_session):
        """add_note_assets emits note.assets_added."""
        from memex_core.memory.sql_models import Note, Vault

        note_id = uuid4()
        mock_note = MagicMock(spec=Note)
        mock_note.id = note_id
        mock_note.vault_id = uuid4()
        mock_note.assets = []

        mock_vault = MagicMock(spec=Vault)
        mock_vault.name = 'test-vault'

        mock_session.get = AsyncMock(side_effect=[mock_note, mock_vault])

        with patch('memex_core.services.notes.AsyncTransaction') as mock_txn_cls:
            ctx = AsyncMock()
            ctx.db_session = mock_session
            mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
            mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await note_service.add_note_assets(note_id, {'file.txt': b'data'})

        note_service._audit_service.log.assert_called_once()
        call_kwargs = note_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'note.assets_added'
        assert call_kwargs['resource_id'] == str(note_id)
        assert call_kwargs['details']['count'] == 1

    @pytest.mark.asyncio
    async def test_delete_note_assets_emits_event(self, note_service, mock_session):
        """delete_note_assets emits note.assets_deleted."""
        from memex_core.memory.sql_models import Note

        note_id = uuid4()
        asset_path = f'assets/test-vault/{note_id}/file.txt'
        mock_note = MagicMock(spec=Note)
        mock_note.id = note_id
        mock_note.assets = [asset_path]

        mock_session.get = AsyncMock(return_value=mock_note)

        with patch('memex_core.services.notes.AsyncTransaction') as mock_txn_cls:
            ctx = AsyncMock()
            ctx.db_session = mock_session
            mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
            mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await note_service.delete_note_assets(note_id, [asset_path])

        note_service._audit_service.log.assert_called_once()
        call_kwargs = note_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'note.assets_deleted'
        assert call_kwargs['resource_id'] == str(note_id)
        assert call_kwargs['details']['count'] == 1


# ---------------------------------------------------------------------------
# IngestionService domain events (AC-014)
# ---------------------------------------------------------------------------


class TestIngestionServiceAuditEvents:
    """AC-014: Ingestion operations emit domain events."""

    @pytest.fixture
    def ingestion_service(self, mock_metastore, mock_filestore, mock_config):
        from memex_core.services.ingestion import IngestionService
        from memex_core.services.vaults import VaultService

        memory = AsyncMock()
        memory.retain = AsyncMock(return_value={'status': 'success'})
        lm = MagicMock()
        file_processor = MagicMock()
        vaults = MagicMock(spec=VaultService)
        vaults.resolve_vault_identifier = AsyncMock(return_value=uuid4())

        svc = IngestionService(
            metastore=mock_metastore,
            filestore=mock_filestore,
            config=mock_config,
            lm=lm,
            memory=memory,
            file_processor=file_processor,
            vaults=vaults,
        )
        svc._audit_service = _mock_audit_service()
        return svc

    @pytest.mark.asyncio
    async def test_ingest_emits_event(self, ingestion_service, mock_session):
        """ingest() emits note.ingested with title after successful ingestion."""
        from memex_core.memory.sql_models import Vault

        note_id = uuid4()
        note = MagicMock()
        note.idempotency_key = note_id
        note._metadata.name = 'Test Note'
        note._metadata.description = 'desc'
        note._metadata.author = None
        note._metadata.tags = []
        note._content = b'# Test content'
        note._files = {}
        note.source_uri = None
        note.content_fingerprint = 'abc123'
        note.template = None

        # Idempotency check: no existing note
        mock_session.exec.return_value.first.return_value = None

        mock_vault = MagicMock(spec=Vault)
        mock_vault.name = 'test-vault'
        mock_session.get = AsyncMock(return_value=mock_vault)

        with (
            patch('memex_core.services.ingestion.AsyncTransaction') as mock_txn_cls,
            patch(
                'memex_core.services.ingestion.resolve_document_title', new_callable=AsyncMock
            ) as mock_title,
            patch('memex_core.services.ingestion.audit_event') as mock_ae,
        ):
            ctx = AsyncMock()
            ctx.db_session = mock_session
            mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
            mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_title.return_value = 'Resolved Title'

            # _detect_overlapping_notes returns empty
            ingestion_service._detect_overlapping_notes = AsyncMock(return_value=[])

            await ingestion_service.ingest(
                note, vault_id=uuid4(), event_date=datetime.now(timezone.utc)
            )

            mock_ae.assert_called_once()
            args = mock_ae.call_args
            assert args[0][1] == 'note.ingested'
            assert args[0][2] == 'note'
            assert args[0][3] == str(note_id)
            assert args[1]['title'] == 'Resolved Title'

    @pytest.mark.asyncio
    async def test_ingest_from_url_emits_event(self, ingestion_service):
        """ingest_from_url() emits note.ingested_url after delegation."""
        note_id = uuid4()

        # Mock self.ingest to return a result dict
        ingestion_service.ingest = AsyncMock(return_value={'status': 'success', 'note_id': note_id})

        extracted = MagicMock()
        extracted.content = 'Web content'
        extracted.source = 'https://example.com'
        extracted.metadata = {'hostname': 'example.com', 'title': 'Example', 'date': None}
        extracted.document_date = None

        with (
            patch(
                'memex_core.services.ingestion.WebContentProcessor.fetch_and_extract',
                new_callable=AsyncMock,
                return_value=extracted,
            ),
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch('memex_core.services.ingestion.audit_event') as mock_ae,
        ):
            await ingestion_service.ingest_from_url('https://example.com')

            mock_ae.assert_called_once()
            args = mock_ae.call_args
            assert args[0][1] == 'note.ingested_url'
            assert args[0][2] == 'note'
            assert args[0][3] == str(note_id)
            assert args[1]['url'] == 'https://example.com'

    @pytest.mark.asyncio
    async def test_ingest_from_file_emits_event(self, ingestion_service):
        """ingest_from_file() emits note.ingested_file after delegation."""
        note_id = uuid4()

        # Mock self.ingest to return a result dict
        ingestion_service.ingest = AsyncMock(return_value={'status': 'success', 'note_id': note_id})

        extracted = MagicMock()
        extracted.content = 'PDF content'
        extracted.content_type = 'pdf'
        extracted.metadata = {'title': 'Report', 'author': None, 'creation_date': None}
        extracted.images = {}
        extracted.document_date = None

        with (
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
                return_value=datetime.now(timezone.utc),
            ),
            patch(
                'memex_core.services.ingestion._is_meaningful_name',
                return_value=True,
            ),
            patch('memex_core.services.ingestion.audit_event') as mock_ae,
        ):
            ingestion_service._file_processor.extract = AsyncMock(return_value=extracted)

            await ingestion_service.ingest_from_file('/tmp/report.pdf')

            mock_ae.assert_called_once()
            args = mock_ae.call_args
            assert args[0][1] == 'note.ingested_file'
            assert args[0][2] == 'note'
            assert args[0][3] == str(note_id)
            assert args[1]['file_path'] == '/tmp/report.pdf'


# ---------------------------------------------------------------------------
# KVService domain events (AC-015)
# ---------------------------------------------------------------------------


class TestKVServiceAuditEvents:
    """AC-015: KV mutations emit domain events."""

    @pytest.fixture
    def kv_service(self, mock_metastore, mock_filestore, mock_config):
        from memex_core.services.kv import KVService

        svc = KVService(mock_metastore, mock_filestore, mock_config)
        svc._audit_service = _mock_audit_service()
        return svc

    @pytest.mark.asyncio
    async def test_put_emits_event(self, kv_service, mock_session):
        """put() emits kv.written after successful upsert."""
        mock_row = MagicMock()
        mock_row.id = uuid4()

        mock_result = MagicMock()
        mock_result.first.return_value = mock_row
        mock_session.exec = AsyncMock(return_value=mock_result)

        mock_entry = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_entry)

        result = await kv_service.put('global:test-key', 'test-value')

        assert result is mock_entry
        kv_service._audit_service.log.assert_called_once()
        call_kwargs = kv_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'kv.written'
        assert call_kwargs['resource_type'] == 'kv'
        assert call_kwargs['resource_id'] == 'global:test-key'

    @pytest.mark.asyncio
    async def test_delete_emits_event_on_success(self, kv_service, mock_session):
        """delete() emits kv.deleted when key exists."""
        mock_entry = MagicMock()
        mock_result = MagicMock()
        mock_result.first.return_value = mock_entry
        mock_session.exec = AsyncMock(return_value=mock_result)
        mock_session.delete = AsyncMock()

        result = await kv_service.delete('global:test-key')

        assert result is True
        kv_service._audit_service.log.assert_called_once()
        call_kwargs = kv_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'kv.deleted'
        assert call_kwargs['resource_id'] == 'global:test-key'

    @pytest.mark.asyncio
    async def test_delete_no_event_when_not_found(self, kv_service, mock_session):
        """delete() emits no event when key not found."""
        mock_result = MagicMock()
        mock_result.first.return_value = None
        mock_session.exec = AsyncMock(return_value=mock_result)

        result = await kv_service.delete('global:missing')

        assert result is False
        kv_service._audit_service.log.assert_not_called()


# ---------------------------------------------------------------------------
# VaultService domain events (AC-016)
# ---------------------------------------------------------------------------


class TestVaultServiceAuditEvents:
    """AC-016: Vault mutations emit domain events."""

    @pytest.fixture
    def vault_service(self, mock_metastore, mock_filestore, mock_config):
        from memex_core.services.vaults import VaultService

        svc = VaultService(mock_metastore, mock_filestore, mock_config)
        svc._audit_service = _mock_audit_service()
        return svc

    @pytest.mark.asyncio
    async def test_create_vault_emits_event(self, vault_service, mock_session):
        """create_vault() emits vault.created."""
        # Mock no existing vault with same name
        mock_result = MagicMock()
        mock_result.first.return_value = None
        mock_session.exec = AsyncMock(return_value=mock_result)

        mock_session.refresh = AsyncMock()

        await vault_service.create_vault('test-vault')

        vault_service._audit_service.log.assert_called_once()
        call_kwargs = vault_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'vault.created'
        assert call_kwargs['resource_type'] == 'vault'
        assert call_kwargs['details']['name'] == 'test-vault'

    @pytest.mark.asyncio
    async def test_delete_vault_emits_event(self, vault_service, mock_session):
        """delete_vault() emits vault.deleted on success."""
        vault_id = uuid4()
        mock_vault = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_vault)
        mock_session.delete = AsyncMock()

        result = await vault_service.delete_vault(vault_id)

        assert result is True
        vault_service._audit_service.log.assert_called_once()
        call_kwargs = vault_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'vault.deleted'
        assert call_kwargs['resource_id'] == str(vault_id)

    @pytest.mark.asyncio
    async def test_delete_vault_no_event_when_not_found(self, vault_service, mock_session):
        """delete_vault() emits no event when vault not found."""
        mock_session.get = AsyncMock(return_value=None)

        result = await vault_service.delete_vault(uuid4())

        assert result is False
        vault_service._audit_service.log.assert_not_called()

    @pytest.mark.asyncio
    async def test_truncate_vault_emits_event(self, vault_service, mock_session):
        """truncate_vault() emits vault.truncated."""
        vault_id = uuid4()

        # Mock note query returning empty
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.exec = AsyncMock(return_value=mock_result)

        # Mock delete results
        mock_delete_result = MagicMock()
        mock_delete_result.rowcount = 0
        mock_session.exec = AsyncMock(return_value=mock_delete_result)

        await vault_service.truncate_vault(vault_id)

        vault_service._audit_service.log.assert_called_once()
        call_kwargs = vault_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'vault.truncated'
        assert call_kwargs['resource_id'] == str(vault_id)


# ---------------------------------------------------------------------------
# EntityService domain events (AC-017)
# ---------------------------------------------------------------------------


class TestEntityServiceAuditEvents:
    """AC-017: Entity mutations emit domain events."""

    @pytest.fixture
    def entity_service(self, mock_metastore, mock_filestore, mock_config):
        from memex_core.services.entities import EntityService

        svc = EntityService(mock_metastore, mock_filestore, mock_config)
        svc._audit_service = _mock_audit_service()
        return svc

    @pytest.mark.asyncio
    async def test_delete_entity_emits_event(self, entity_service, mock_session):
        """delete_entity() emits entity.deleted."""
        entity_id = uuid4()
        mock_entity = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_entity)
        mock_session.delete = AsyncMock()

        # Mock the mental model query
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.exec = AsyncMock(return_value=mock_result)

        result = await entity_service.delete_entity(entity_id)

        assert result is True
        entity_service._audit_service.log.assert_called_once()
        call_kwargs = entity_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'entity.deleted'
        assert call_kwargs['resource_type'] == 'entity'
        assert call_kwargs['resource_id'] == str(entity_id)

    @pytest.mark.asyncio
    async def test_delete_mental_model_emits_event(self, entity_service, mock_session):
        """delete_mental_model() emits mental_model.deleted."""
        entity_id = uuid4()
        vault_id = uuid4()
        mock_model = MagicMock()

        mock_result = MagicMock()
        mock_result.first.return_value = mock_model
        mock_session.exec = AsyncMock(return_value=mock_result)
        mock_session.delete = AsyncMock()

        result = await entity_service.delete_mental_model(entity_id, vault_id)

        assert result is True
        entity_service._audit_service.log.assert_called_once()
        call_kwargs = entity_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'mental_model.deleted'
        assert call_kwargs['resource_type'] == 'entity'
        assert call_kwargs['resource_id'] == str(entity_id)
        assert call_kwargs['details']['vault_id'] == str(vault_id)


# ---------------------------------------------------------------------------
# ReflectionService domain events (AC-018)
# ---------------------------------------------------------------------------


class TestReflectionServiceAuditEvents:
    """AC-018: Reflection operations emit domain events."""

    @pytest.fixture
    def reflection_service(self, mock_metastore, mock_config):
        from memex_core.services.reflection import ReflectionService

        svc = ReflectionService(
            metastore=mock_metastore,
            config=mock_config,
            lm=MagicMock(),
            memory=MagicMock(),
            extraction=MagicMock(),
            queue_service=MagicMock(),
            embedding_model=MagicMock(),
        )
        svc._audit_service = _mock_audit_service()
        return svc

    @pytest.mark.asyncio
    async def test_reflect_emits_event_on_success(self, reflection_service, mock_session):
        """reflect() emits reflection.triggered on success path."""
        from memex_core.memory.reflect.models import ReflectionRequest
        from memex_core.memory.sql_models import MentalModel

        entity_id = uuid4()
        vault_id = uuid4()
        request = ReflectionRequest(entity_id=entity_id, vault_id=vault_id)

        mock_model = MentalModel(
            entity_id=entity_id,
            vault_id=vault_id,
            name='test-entity',
            observations=[],
        )

        with patch('memex_core.memory.reflect.reflection.ReflectionEngine') as mock_re:
            mock_re.return_value.reflect_batch = AsyncMock(return_value=[mock_model])
            reflection_service.queue_service.complete_reflection = AsyncMock()

            await reflection_service.reflect(request)

        reflection_service._audit_service.log.assert_called_once()
        call_kwargs = reflection_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'reflection.triggered'
        assert call_kwargs['resource_type'] == 'entity'
        assert call_kwargs['resource_id'] == str(entity_id)
        assert call_kwargs['details']['vault_id'] == str(vault_id)

    @pytest.mark.asyncio
    async def test_reflect_no_event_on_failure(self, reflection_service, mock_session):
        """reflect() does NOT emit event when reflection fails (no models produced)."""
        from memex_core.memory.reflect.models import ReflectionRequest

        entity_id = uuid4()
        vault_id = uuid4()
        request = ReflectionRequest(entity_id=entity_id, vault_id=vault_id)

        with patch('memex_core.memory.reflect.reflection.ReflectionEngine') as mock_re:
            mock_re.return_value.reflect_batch = AsyncMock(return_value=[])
            reflection_service.queue_service.mark_failed = AsyncMock()

            await reflection_service.reflect(request)

        reflection_service._audit_service.log.assert_not_called()

    @pytest.mark.asyncio
    async def test_reflect_batch_emits_only_for_succeeded(self, reflection_service, mock_session):
        """reflect_batch() emits events only for succeeded entities, not failed."""
        from memex_core.memory.reflect.models import ReflectionRequest
        from memex_core.memory.sql_models import MentalModel

        entity1 = uuid4()
        entity2 = uuid4()
        entity3 = uuid4()
        vault_id = uuid4()

        requests = [
            ReflectionRequest(entity_id=entity1, vault_id=vault_id),
            ReflectionRequest(entity_id=entity2, vault_id=vault_id),
            ReflectionRequest(entity_id=entity3, vault_id=vault_id),
        ]

        # Only entity1 and entity3 succeed
        model1 = MentalModel(entity_id=entity1, vault_id=vault_id, name='e1', observations=[])
        model3 = MentalModel(entity_id=entity3, vault_id=vault_id, name='e3', observations=[])

        with patch('memex_core.memory.reflect.reflection.ReflectionEngine') as mock_re:
            mock_re.return_value.reflect_batch = AsyncMock(return_value=[model1, model3])
            reflection_service.queue_service.complete_reflection = AsyncMock()
            reflection_service.queue_service.mark_failed = AsyncMock()

            await reflection_service.reflect_batch(requests)

        # Should emit exactly 2 events (entity1, entity3), not entity2
        assert reflection_service._audit_service.log.call_count == 2
        triggered_ids = set()
        for call in reflection_service._audit_service.log.call_args_list:
            assert call.kwargs['action'] == 'reflection.triggered'
            triggered_ids.add(call.kwargs['resource_id'])
        assert triggered_ids == {str(entity1), str(entity3)}

    @pytest.mark.asyncio
    async def test_retry_dead_letter_emits_event(self, reflection_service, mock_session):
        """retry_dead_letter_item() emits reflection.dlq_retried."""
        item_id = uuid4()
        reflection_service.queue_service.retry_dead_letter = AsyncMock(return_value=MagicMock())

        await reflection_service.retry_dead_letter_item(item_id)

        reflection_service._audit_service.log.assert_called_once()
        call_kwargs = reflection_service._audit_service.log.call_args.kwargs
        assert call_kwargs['action'] == 'reflection.dlq_retried'
        assert call_kwargs['resource_type'] == 'reflection'
        assert call_kwargs['resource_id'] == str(item_id)
