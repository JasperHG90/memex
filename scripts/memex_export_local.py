"""Memex offline export — Postgres → local SQLite cache.

Exports Memex data to a local SQLite database for offline access.
Same architectural pattern as homelab-research/scripts/catalog_sync.py.

Usage:
    python scripts/memex_export_local.py                # incremental export
    python scripts/memex_export_local.py --full          # full re-export
    python scripts/memex_export_local.py --stats         # show export stats
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_TEXT_SIZE = 200_000  # characters — cap for FTS content

TABLES_TO_EXPORT = [
    "notes", "memory_units", "entities",
    "entity_cooccurrences", "mental_models", "kv_entries",
]


# ---------------------------------------------------------------------------
# Postgres Reader
# ---------------------------------------------------------------------------

def get_pg_connection(password: str | None = None):
    """Connect to Memex Postgres. Returns connection or None."""
    try:
        import psycopg2
    except ImportError:
        logger.warning("[export] psycopg2 not installed. Install with: pip install psycopg2-binary")
        return None

    pw = password or os.environ.get("MEMEX_DB_PASSWORD")
    if not pw:
        logger.warning("[export] MEMEX_DB_PASSWORD not set.")
        return None

    try:
        conn = psycopg2.connect(
            host="localhost", port=5432,
            database="postgres", user="postgres", password=pw,
        )
        return conn
    except Exception:
        logger.warning("[export] Could not connect to Postgres", exc_info=True)
        return None


def _rows_to_dicts(cursor) -> list[dict]:
    """Convert cursor results to list of dicts."""
    cols = [desc[0] for desc in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def fetch_notes(pg_conn, since: datetime | None = None) -> list[dict]:
    """Fetch notes from Postgres, optionally since a watermark."""
    with pg_conn.cursor() as cur:
        if since:
            cur.execute(
                "SELECT id, vault_id, title, status, publish_date, created_at, updated_at, metadata "
                "FROM notes WHERE updated_at > %s ORDER BY updated_at", (since,)
            )
        else:
            cur.execute(
                "SELECT id, vault_id, title, status, publish_date, created_at, updated_at, metadata "
                "FROM notes ORDER BY updated_at"
            )
        return _rows_to_dicts(cur)


def fetch_memory_units(pg_conn, since: datetime | None = None) -> list[dict]:
    """Fetch memory units (without embeddings)."""
    with pg_conn.cursor() as cur:
        if since:
            cur.execute(
                "SELECT id, note_id, text, fact_type, event_date, confidence, status, context, "
                "created_at, updated_at FROM memory_units WHERE updated_at > %s ORDER BY updated_at",
                (since,),
            )
        else:
            cur.execute(
                "SELECT id, note_id, text, fact_type, event_date, confidence, status, context, "
                "created_at, updated_at FROM memory_units ORDER BY updated_at"
            )
        return _rows_to_dicts(cur)


def fetch_entities(pg_conn) -> list[dict]:
    """Fetch all entities."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT id, canonical_name, entity_type, mention_count FROM entities ORDER BY mention_count DESC"
        )
        return _rows_to_dicts(cur)


def fetch_entity_cooccurrences(pg_conn) -> list[dict]:
    """Fetch entity co-occurrence pairs."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT entity_id_1, entity_id_2, cooccurrence_count "
            "FROM entity_cooccurrences ORDER BY cooccurrence_count DESC"
        )
        return _rows_to_dicts(cur)


def fetch_mental_models(pg_conn) -> list[dict]:
    """Fetch mental models."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT id, entity_id, name, observations, version FROM mental_models ORDER BY name"
        )
        return _rows_to_dicts(cur)


def fetch_kv_entries(pg_conn) -> list[dict]:
    """Fetch KV store entries (without embeddings)."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id, key, value, created_at, updated_at FROM kv_entries ORDER BY key")
        return _rows_to_dicts(cur)


# ---------------------------------------------------------------------------
# Freshness Scoring
# ---------------------------------------------------------------------------

def compute_freshness(updated_at: datetime | None, today: date | None = None) -> float:
    """Compute freshness score for a note. Slow decay (3yr half-life)."""
    if updated_at is None:
        return 0.5
    if today is None:
        today = date.today()
    ref = updated_at.date() if isinstance(updated_at, datetime) else updated_at
    age_months = (today.year - ref.year) * 12 + (today.month - ref.month)
    return round(max(0.2, 1.0 - age_months / 36), 4)


# ---------------------------------------------------------------------------
# SQLite Writer
# ---------------------------------------------------------------------------

def init_sqlite(db_path: Path) -> sqlite3.Connection:
    """Create/open SQLite DB with all export tables + FTS5."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            vault_id TEXT,
            title TEXT,
            status TEXT,
            publish_date TEXT,
            created_at TEXT,
            updated_at TEXT,
            metadata TEXT,
            freshness_score REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_units (
            id TEXT PRIMARY KEY,
            note_id TEXT,
            text TEXT,
            fact_type TEXT,
            event_date TEXT,
            confidence REAL,
            status TEXT,
            context TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY,
            canonical_name TEXT,
            entity_type TEXT,
            mention_count INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_cooccurrences (
            entity_id_1 TEXT,
            entity_id_2 TEXT,
            cooccurrence_count INTEGER,
            PRIMARY KEY (entity_id_1, entity_id_2)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mental_models (
            id TEXT PRIMARY KEY,
            entity_id TEXT,
            name TEXT,
            observations TEXT,
            version INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv_entries (
            id TEXT PRIMARY KEY,
            key TEXT UNIQUE,
            value TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS export_fts
        USING fts5(source_type, source_id, title, content)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS export_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def _str_dt(val) -> str | None:
    """Convert datetime to ISO string, pass through strings."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def upsert_notes(conn: sqlite3.Connection, notes: list[dict]) -> int:
    """Upsert notes into SQLite. Returns count."""
    for note in notes:
        freshness = compute_freshness(note.get("updated_at"))
        metadata = note.get("metadata")
        if isinstance(metadata, dict):
            metadata = json.dumps(metadata)
        conn.execute(
            "INSERT OR REPLACE INTO notes (id, vault_id, title, status, publish_date, "
            "created_at, updated_at, metadata, freshness_score) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(note["id"]), str(note.get("vault_id", "")),
                note.get("title"), note.get("status"),
                _str_dt(note.get("publish_date")),
                _str_dt(note.get("created_at")), _str_dt(note.get("updated_at")),
                metadata, freshness,
            ),
        )
    conn.commit()
    return len(notes)


def upsert_memory_units(conn: sqlite3.Connection, units: list[dict]) -> int:
    """Upsert memory units into SQLite. Returns count."""
    for u in units:
        text = u.get("text", "")
        if text and len(text) > MAX_TEXT_SIZE:
            text = text[:MAX_TEXT_SIZE]
        conn.execute(
            "INSERT OR REPLACE INTO memory_units (id, note_id, text, fact_type, event_date, "
            "confidence, status, context, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(u["id"]), str(u.get("note_id", "")),
                text, u.get("fact_type"),
                _str_dt(u.get("event_date")),
                u.get("confidence"), u.get("status"), u.get("context"),
                _str_dt(u.get("created_at")), _str_dt(u.get("updated_at")),
            ),
        )
    conn.commit()
    return len(units)


def upsert_entities(conn: sqlite3.Connection, entities: list[dict]) -> int:
    """Upsert entities into SQLite. Returns count."""
    for e in entities:
        conn.execute(
            "INSERT OR REPLACE INTO entities (id, canonical_name, entity_type, mention_count) "
            "VALUES (?,?,?,?)",
            (str(e["id"]), e.get("canonical_name"), e.get("entity_type"), e.get("mention_count")),
        )
    conn.commit()
    return len(entities)


def upsert_entity_cooccurrences(conn: sqlite3.Connection, coocs: list[dict]) -> int:
    """Upsert entity co-occurrences. Returns count."""
    for c in coocs:
        conn.execute(
            "INSERT OR REPLACE INTO entity_cooccurrences (entity_id_1, entity_id_2, cooccurrence_count) "
            "VALUES (?,?,?)",
            (str(c["entity_id_1"]), str(c["entity_id_2"]), c.get("cooccurrence_count")),
        )
    conn.commit()
    return len(coocs)


def upsert_mental_models(conn: sqlite3.Connection, models: list[dict]) -> int:
    """Upsert mental models. Returns count."""
    for m in models:
        obs = m.get("observations")
        if isinstance(obs, list):
            obs = json.dumps(obs)
        conn.execute(
            "INSERT OR REPLACE INTO mental_models (id, entity_id, name, observations, version) "
            "VALUES (?,?,?,?,?)",
            (str(m["id"]), str(m.get("entity_id", "")), m.get("name"), obs, m.get("version")),
        )
    conn.commit()
    return len(models)


def upsert_kv_entries(conn: sqlite3.Connection, entries: list[dict]) -> int:
    """Upsert KV entries. Returns count."""
    for e in entries:
        conn.execute(
            "INSERT OR REPLACE INTO kv_entries (id, key, value, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (str(e["id"]), e.get("key"), e.get("value"),
             _str_dt(e.get("created_at")), _str_dt(e.get("updated_at"))),
        )
    conn.commit()
    return len(entries)


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild FTS5 index from notes + memory_units."""
    conn.execute("DELETE FROM export_fts")
    # Index notes by title
    for row in conn.execute("SELECT id, title FROM notes WHERE title IS NOT NULL"):
        conn.execute(
            "INSERT INTO export_fts (source_type, source_id, title, content) VALUES (?,?,?,?)",
            ("note", row[0], row[1], row[1]),
        )
    # Index memory units by text
    for row in conn.execute("SELECT id, text FROM memory_units WHERE text IS NOT NULL"):
        text = row[1][:MAX_TEXT_SIZE] if row[1] else ""
        conn.execute(
            "INSERT INTO export_fts (source_type, source_id, title, content) VALUES (?,?,?,?)",
            ("memory", row[0], "", text),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Sync Metadata
# ---------------------------------------------------------------------------

def get_watermark(conn: sqlite3.Connection) -> datetime | None:
    """Get last export watermark timestamp."""
    row = conn.execute("SELECT value FROM export_meta WHERE key = 'watermark'").fetchone()
    if row and row[0]:
        return datetime.fromisoformat(row[0])
    return None


def set_watermark(conn: sqlite3.Connection, ts: datetime) -> None:
    """Set export watermark timestamp."""
    conn.execute(
        "INSERT OR REPLACE INTO export_meta (key, value) VALUES ('watermark', ?)",
        (ts.isoformat(),),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def export_full(pg_conn, sqlite_conn: sqlite3.Connection) -> dict:
    """Full export: all tables from Postgres to SQLite."""
    now = datetime.now(timezone.utc)

    notes = fetch_notes(pg_conn)
    units = fetch_memory_units(pg_conn)
    entities = fetch_entities(pg_conn)
    coocs = fetch_entity_cooccurrences(pg_conn)
    models = fetch_mental_models(pg_conn)
    kv = fetch_kv_entries(pg_conn)

    n_notes = upsert_notes(sqlite_conn, notes)
    n_units = upsert_memory_units(sqlite_conn, units)
    n_ents = upsert_entities(sqlite_conn, entities)
    n_coocs = upsert_entity_cooccurrences(sqlite_conn, coocs)
    n_models = upsert_mental_models(sqlite_conn, models)
    n_kv = upsert_kv_entries(sqlite_conn, kv)

    rebuild_fts(sqlite_conn)
    set_watermark(sqlite_conn, now)

    return {
        "notes": n_notes, "memory_units": n_units, "entities": n_ents,
        "entity_cooccurrences": n_coocs, "mental_models": n_models,
        "kv_entries": n_kv, "mode": "full",
    }


def export_incremental(pg_conn, sqlite_conn: sqlite3.Connection) -> dict:
    """Incremental export: only records updated since last watermark."""
    now = datetime.now(timezone.utc)
    wm = get_watermark(sqlite_conn)

    if wm is None:
        return export_full(pg_conn, sqlite_conn)

    notes = fetch_notes(pg_conn, since=wm)
    units = fetch_memory_units(pg_conn, since=wm)
    # Entities/cooccurrences/models: always full refresh (small tables, no updated_at)
    entities = fetch_entities(pg_conn)
    coocs = fetch_entity_cooccurrences(pg_conn)
    models = fetch_mental_models(pg_conn)
    kv = fetch_kv_entries(pg_conn)

    n_notes = upsert_notes(sqlite_conn, notes)
    n_units = upsert_memory_units(sqlite_conn, units)
    n_ents = upsert_entities(sqlite_conn, entities)
    n_coocs = upsert_entity_cooccurrences(sqlite_conn, coocs)
    n_models = upsert_mental_models(sqlite_conn, models)
    n_kv = upsert_kv_entries(sqlite_conn, kv)

    rebuild_fts(sqlite_conn)
    set_watermark(sqlite_conn, now)

    return {
        "notes": n_notes, "memory_units": n_units, "entities": n_ents,
        "entity_cooccurrences": n_coocs, "mental_models": n_models,
        "kv_entries": n_kv, "mode": "incremental", "since": wm.isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Export Memex data to local SQLite for offline access",
    )
    parser.add_argument("--full", action="store_true", help="Full re-export (drop and rebuild)")
    parser.add_argument("--stats", action="store_true", help="Show export statistics")
    parser.add_argument("--db-path", type=Path, default=None, help="SQLite path (default: memex-local.db)")
    args = parser.parse_args(argv)

    db_path = args.db_path or Path(__file__).resolve().parent.parent / "memex-local.db"

    if args.stats:
        if not db_path.exists():
            print("[export] No memex-local.db found. Run export first.", file=sys.stderr)
            raise SystemExit(1)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        for table in TABLES_TO_EXPORT:
            try:
                count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
                print(f"  {table}: {count} rows")
            except sqlite3.OperationalError:
                print(f"  {table}: not exported yet")
        wm = get_watermark(conn)
        print(f"  Last export: {wm or 'never'}")
        conn.close()
        return

    pg_conn = get_pg_connection()
    if pg_conn is None:
        print("[export] Cannot connect to Postgres. Set MEMEX_DB_PASSWORD.", file=sys.stderr)
        raise SystemExit(1)

    sqlite_conn = init_sqlite(db_path)

    if args.full:
        result = export_full(pg_conn, sqlite_conn)
    else:
        result = export_incremental(pg_conn, sqlite_conn)

    sqlite_conn.close()
    pg_conn.close()
    print(f"[export] Done: {result}")


if __name__ == "__main__":
    main()
