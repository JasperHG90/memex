# Back up and export a vault

Your memory lives in two places: Postgres (the MetaStore — notes, memory units, entities, links, vault summaries) and the FileStore (assets and uploaded source documents like PDFs).<code-ref path="packages/core/src/memex_core/storage/metastore.py" lines="83-128" /><code-ref path="packages/core/src/memex_core/storage/filestore.py" lines="372-509" /> Note bodies sit in the Postgres `note.original_text` column, not on the FileStore.<code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="247-276" /> A full backup means snapshotting both stores at the same point in time.

This guide covers three jobs: take a full backup, restore from one, and export a single vault for portability or sharing.

## Prerequisites

* A running Memex installation you can stop briefly (the server holds a Postgres connection pool).
* Shell access to the machine running Postgres, with `pg_dump` and `pg_restore` matching your server's major version.
* For local FileStores: `rsync` on the source and target.
* For S3 or GCS FileStores: the relevant CLI (`aws` or `gcloud`) authenticated against your account.
* Disk space for the dump roughly equal to your Postgres size on disk plus the FileStore root.

## Procedure

The four jobs below can run independently. Run all of 1 and 2 for a full backup; run 4 alone for portability.

### 1. Back up Postgres with `pg_dump`

Stop the Memex server first so no writes land mid-dump.

```bash
memex server stop
```

Then run a custom-format dump. The custom format (`-Fc`) is compressed and lets you do partial restores later.

```bash
pg_dump \
  --host=localhost \
  --port=5432 \
  --username=postgres \
  --dbname=memex \
  --format=custom \
  --file=memex-$(date +%Y%m%d-%H%M%S).dump
```

Adjust the connection flags to match your `MEMEX_SERVER__META_STORE__INSTANCE__*` settings.<code-ref path="packages/core/src/memex_core/storage/db_url.py" lines="10-30" /> The dump captures every table — vaults, notes, memory units, chunks, entities, mental models, vault summaries — at the transaction point when the dump started.

Restart the server when the dump finishes:

```bash
memex server start
```

For automated nightly backups, you can leave the server running and accept the small risk of a half-written reflection result; pair the dump with a logical replica if you need read-consistent backups without downtime.

### 2. Back up the FileStore

Find your FileStore root in your Memex config. The default on Linux is `~/.local/share/memex/notes/` (the framework appends `notes/` to the configured `root`).<code-ref path="packages/common/src/memex_common/config.py" lines="121-149" />

**Local FileStore.** Mirror the directory with `rsync`:

```bash
rsync -av --delete /path/to/memex-data/ /backup/memex-data-$(date +%Y%m%d)/
```

The trailing slash on the source matters — without it `rsync` nests the source directory inside the target.

**S3 FileStore.** Use S3's own versioning and cross-region replication; they outperform anything you can script. Turn on bucket versioning once and let the bucket be its own backup. For a point-in-time copy, run:

```bash
aws s3 sync s3://my-memex-bucket/ s3://my-memex-backup/$(date +%Y%m%d)/
```

**GCS FileStore.** Same pattern with `gcloud`:

```bash
gcloud storage rsync --recursive gs://my-memex-bucket/ gs://my-memex-backup/$(date +%Y%m%d)/
```

Take the FileStore copy as close in time to the Postgres dump as you can manage. If a note was added between dump and copy, its row will land in the restored Postgres but its asset bytes will be missing — that is the only failure mode worth planning around.

### 3. Restore from a backup

Restoring is the same two steps in reverse. Drop the live database first if you are restoring over a corrupted one:

```bash
memex server stop
dropdb --host=localhost --username=postgres memex
createdb --host=localhost --username=postgres memex
```

Restore the dump:

```bash
pg_restore \
  --host=localhost \
  --port=5432 \
  --username=postgres \
  --dbname=memex \
  --clean \
  --if-exists \
  memex-20260520-143000.dump
```

Then restore the FileStore. For local stores, reverse the `rsync`:

```bash
rsync -av --delete /backup/memex-data-20260520/ /path/to/memex-data/
```

For S3 or GCS, `aws s3 sync` or `gcloud storage rsync` works the same way in reverse. Restart the server and Memex will read both stores as if no time had passed.

```bash
memex server start
```

You do not need to run `memex database upgrade` after restoring a dump from the same Memex version — the dump already carries the `alembic_version` row.<code-ref path="packages/core/src/memex_core/storage/metastore.py" lines="129-196" /> If you restore into a newer Memex, run `memex database upgrade` to apply pending migrations.

### 4. Export a vault for portability

For sharing a vault, archiving one for analytics, or moving a slice of memory between installations, use `memex vault snapshot export`.<code-ref path="packages/cli/src/memex_cli/vaults.py" lines="374-488" />

```bash
memex vault snapshot export project-hindsight --output ./snapshot-hindsight/
```

This writes a self-describing directory: a `manifest.json` declaring snapshot version, source vault, alembic head, and embedding-model identity; one JSONL file per database table scoped to that vault; one folder per note containing `note.md` plus an `assets/` subfolder with rewritten asset paths.<code-ref path="packages/core/src/memex_core/services/snapshot/exporter.py" lines="346-408" /><code-ref path="packages/core/src/memex_core/services/snapshot/manifest.py" lines="68-95" /> The export refuses to overwrite an existing snapshot directory and writes the `manifest.json` last, so a partial failure leaves a `.exporting` marker rather than a half-written snapshot.

Read the layout straight from your filesystem — the Markdown in `notes/*/note.md` is the same text you ingested, in plain UTF-8. The export refuses the global vault and any vault named `global` or `default`.<code-ref path="packages/core/src/memex_core/services/snapshot/exporter.py" lines="78-84" />

**One-way export.** This snapshot format is for downstream consumers (analytics tools, eval suites, ML pipelines). Memex does not ship a general `snapshot import` command — only the eval framework reads snapshots back in, and it does so to skip extraction during reruns, not to merge a vault into a different installation.<code-ref path="packages/core/src/memex_core/services/snapshot/exporter.py" lines="1-15" /> If you want to move a vault between Memex installations, take a full Postgres + FileStore backup (steps 1 and 2) of the source, restore it (step 3) on the target, and prune the unwanted vaults afterwards with `memex vault delete`.

## Verification

Restore the backup into a scratch installation, then run a search that should hit a note you remember being there.

```bash
memex memory search "the project kickoff"
```

You should see the same memory units with the same UUIDs as on the production instance — UUIDs are preserved through `pg_dump` / `pg_restore`.<code-ref path="packages/cli/src/memex_cli/memory.py" lines="369-572" /> Then check that an asset-bearing note still resolves to its files:

```bash
memex note find "project kickoff"
```

The output lists matching notes with their `assets` paths. Open one of those paths on the restored FileStore root and confirm the file is there. If both checks pass, the backup is sound.

For a snapshot export (step 4), open `manifest.json` and check `table_counts` against what you expect. `memex vault summary <vault>` on the source gives you a baseline to compare against.<code-ref path="packages/cli/src/memex_cli/vaults.py" lines="251-298" /> Then read one `notes/<dir>/note.md` and confirm the body matches what you ingested.

## Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| `pg_restore: error: input file appears to be a text format dump` | Your dump was written without `--format=custom`. Use `psql -f` to load it instead of `pg_restore`. |
| `pg_restore: warning: errors ignored on restore: N` with `extension "vector" is not available` | The target Postgres lacks pgvector. Install pgvector first (`CREATE EXTENSION vector`); Memex will not run without it.<code-ref path="packages/core/src/memex_core/storage/metastore.py" lines="180-182" /> |
| `pg_restore: server version: 15.4; pg_dump version: 16.1` | Your client tools are newer than the server. Either upgrade the server or install matching client tools — restoring across major versions is unsupported. |
| Restored database, but notes return `404 asset not found` when read | The FileStore restore lagged the Postgres restore. Re-run the `rsync` or `aws s3 sync` step and confirm the source path matches `file_store.root` in your config. |
| Snapshot export aborts with `Refusing to overwrite existing snapshot` | The target directory already holds a `manifest.json`. Remove the directory contents (or pick a fresh `--output` path) and rerun. |
| Snapshot export writes assets but `metadata.json` shows `filestore_path: null` | The original note carried no source document (it was created via `memex note add` rather than ingested from a file). This is expected; the body still lives in `note.md`. |

## See also

* [How-to: Manage database migrations](cli-commands.md)
* [How-to: Organise content with vaults](vaults.md)
* [Reference: Configuration](../reference/configuration-options.md)
* [Explanation: Design principles — P11 local-first, zero lock-in](../explanation/design-principles.md#p11--local-first-open-source-zero-lock-in)
