import base64

import reflex as rx

from memex_common.schemas import NoteCreateDTO

from .api import api_client
from .vault_state import VaultState


class State(rx.State):
    """The app state."""

    # current_page is now a computed var
    is_fullscreen: bool = False

    # Quick Note State
    quick_note_content: str = ''
    is_quick_note_open: bool = False
    is_saving_note: bool = False

    @rx.var
    def current_page(self) -> str:
        path = self.router.url.path
        if path == '/lineage':
            return 'Lineage'
        elif path == '/entity':
            return 'Entity Graph'
        elif path == '/search':
            return 'Memory Search'
        elif path == '/status':
            return 'System Status'
        elif path == '/settings':
            return 'Settings'
        elif path == '/doc-search':
            return 'Note Search'
        return 'Overview'

    def set_quick_note_content(self, value: str):
        self.quick_note_content = value

    def toggle_quick_note(self):
        self.is_quick_note_open = not self.is_quick_note_open
        if self.is_quick_note_open:
            self.quick_note_content = ''

    async def save_quick_note(self):
        if not self.quick_note_content:
            return

        self.is_saving_note = True
        try:
            # Create a NoteCreateDTO (Note: metadata is not a field in NoteCreateDTO)
            note = NoteCreateDTO(
                name='Quick Note',
                description='Note captured from dashboard',
                # Pass as bytes; schema validator handles it or serializer handles it
                content=base64.b64encode(self.quick_note_content.encode('utf-8')),
                tags=['dashboard', 'quick-note'],
            )

            # Target the writer vault if one is selected
            vault_state = await self.get_state(VaultState)
            if vault_state.writer_vault_id:
                note.vault_id = vault_state.writer_vault_id

            await api_client.api.ingest(note)

            self.is_quick_note_open = False
            self.quick_note_content = ''  # Clear after success
            return rx.toast.success('Note saved successfully!')
        except Exception as e:
            print(f'Failed to save note: {e}')
            return rx.toast.error(f'Failed to save note: {e}')
        finally:
            self.is_saving_note = False

    def toggle_fullscreen(self):
        self.is_fullscreen = not self.is_fullscreen
