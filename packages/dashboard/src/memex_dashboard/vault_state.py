"""Global vault selection state for the dashboard."""

import logging

import reflex as rx

from .api import api_client

logger = logging.getLogger('memex.dashboard.vault_state')


class VaultState(rx.State):
    """Tracks the active (writer) vault and attached read-only vaults."""

    writer_vault_id: str = ''
    writer_vault_name: str = ''
    attached_vaults: list[dict] = []
    is_loading: bool = False
    _initialized: bool = False

    @rx.var(cache=True)
    def attached_vault_ids(self) -> list[str]:
        """Just the attached (non-writer) vault IDs."""
        return [v['id'] for v in self.attached_vaults]

    @rx.var(cache=True)
    def all_selected_vault_ids(self) -> list[str]:
        """Combine writer + attached vault IDs for search queries."""
        ids: list[str] = []
        if self.writer_vault_id:
            ids.append(self.writer_vault_id)
        ids.extend(v['id'] for v in self.attached_vaults)
        return ids

    async def on_load(self):
        """Load default vaults once on app startup."""
        if self._initialized:
            return
        await self.load_default_vaults()

    async def load_default_vaults(self):
        """Fetch the writer and attached vaults from the server."""
        self.is_loading = True
        try:
            defaults = await api_client.api.get_default_vaults()
            self.writer_vault_id = str(defaults.active_vault.id)
            self.writer_vault_name = defaults.active_vault.name
            self.attached_vaults = [
                {'id': str(v.id), 'name': v.name} for v in defaults.attached_vaults
            ]
            self._initialized = True
        except Exception as e:
            logger.warning('Failed to load default vaults: %s', e)
        finally:
            self.is_loading = False

    def set_writer_vault(self, vault_id: str, vault_name: str):
        """Set a new writer vault."""
        # If this vault was previously attached, remove it
        self.attached_vaults = [v for v in self.attached_vaults if v['id'] != vault_id]
        self.writer_vault_id = vault_id
        self.writer_vault_name = vault_name

    def toggle_attached_vault(self, vault_id: str, vault_name: str, checked: bool):
        """Add or remove a vault from the attached list."""
        if checked:
            if not any(v['id'] == vault_id for v in self.attached_vaults):
                self.attached_vaults = [
                    *self.attached_vaults,
                    {'id': vault_id, 'name': vault_name},
                ]
        else:
            self.attached_vaults = [v for v in self.attached_vaults if v['id'] != vault_id]
