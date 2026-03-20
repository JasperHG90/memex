# How to Ingest Documents in Batch

This guide shows you how to import many documents into Memex at once, using either the CLI or the REST API.

## Prerequisites

* A running Memex server (`memex server start`)
* Documents to import (Markdown, PDF, text files)
* For API usage: Python 3.12+ with `httpx` installed

## Instructions

### Option A: CLI Directory Ingestion

1. **Run the CLI import command**

   Point `memex note add` at a directory. Memex recursively scans and ingests all supported files.

   ```bash
   memex note add --file ./my-notes/ --vault project-x
   ```

   This processes files synchronously — the CLI waits for each file to be summarized before moving to the next.

2. **Import with assets**

   To attach supporting files (images, PDFs) to a note, use `--asset` alongside `--file`:

   ```bash
   memex note add --file ./report.md --asset ./diagram.png --vault project-x
   ```

   > **Note:** `--asset` cannot be used with a directory `--file`. Point `--file` to a single file when using `--asset`.

### Option B: REST API Batch Jobs (Asynchronous)

For large-scale imports (hundreds of documents), use the `/ingestions/batch` endpoint. This queues work in the background so your client is never blocked.

1. **Prepare your notes**

   Each note requires `name`, `content` (base64-encoded), and optional `description`, `tags`, and `files`:

   ```python
   import base64

   note = {
       'name': 'Meeting Notes 2025-01',
       'content': base64.b64encode(b'# Meeting Notes\n\nKey decisions...').decode(),
       'description': 'January planning meeting notes',
       'tags': ['meetings', 'planning'],
       'files': {}  # filename -> base64 string for attachments
   }
   ```

2. **Submit the batch job**

   ```python
   import httpx

   async with httpx.AsyncClient(timeout=60.0) as client:
       response = await client.post(
           'http://localhost:8000/api/v1/ingestions/batch',
           json={
               'notes': [note],
               'vault_id': 'global',
               'batch_size': 10
           }
       )
       job_id = response.json()['job_id']
   ```

3. **Poll for completion**

   ```python
   import asyncio

   while True:
       status_res = await client.get(f'http://localhost:8000/api/v1/ingestions/{job_id}')
       data = status_res.json()
       if data['status'] in ('completed', 'failed'):
           break
       await asyncio.sleep(1.0)
   ```

4. **Handle errors in the results**

   Check `failed_count` and `error_info` in the response:

   ```python
   if data['status'] == 'completed':
       result = data.get('result', {})
       print(f'Created {len(result.get("note_ids", []))} notes.')
       if result.get('failed_count', 0) > 0:
           print(f'Failed: {result["failed_count"]}')
           print(f'Errors: {result.get("errors")}')
   else:
       print(f'Job failed: {data.get("error")}')
   ```

   A complete, runnable example is available in [`docs/examples/batch_ingestion_full.py`](../examples/batch_ingestion_full.py).

### Option C: Single-Note Background Ingestion

For one-off imports that should not block the client, append `?background=true` to any single-note ingestion endpoint. The server returns `202 Accepted` immediately with a `job_id` you can poll.

## Error Handling

| Error | Cause | Fix |
| :--- | :--- | :--- |
| `409 Conflict` | Duplicate content hash | Note already exists — safe to skip |
| `413 Payload Too Large` | File exceeds server limit | Split into smaller files or increase `max_content_size` in config |
| `429 Too Many Requests` | Rate limit hit | Reduce `batch_size` or wait between submissions |
| `500 Internal Server Error` | Server-side failure | Check server logs; retry the failed notes |

## Verification

To verify that batch ingestion was successful, list recent notes in the target vault:

```bash
memex --vault project-x note list
```

Or poll the job status via the API:

```bash
curl http://localhost:8000/api/v1/ingestions/<job_id>
```

## Tips

- **Idempotency**: Memex checks for existing content using a hash. Re-ingesting the same folder skips duplicates.
- **Rate limiting**: Batch jobs are processed by background workers. Configure the number of workers in `config.yaml` to tune throughput.
- **Vaults**: Always specify a `--vault` to keep project data organized.

## See Also

* [Organizing with Vaults](organize-with-vaults.md) — vault isolation
* [Configuring Memex](configure-memex.md) — worker and rate limit settings
* [REST API Reference](../reference/rest-api.md) — full endpoint documentation
