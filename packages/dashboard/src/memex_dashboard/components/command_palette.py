import reflex as rx
from .. import style


class CommandPaletteState(rx.State):
    show: bool = False
    search_query: str = ''

    def toggle(self):
        self.show = not self.show
        if self.show:
            self.search_query = ''

    def set_show(self, open: bool):
        self.show = open
        if self.show:
            self.search_query = ''

    def handle_search(self, query: str):
        self.search_query = query


def command_palette() -> rx.Component:
    return rx.fragment(
        rx.dialog.root(
            rx.dialog.content(
                rx.vstack(
                    rx.input(
                        placeholder='Search or jump to...',
                        width='100%',
                        size='3',
                        variant='soft',
                        on_change=CommandPaletteState.handle_search,
                        auto_focus=True,
                    ),
                    rx.divider(),
                    rx.vstack(
                        rx.text('No results found', color=style.SECONDARY_TEXT, size='2'),
                        padding='20px',
                        align='center',
                        width='100%',
                    ),
                    spacing='3',
                ),
                max_width='600px',
                bg=style.SIDEBAR_BG,
                border=f'1px solid {style.BORDER_COLOR}',
                padding='12px',
            ),
            open=CommandPaletteState.show,
            on_open_change=CommandPaletteState.set_show,
        ),
    )
