import reflex as rx
from . import style
from .state import State
from .vault_state import VaultState
from .components.sidebar import sidebar
from .components.command_palette import command_palette
from .pages.overview import overview_page
from .pages.entity import entity_page
from .pages.lineage import lineage_page
from .pages.search import search_page
from .pages.status import status_page
from .pages.settings import settings_page
from .pages.doc_search import doc_search_page


def quick_note_modal() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title('Quick Note'),
            rx.vstack(
                rx.text_area(
                    placeholder='Capture a quick thought or fact...',
                    value=State.quick_note_content,
                    on_change=State.set_quick_note_content,
                    height='150px',
                    width='100%',
                ),
                rx.hstack(
                    rx.dialog.close(
                        rx.button('Cancel', variant='soft', color_scheme='gray'),
                    ),
                    rx.button(
                        'Save Note',
                        on_click=State.save_quick_note,
                        loading=State.is_saving_note,
                    ),
                    width='100%',
                    justify='end',
                    spacing='2',
                ),
                spacing='4',
            ),
        ),
        open=State.is_quick_note_open,
        on_open_change=State.toggle_quick_note,
    )


def main_content() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.cond(
                ~State.is_fullscreen,
                rx.hstack(
                    rx.heading(State.current_page, size='6', weight='bold'),
                    rx.spacer(),
                    rx.button(
                        rx.icon('plus', size=16),
                        'Quick Note',
                        bg=style.ACCENT_COLOR,
                        color='white',
                        size='2',
                        on_click=State.toggle_quick_note,
                    ),
                    width='100%',
                    padding_bottom='24px',
                    border_bottom=f'1px solid {style.BORDER_COLOR}',
                    margin_bottom='24px',
                ),
            ),
            rx.match(
                State.current_page,
                ('Overview', overview_page()),
                ('Entity Graph', entity_page()),
                ('Lineage', lineage_page()),
                ('Memory Search', search_page()),
                ('Note Search', doc_search_page()),
                ('System Status', status_page()),
                ('Settings', settings_page()),
                overview_page(),
            ),
            width='100%',
            padding='40px',
        ),
        flex='1',
        bg=style.BG_COLOR,
        height='100vh',
        overflow_y='auto',
    )


def index() -> rx.Component:
    return rx.hstack(
        rx.cond(
            ~State.is_fullscreen,
            rx.fragment(
                command_palette(),
                sidebar(),
            ),
        ),
        main_content(),
        quick_note_modal(),
        width='100%',
        height='100vh',
        bg=style.BG_COLOR,
        color=style.TEXT_COLOR,
        spacing='0',
        on_mount=VaultState.on_load,
    )


app = rx.App(
    theme=rx.theme(
        appearance='dark',
        has_background=True,
        accent_color='blue',
    ),
)
app.add_page(index)
app.add_page(index, route='/lineage')
app.add_page(index, route='/entity')
app.add_page(index, route='/search')
app.add_page(index, route='/status')
app.add_page(index, route='/settings')
app.add_page(index, route='/overview')
app.add_page(index, route='/doc-search')
