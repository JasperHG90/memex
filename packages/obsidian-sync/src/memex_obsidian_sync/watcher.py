from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import structlog
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import ObsidianSyncConfig, WatchMode
from .scanner import _is_excluded
from .sync import SyncResult, sync_vault

logger = structlog.get_logger()


class _MarkdownEventHandler(FileSystemEventHandler):
    """Collects modified .md file paths with debouncing."""

    def __init__(
        self,
        vault_path: Path,
        config: ObsidianSyncConfig,
        loop: asyncio.AbstractEventLoop,
        callback: asyncio.Event,
    ) -> None:
        super().__init__()
        self._vault_path = vault_path.resolve()
        self._config = config
        self._loop = loop
        self._callback = callback
        self.changed_paths: set[str] = set()

    def _should_handle(self, path: str) -> bool:
        p = Path(path)
        if p.suffix.lower() != '.md':
            return False
        if _is_excluded(p, self._vault_path, self._config.sync.exclude):
            return False
        return True

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._should_handle(str(event.src_path)):
            rel = str(Path(str(event.src_path)).relative_to(self._vault_path))
            self.changed_paths.add(rel)
            self._loop.call_soon_threadsafe(self._callback.set)

    def on_created(self, event: FileSystemEvent) -> None:
        self.on_modified(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        if hasattr(event, 'dest_path') and self._should_handle(str(event.dest_path)):
            rel = str(Path(str(event.dest_path)).relative_to(self._vault_path))
            self.changed_paths.add(rel)
            self._loop.call_soon_threadsafe(self._callback.set)


class VaultWatcher:
    """Watches an Obsidian vault and syncs changes to Memex."""

    def __init__(
        self,
        vault_path: Path,
        config: ObsidianSyncConfig,
        on_sync: Any | None = None,
    ) -> None:
        self._vault_path = vault_path.resolve()
        self._config = config
        self._on_sync = on_sync
        self._stop = False

    async def run(self) -> None:
        """Start watching based on configured mode."""
        mode = self._config.watch.mode
        if mode == WatchMode.events:
            await self._run_events()
        elif mode == WatchMode.poll:
            await self._run_poll()
        else:
            raise ValueError(f'Unknown watch mode: {mode}')

    async def _run_events(self) -> None:
        """Watchdog-based event-driven sync with debounce."""
        loop = asyncio.get_running_loop()
        change_event = asyncio.Event()

        handler = _MarkdownEventHandler(
            self._vault_path,
            self._config,
            loop,
            change_event,
        )
        observer = Observer()
        observer.schedule(handler, str(self._vault_path), recursive=True)
        observer.start()

        debounce = self._config.watch.debounce_seconds
        logger.info(
            'Watching %s for changes (debounce=%ds)',
            self._vault_path,
            debounce,
        )

        try:
            while not self._stop:
                # Wait for a filesystem event
                try:
                    await asyncio.wait_for(change_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # Debounce: wait for quiet period
                change_event.clear()
                await asyncio.sleep(debounce)

                # Drain any events that arrived during debounce
                change_event.clear()
                paths = list(handler.changed_paths)
                handler.changed_paths.clear()

                if paths:
                    logger.info('Syncing %d changed note(s)', len(paths))
                    result = await sync_vault(
                        self._vault_path,
                        self._config,
                        notes_filter=paths,
                    )
                    self._report(result)
        finally:
            observer.stop()
            observer.join()

    async def _run_poll(self) -> None:
        """Polling-based periodic sync."""
        interval = self._config.watch.poll_interval_seconds
        logger.info(
            'Polling %s every %ds',
            self._vault_path,
            interval,
        )

        while not self._stop:
            result = await sync_vault(self._vault_path, self._config)
            self._report(result)
            await asyncio.sleep(interval)

    def _report(self, result: SyncResult) -> None:
        """Log sync results and call optional callback."""
        if result.changed == 0:
            logger.debug('No changes detected')
            return
        logger.info(
            'Sync complete: %d ingested, %d skipped, %d failed',
            result.ingested,
            result.skipped,
            result.failed,
        )
        for err in result.errors:
            logger.warning('Error: %s', err)
        if self._on_sync:
            self._on_sync(result)

    def stop(self) -> None:
        """Signal the watcher to stop gracefully."""
        self._stop = True


async def run_watcher(
    vault_path: Path,
    config: ObsidianSyncConfig,
) -> None:
    """Run the watcher with signal handling."""
    watcher = VaultWatcher(vault_path, config)
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, watcher.stop)

    await watcher.run()
