import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from uuid import uuid4
from memex_dashboard.pages.entity import EntityState
from memex_common.schemas import DocumentDTO


@pytest.mark.asyncio
async def test_open_mention():
    state = EntityState()

    # Mock API
    mock_api = AsyncMock()

    # Mock _get response for memory unit (which is still a dict from api._get)
    doc_id = uuid4()
    mock_api._get.return_value = {
        'id': str(uuid4()),
        'document_id': str(doc_id),
        'text': 'Some memory text',
        'fact_type': 'observation',
        'unit_metadata': {'confidence': 0.9},
    }

    # Mock get_document returning DTO
    doc_dto = MagicMock(spec=DocumentDTO)
    doc_dto.name = 'My Document'
    mock_api.get_document.return_value = doc_dto

    with patch('memex_dashboard.pages.entity.api_client') as mock_client_module:
        mock_client_module.api = mock_api

        await state.open_mention('some-mention-id')

        # Assertions
        assert state.is_mention_modal_open is True
        assert state.selected_mention is not None
        assert state.selected_mention['doc_title'] == 'My Document'
        assert state.selected_mention['fact_type'] == 'observation'

        # Verify props were extracted
        props_dict = dict(state.selected_mention_props)
        assert props_dict['meta.confidence'] == '0.9'

        # Verify API calls
        mock_api.get_document.assert_called_once()
