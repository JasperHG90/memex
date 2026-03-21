"""Tests for memex_export_local."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from memex_export_local import (
    compute_freshness,
    export_full,
    get_watermark,
    init_sqlite,
    main,
    rebuild_fts,
    set_watermark,
    upsert_entities,
    upsert_entity_cooccurrences,
    upsert_kv_entries,
    upsert_memory_units,
    upsert_mental_models,
    upsert_notes,
)


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

class TestFreshness:
    def test_recent_note(self):
        updated = datetime(2026, 3, 15, tzinfo=timezone.utc)
        score = compute_freshness(updated, today=date(2026, 3, 21))
        assert score > 0.95

    def test_old_note(self):
        updated = datetime(2023, 1, 1, tzinfo=timezone.utc)
        score = compute_freshness(updated, today=date(2026, 3, 21))
        assert score == 0.2  # floor

    def test_none_returns_half(self):
        assert compute_freshness(None) == 0.5


# ---------------------------------------------------------------------------
# SQLite Init
# ---------------------------------------------------------------------------

class TestInitSqlite:
    def test_creates_all_tables(self, tmp_path):
        conn = init_sqlite(tmp_path / "test.db")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "notes" in tables
        assert "memory_units" in tables
        assert "entities" in tables
        assert "entity_cooccurrences" in tables
        assert "mental_models" in tables
        assert "kv_entries" in tables
        assert "export_fts" in tables
        assert "export_meta" in tables
        conn.close()


# ---------------------------------------------------------------------------
# Upsert Tests
# ---------------------------------------------------------------------------

class TestUpsertNotes:
    def test_insert_notes(self, tmp_path, sample_notes):
        conn = init_sqlite(tmp_path / "test.db")
        count = upsert_notes(conn, sample_notes)
        assert count == 2
        rows = conn.execute("SELECT id, title, freshness_score FROM notes").fetchall()
        assert len(rows) == 2
        # Freshness score should be populated
        assert all(r[2] is not None and r[2] > 0 for r in rows)
        conn.close()

    def test_upsert_updates_existing(self, tmp_path, sample_notes):
        conn = init_sqlite(tmp_path / "test.db")
        upsert_notes(conn, sample_notes)
        # Update title
        sample_notes[0]["title"] = "Updated Title"
        upsert_notes(conn, sample_notes)
        rows = conn.execute("SELECT title FROM notes").fetchall()
        titles = [r[0] for r in rows]
        assert "Updated Title" in titles
        assert len(rows) == 2  # No duplicates
        conn.close()


class TestUpsertMemoryUnits:
    def test_insert_units(self, tmp_path, sample_memory_units):
        conn = init_sqlite(tmp_path / "test.db")
        count = upsert_memory_units(conn, sample_memory_units)
        assert count == 2
        conn.close()


class TestUpsertEntities:
    def test_insert_entities(self, tmp_path, sample_entities):
        conn = init_sqlite(tmp_path / "test.db")
        count = upsert_entities(conn, sample_entities)
        assert count == 2
        conn.close()


class TestUpsertCooccurrences:
    def test_insert_cooccurrences(self, tmp_path, sample_cooccurrences):
        conn = init_sqlite(tmp_path / "test.db")
        count = upsert_entity_cooccurrences(conn, sample_cooccurrences)
        assert count == 1
        conn.close()


class TestUpsertMentalModels:
    def test_insert_models(self, tmp_path, sample_mental_models):
        conn = init_sqlite(tmp_path / "test.db")
        count = upsert_mental_models(conn, sample_mental_models)
        assert count == 1
        conn.close()


# ---------------------------------------------------------------------------
# FTS
# ---------------------------------------------------------------------------

class TestFts:
    def test_fts_search(self, tmp_path, sample_notes, sample_memory_units):
        conn = init_sqlite(tmp_path / "test.db")
        upsert_notes(conn, sample_notes)
        upsert_memory_units(conn, sample_memory_units)
        rebuild_fts(conn)

        results = conn.execute(
            "SELECT source_type, title FROM export_fts WHERE export_fts MATCH ?",
            ("ollama",),
        ).fetchall()
        assert len(results) >= 1
        conn.close()


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

class TestWatermark:
    def test_get_set(self, tmp_path):
        conn = init_sqlite(tmp_path / "test.db")
        assert get_watermark(conn) is None
        ts = datetime(2026, 3, 21, 12, 0, 0, tzinfo=timezone.utc)
        set_watermark(conn, ts)
        result = get_watermark(conn)
        assert result == ts
        conn.close()


# ---------------------------------------------------------------------------
# Full Export Pipeline
# ---------------------------------------------------------------------------

class TestExportFull:
    @patch("memex_export_local.fetch_kv_entries", return_value=[])
    @patch("memex_export_local.fetch_mental_models")
    @patch("memex_export_local.fetch_entity_cooccurrences")
    @patch("memex_export_local.fetch_entities")
    @patch("memex_export_local.fetch_memory_units")
    @patch("memex_export_local.fetch_notes")
    def test_full_export(
        self, mock_notes, mock_units, mock_ents, mock_coocs,
        mock_mm, mock_kv,
        tmp_path, sample_notes, sample_memory_units,
        sample_entities, sample_cooccurrences, sample_mental_models,
    ):
        mock_notes.return_value = sample_notes
        mock_units.return_value = sample_memory_units
        mock_ents.return_value = sample_entities
        mock_coocs.return_value = sample_cooccurrences
        mock_mm.return_value = sample_mental_models

        sqlite_conn = init_sqlite(tmp_path / "test.db")
        pg_conn = MagicMock()

        result = export_full(pg_conn, sqlite_conn)
        assert result["notes"] == 2
        assert result["memory_units"] == 2
        assert result["entities"] == 2

        # FTS should work after export
        fts = sqlite_conn.execute(
            "SELECT * FROM export_fts WHERE export_fts MATCH ?", ("architecture",)
        ).fetchall()
        assert len(fts) >= 1

        # Watermark should be set
        wm = get_watermark(sqlite_conn)
        assert wm is not None
        sqlite_conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_stats_no_db(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(["--stats", "--db-path", str(tmp_path / "nonexistent.db")])
        assert exc_info.value.code == 1
