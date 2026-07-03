# Memex vault snapshot

Snapshot version: 1.2.0
Exported at: 2026-06-12T19:48:29.959466+00:00

See ``manifest.json`` for the machine-readable header. JSONL files under ``derived/`` are line-delimited JSON; one record per line. ``notes/<dir>/note.md`` holds the original note text. ``notes/<dir>/metadata.json`` carries note metadata with asset paths rewritten to relative form.

This snapshot is consumed by the eval-only import (V12) inside the memex_eval package. Other downstream consumers pin to the snapshot SemVer in ``manifest.json::snapshot_version`` and parse the JSONL schemas accordingly.
