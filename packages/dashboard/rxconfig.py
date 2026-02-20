import reflex as rx

config = rx.Config(
    app_name='memex_dashboard',
    plugins=[
        rx.plugins.SitemapPlugin(),
        rx.plugins.TailwindV4Plugin(),
    ],
)
