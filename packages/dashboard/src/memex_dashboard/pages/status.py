import reflex as rx
import httpx
import asyncio
from typing import Dict
from .. import style
from ..api import api_client

import time


class StatusState(rx.State):
    metrics: Dict[str, str] = {}
    is_loading: bool = False

    async def on_load(self):
        return [StatusState.fetch_metrics]

    async def tick(self):
        await asyncio.sleep(5)
        await self.fetch_metrics()
        return StatusState.tick

    async def fetch_metrics(self):
        self.is_loading = True
        try:
            # Consistent with overview.py logic
            base_url = api_client.api.client.base_url
            if not base_url:
                base_url = 'http://localhost:8000'
            else:
                # Strip /api/v1/ if present to get root for /metrics
                base_url = str(base_url).split('/api/')[0]

            candidate_urls = [
                f'{base_url}/api/v1/metrics',
                'http://localhost:8000/api/v1/metrics',
                'http://127.0.0.1:8000/api/v1/metrics',
            ]

            metrics_text = ''
            async with httpx.AsyncClient(timeout=2.0) as client:
                for url in candidate_urls:
                    try:
                        resp = await client.get(url)
                        if resp.status_code == 200:
                            metrics_text = resp.text
                            break
                    except Exception:
                        continue

            parsed = {}
            if metrics_text:
                for line in metrics_text.split('\n'):
                    if line.startswith('#') or not line:
                        continue
                    parts = line.split(' ')
                    if len(parts) >= 2:
                        parsed[parts[0]] = parts[1]
            self.metrics = parsed
        except Exception as e:
            print(f'Error fetching stats: {e}')
        finally:
            self.is_loading = False

    def refresh(self):
        return StatusState.on_load

    @rx.var
    def request_count(self) -> str:
        # Cast to int to remove .0 if present
        val = self.metrics.get('http_requests_total', '0')
        try:
            return str(int(float(val)))
        except Exception:
            return val

    @rx.var
    def cpu_seconds(self) -> str:
        val = self.metrics.get('process_cpu_seconds_total', '0')
        try:
            return f'{float(val):.2f}s'
        except Exception:
            return f'{val}s'

    @rx.var
    def memory_usage(self) -> str:
        val = self.metrics.get('process_resident_memory_bytes', '0')
        try:
            mb = float(val) / 1024 / 1024
            return f'{mb:.2f} MB'
        except Exception:
            return val

    @rx.var
    def uptime(self) -> str:
        start_time = self.metrics.get('process_start_time_seconds', '0')
        try:
            uptime_seconds = time.time() - float(start_time)
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            return f'{hours}h {minutes}m'
        except Exception:
            return 'Unknown'

    @rx.var
    def avg_rps(self) -> str:
        count = self.metrics.get('http_requests_total', '0')
        start_time = self.metrics.get('process_start_time_seconds', '0')
        try:
            total = float(count)
            uptime = time.time() - float(start_time)
            if uptime > 0:
                rps = total / uptime
                return f'{rps:.2f} req/s'
        except Exception:
            pass
        return '0 req/s'


def metric_card(title: str, value: rx.Var, icon: str, subtext: str = '') -> rx.Component:
    return rx.box(
        rx.hstack(
            rx.icon(icon, size=24, color=style.ACCENT_COLOR),
            rx.vstack(
                rx.text(title, color=style.SECONDARY_TEXT, font_size='12px'),
                rx.text(value, font_size='24px', font_weight='bold'),
                rx.cond(subtext != '', rx.text(subtext, color='gray', font_size='10px')),
                spacing='1',
            ),
            spacing='4',
            align='center',
        ),
        padding='20px',
        bg=style.SIDEBAR_BG,
        border=f'1px solid {style.BORDER_COLOR}',
        border_radius='12px',
        width='100%',
    )


def status_page() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.heading('System Status', size='8'),
            rx.spacer(),
            rx.button(
                rx.icon('refresh-cw', size=16),
                'Refresh',
                on_click=StatusState.refresh,
                variant='surface',
            ),
            width='100%',
            align='center',
        ),
        rx.divider(margin_y='4'),
        rx.text('Key Performance Indicators', font_weight='bold', margin_bottom='2'),
        rx.grid(
            metric_card('Total Requests', StatusState.request_count, 'activity'),
            metric_card('Avg Throughput', StatusState.avg_rps, 'bar-chart-2', 'Lifetime Average'),
            metric_card('Uptime', StatusState.uptime, 'clock'),
            columns='3',
            spacing='4',
            width='100%',
        ),
        rx.text('Resource Usage', font_weight='bold', margin_top='6', margin_bottom='2'),
        rx.grid(
            metric_card('CPU Time', StatusState.cpu_seconds, 'cpu'),
            metric_card('Memory Usage', StatusState.memory_usage, 'hard-drive'),
            # Placeholder for future metric like Disk Usage or Thread Count
            metric_card('Open FDs', StatusState.metrics['process_open_fds'], 'file'),
            columns='3',
            spacing='4',
            width='100%',
        ),
        width='100%',
        height='100%',
        on_mount=StatusState.on_load,
        spacing='4',
    )
