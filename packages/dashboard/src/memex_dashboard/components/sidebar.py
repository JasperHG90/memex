import reflex as rx
from .. import style
from ..state import State


def sidebar_item(icon: str, text: str, url: str) -> rx.Component:
    is_active = State.current_page == text
    return rx.link(
        rx.hstack(
            rx.icon(icon, size=18),
            rx.text(text, size='2', weight='medium'),
            spacing='3',
            align='center',
        ),
        href=url,
        underline='none',
        style=rx.cond(
            is_active,
            style.active_sidebar_item_style,
            style.sidebar_item_style,
        ),
        width='100%',
    )


def sidebar() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.box(
                bg=style.ACCENT_COLOR,
                width='24px',
                height='24px',
                border_radius='4px',
            ),
            rx.heading('Memex', size='4', weight='bold', color=style.TEXT_COLOR),
            spacing='3',
            align='center',
            padding_bottom='24px',
        ),
        rx.vstack(
            sidebar_item('layout-dashboard', 'Overview', '/'),
            sidebar_item('share-2', 'Entity Graph', '/entity'),
            sidebar_item('git-branch', 'Lineage', '/lineage'),
            sidebar_item('search', 'Memory Search', '/search'),
            sidebar_item('file-search', 'Document Search', '/doc-search'),
            sidebar_item('activity', 'System Status', '/status'),
            spacing='1',
            width='100%',
        ),
        rx.spacer(),
        rx.vstack(
            sidebar_item('settings', 'Settings', '/settings'),
            sidebar_item('circle-help', 'Help', '#'),
            spacing='1',
            width='100%',
        ),
        width=style.SIDEBAR_WIDTH,
        height='100vh',
        bg=style.SIDEBAR_BG,
        padding='20px',
        border_right=f'1px solid {style.BORDER_COLOR}',
        align_items='start',
    )
