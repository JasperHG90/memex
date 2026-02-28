# How to Manage Database Migrations

This guide shows you how to manage PostgreSQL schema migrations using the `memex db` CLI commands, which wrap [Alembic](https://alembic.sqlalchemy.org/).

## Prerequisites

* Memex installed (`uv tool install memex-cli`)
* A running PostgreSQL database with pgvector
* Database connection configured (via `memex config init` or `MEMEX_META_STORE__DSN` environment variable)

## Instructions

### 1. Check the Current Migration Status

```bash
memex db current
```

This shows which migration revision the database is currently at. If no output appears, no migrations have been applied yet.

### 2. Apply Pending Migrations

```bash
memex db upgrade
```

This runs all pending migrations up to the latest (`head`). Memex uses advisory locking (`SELECT ... FOR UPDATE`) to prevent concurrent migration races.

To upgrade to a specific revision instead of `head`:

```bash
memex db upgrade <revision>
```

### 3. Roll Back a Migration

```bash
memex db downgrade
```

This rolls back one migration step (default: `-1`). To roll back to a specific revision:

```bash
memex db downgrade <revision>
```

### 4. View Migration History

```bash
memex db history
```

Lists all migration revisions in order, showing which have been applied.

### 5. Stamp an Existing Database

If you have a database that was created via `SQLModel.metadata.create_all()` (e.g., before migrations were introduced) and already has the correct schema:

```bash
memex db stamp head
```

This marks the database as being at the `head` revision without actually running any migrations.

### 6. Generate a New Migration (Development)

When you've made changes to SQLModel definitions in the codebase:

```bash
memex db revision -m "add webhook_deliveries table"
```

This auto-detects schema differences between your models and the database and generates a new migration script in `packages/core/alembic/versions/`. The `--autogenerate` flag is enabled by default.

To generate an empty migration script for manual editing:

```bash
memex db revision -m "custom data migration" --no-autogenerate
```

## How It Works

Memex uses Alembic with an async SQLAlchemy engine (asyncpg). The migration configuration lives in `packages/core/alembic/`:

- `alembic.ini` — Alembic configuration (database URL is resolved programmatically)
- `env.py` — Migration environment with advisory locking, pgvector support, and async engine setup
- `versions/` — Migration scripts

The database URL is resolved from your Memex configuration (`meta_store.dsn`) or the `MEMEX_META_STORE__DSN` environment variable.

## Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| `alembic.ini not found` | Ensure `memex-core` is installed: `uv pip install memex-cli` |
| `Connection refused` | Check PostgreSQL is running and `MEMEX_META_STORE__DSN` is correct |
| `Relation already exists` | Database was created with `create_all` — run `memex db stamp head` |
| Concurrent migration conflict | Advisory locking handles this automatically; retry if it fails |

## See Also

* [CLI Commands — db](../reference/cli-commands.md#db) — full reference for all `memex db` subcommands
* [Configuration](../reference/configuration.md) — database connection settings (`meta_store.dsn`)
