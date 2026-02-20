import reflex as rx

from memex_common.schemas import CreateVaultRequest

from .. import style
from ..api import api_client
from ..vault_state import VaultState


class SettingsState(rx.State):
    """State for the Settings page."""

    vaults: list[dict] = []
    is_loading: bool = False

    # Create Vault Form
    is_create_modal_open: bool = False
    new_vault_name: str = ''
    new_vault_description: str = ''

    async def on_load(self):
        """Load initial data."""
        await self.load_vaults()

    async def load_vaults(self):
        """Fetch the list of vaults from the API."""
        self.is_loading = True
        try:
            vaults = await api_client.api.list_vaults()
            self.vaults = [v.model_dump(mode='json') for v in vaults]
        except Exception as e:
            print(f'Error loading vaults: {e}')
            return rx.toast.error(f'Error loading vaults: {e}')
        finally:
            self.is_loading = False

    def open_create_modal(self):
        self.new_vault_name = ''
        self.new_vault_description = ''
        self.is_create_modal_open = True

    def close_create_modal(self):
        self.is_create_modal_open = False

    def set_new_vault_name(self, value: str):
        self.new_vault_name = value

    def set_new_vault_description(self, value: str):
        self.new_vault_description = value

    async def create_vault(self):
        """Create a new vault."""
        if not self.new_vault_name:
            return rx.toast.error('Vault name is required.')

        self.is_loading = True
        try:
            req = CreateVaultRequest(
                name=self.new_vault_name, description=self.new_vault_description
            )
            await api_client.api.create_vault(req)
            self.close_create_modal()
            await self.load_vaults()
            return rx.toast.success(f"Vault '{self.new_vault_name}' created successfully.")
        except Exception as e:
            print(f'Error creating vault: {e}')
            return rx.toast.error(f'Error creating vault: {e}')
        finally:
            self.is_loading = False

    async def delete_vault(self, vault_id: str):
        """Delete a vault."""
        self.is_loading = True
        try:
            success = await api_client.api.delete_vault(vault_id)
            if success:
                await self.load_vaults()
                return rx.toast.success('Vault deleted.')
            else:
                return rx.toast.error('Failed to delete vault.')
        except Exception as e:
            print(f'Error deleting vault: {e}')
            return rx.toast.error(f'Error deleting vault: {e}')
        finally:
            self.is_loading = False


def _vault_role_badge(vault: dict) -> rx.Component:
    """Show Writer / Attached / Available badge based on vault role."""
    vault_id = vault['id']
    return rx.cond(
        vault_id == VaultState.writer_vault_id,
        rx.badge('Writer', color_scheme='green', variant='soft'),
        rx.cond(
            VaultState.attached_vault_ids.contains(vault_id),
            rx.badge('Attached', color_scheme='blue', variant='soft'),
            rx.badge('Available', color_scheme='gray', variant='soft'),
        ),
    )


def _vault_actions(vault: dict) -> rx.Component:
    """Render action buttons for a vault row."""
    vault_id = vault['id']
    vault_name = vault['name']
    is_writer = vault_id == VaultState.writer_vault_id
    is_attached = VaultState.attached_vault_ids.contains(vault_id)

    return rx.hstack(
        # Attach/Detach toggle — disabled for the writer vault
        rx.tooltip(
            rx.switch(
                checked=is_writer | is_attached,
                on_change=lambda checked: VaultState.toggle_attached_vault(  # type: ignore
                    vault_id, vault_name, checked
                ),
                disabled=is_writer,
                size='1',
            ),
            content=rx.cond(is_writer, 'Writer vault is always included', 'Include in search'),
        ),
        # "Set as Writer" button — disabled for the current writer
        rx.button(
            rx.icon('pen-line', size=14),
            'Writer',
            size='1',
            variant=rx.cond(is_writer, 'soft', 'outline'),
            color_scheme=rx.cond(is_writer, 'green', 'gray'),
            disabled=is_writer,
            on_click=VaultState.set_writer_vault(vault_id, vault_name),  # type: ignore
        ),
        # Delete button
        rx.button(
            rx.icon('trash-2', size=14),
            color_scheme='red',
            variant='ghost',
            size='1',
            on_click=lambda: SettingsState.delete_vault(vault_id),  # type: ignore
        ),
        spacing='3',
        align='center',
    )


def vault_row(vault: dict) -> rx.Component:
    """Render a single row in the vaults table."""
    return rx.table.row(
        rx.table.cell(rx.text(vault['name'], weight='bold'), width='20%'),
        rx.table.cell(
            rx.text(
                rx.cond(vault['description'], vault['description'], '-'),
                color=style.SECONDARY_TEXT,
                no_of_lines=1,
            ),
            width='35%',
        ),
        rx.table.cell(_vault_role_badge(vault), width='10%'),
        rx.table.cell(_vault_actions(vault), width='35%'),
    )


def create_vault_modal() -> rx.Component:
    """Modal dialog for creating a new vault."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title('Create New Vault'),
            rx.dialog.description('Vaults isolate your memories and documents.'),
            rx.vstack(
                rx.text('Name', size='2', weight='bold'),
                rx.input(
                    placeholder='My Personal Vault',
                    value=SettingsState.new_vault_name,
                    on_change=SettingsState.set_new_vault_name,
                ),
                rx.text('Description', size='2', weight='bold'),
                rx.text_area(
                    placeholder='A safe place for my personal thoughts...',
                    value=SettingsState.new_vault_description,
                    on_change=SettingsState.set_new_vault_description,
                ),
                spacing='4',
                margin_top='16px',
            ),
            rx.hstack(
                rx.dialog.close(
                    rx.button('Cancel', variant='soft', color_scheme='gray'),
                ),
                rx.button(
                    'Create Vault',
                    on_click=SettingsState.create_vault,
                    loading=SettingsState.is_loading,
                ),
                justify='end',
                margin_top='24px',
                spacing='3',
            ),
        ),
        open=SettingsState.is_create_modal_open,
        on_open_change=SettingsState.close_create_modal,
    )


def vaults_panel() -> rx.Component:
    """The content of the Vaults tab."""
    return rx.vstack(
        rx.hstack(
            rx.text(
                'Manage your data vaults. Deleting a vault will remove all associated memories.',
                color=style.SECONDARY_TEXT,
                size='2',
            ),
            rx.spacer(),
            rx.button(
                rx.icon('plus', size=16),
                'New Vault',
                on_click=SettingsState.open_create_modal,
            ),
            width='100%',
            align='center',
        ),
        rx.table.root(
            rx.table.header(
                rx.table.row(
                    rx.table.column_header_cell('Name', width='20%'),
                    rx.table.column_header_cell('Description', width='35%'),
                    rx.table.column_header_cell('Role', width='10%'),
                    rx.table.column_header_cell('Actions', width='35%'),
                ),
            ),
            rx.table.body(rx.foreach(SettingsState.vaults, vault_row)),
            variant='surface',
            width='100%',
        ),
        create_vault_modal(),
        spacing='4',
        width='100%',
    )


def preferences_panel() -> rx.Component:
    """The content of the Preferences tab."""
    return rx.vstack(
        rx.heading('Appearance', size='4'),
        rx.text('Theme selection coming soon...', color='gray'),
        rx.divider(),
        rx.heading('System', size='4'),
        rx.text('System configuration is managed via config files.', color='gray'),
        spacing='4',
        padding_top='16px',
    )


def settings_page() -> rx.Component:
    """The main Settings page component."""
    return rx.vstack(
        rx.heading('Settings', size='8'),
        rx.tabs.root(
            rx.tabs.list(
                rx.tabs.trigger('Vaults', value='vaults'),
                rx.tabs.trigger('Preferences', value='preferences'),
            ),
            rx.tabs.content(
                vaults_panel(),
                value='vaults',
                padding_top='24px',
            ),
            rx.tabs.content(
                preferences_panel(),
                value='preferences',
                padding_top='24px',
            ),
            default_value='vaults',
            width='100%',
        ),
        width='100%',
        on_mount=SettingsState.on_load,
    )
