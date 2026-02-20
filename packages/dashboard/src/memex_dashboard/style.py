# Colors and theme constants
BG_COLOR = '#0D0D0D'  # Void Dark
SIDEBAR_BG = '#141414'
BORDER_COLOR = '#262626'
ACCENT_COLOR = '#3B82F6'  # Blue
TEXT_COLOR = '#EDEDED'
SECONDARY_TEXT = '#A1A1AA'
HOVER_COLOR = 'rgba(255, 255, 255, 0.05)'

# Sidebar width
SIDEBAR_WIDTH = '240px'

# Common styles
common_button_style = {
    'border_radius': '6px',
    'transition': 'all 0.2s ease',
    '_hover': {'bg': 'rgba(255, 255, 255, 0.05)'},
}

sidebar_item_style = {
    'width': '100%',
    'padding': '8px 12px',
    'border_radius': '6px',
    'cursor': 'pointer',
    'color': SECONDARY_TEXT,
    'transition': 'all 0.2s ease',
    '_hover': {
        'bg': 'rgba(255, 255, 255, 0.05)',
        'color': TEXT_COLOR,
    },
}

active_sidebar_item_style = {
    **sidebar_item_style,
    'bg': 'rgba(59, 130, 246, 0.1)',
    'color': ACCENT_COLOR,
}
