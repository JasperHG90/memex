# Batch Ingestion

Batch ingestion allows you to import many documents at once. Memex provides two primary ways to do this: via the CLI for local files, and via the Python API for programmatic ingestion.

## 1. CLI Directory Ingestion (Synchronous)

The easiest way to import a folder of notes (e.g., from Obsidian) is using `memex memory add`.

```bash
# Recursively scan and ingest all files in a directory
memex memory add --file ./my-vault/ --vault project-x
```

- **Behavior**: The CLI recursively scans the directory, uploads files to the server, and waits for the server to summarize each one.
- **Assets**: If a directory contains images or PDFs, the CLI will automatically detect them if they are referenced or if you use the `--asset` flag for specific files.

## 2. API Batch Jobs (Asynchronous)

For large-scale imports or integrations, use the `/ingest/batch` endpoint. This is the recommended method for processing hundreds of documents without blocking the client.

### Workflow
1.  **Submit**: POST a list of notes to `/api/v1/ingest/batch`.
2.  **Poll**: The server returns a `job_id`. Use this to poll `/api/v1/ingest/batch/{job_id}` for progress.
3.  **Finish**: Once the status is `completed`, you will receive a list of the generated `document_ids`.

### Python Example

A complete, runnable example is available in `docs/examples/batch_ingestion_full.py`.

Here is a full example:

```python
import asyncio
import base64
import json
import os
import random
from typing import Any, TypedDict
import httpx

# --- Configuration ---
MEMEX_API_URL = os.getenv("MEMEX_API_URL", "http://localhost:8000")
VAULT_ID = "global"  # Target vault (use 'global' or a specific UUID)
BATCH_SIZE = 50      # Number of notes to generate for this example


class NotePayload(TypedDict):
    name: str
    content: str
    description: str | None
    tags: list[str]
    files: dict[str, str]  # filename -> base64_content


async def main():
    print(f"🚀 Starting batch ingestion example against {MEMEX_API_URL}")

    # 1. Generate Dummy Data
    print(f"📦 Generating {BATCH_SIZE} dummy notes...")
    notes: list[NotePayload] = []

    for i in range(BATCH_SIZE):
        # In a real app, you would read these from files
        raw_content = f"# Note {i}\n\nThis is the content for note {i}. It mentions entity-{i%10}."

        # Content must be base64 encoded
        b64_content = base64.b64encode(raw_content.encode("utf-8")).decode("utf-8")

        notes.append({
            "name": f"Batch Note {i}",
            "content": b64_content,
            "description": f"Automatically generated note {i}",
            "tags": ["batch-example", f"group-{i%5}"],
            "files": {} # Attachments would go here (filename: base64_string)
        })

    # 2. Submit Batch Job
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            print("📤 Submitting batch job...")
            response = await client.post(
                f"{MEMEX_API_URL}/api/v1/ingest/batch",
                json={
                    "notes": notes,
                    "vault_id": VAULT_ID,
                    "batch_size": 10  # Process in chunks of 10 internally
                }
            )
            response.raise_for_status()

            job_data = response.json()
            job_id = job_data["job_id"]
            print(f"✅ Job submitted! ID: {job_id}")

        except httpx.HTTPStatusError as e:
            print(f"❌ Failed to submit job: {e.response.text}")
            return
        except Exception as e:
            print(f"❌ Connection error: {e}")
            return

        # 3. Poll for Completion
        print("⏳ Polling for status...")
        while True:
            try:
                status_res = await client.get(f"{MEMEX_API_URL}/api/v1/ingest/batch/{job_id}")
                status_res.raise_for_status()
                status_data = status_res.json()

                status = status_data["status"]
                processed = status_data.get("processed_count", 0)
                total = status_data.get("notes_count", BATCH_SIZE)
                progress = (processed / total) * 100 if total > 0 else 0

                # Clear line and update status
                print(f"Status: {status.upper()} | Progress: {processed}/{total} ({progress:.1f}%)", end="\r")

                if status in ["completed", "failed"]:
                    print("\n")  # New line after loop finishes
                    break

                await asyncio.sleep(1.0)

            except Exception as e:
                print(f"\n❌ Error polling status: {e}")
                break

        # 4. Report Results
        if status_data["status"] == "completed":
            print("🎉 Batch ingestion completed successfully!")
            print(f"Created {len(status_data.get('document_ids', []))} documents.")
            if status_data.get("failed_count", 0) > 0:
                print(f"⚠️ Warning: {status_data['failed_count']} documents failed to process.")
                if status_data.get("error_info"):
                    print("Errors:", json.dumps(status_data["error_info"], indent=2))
        else:
            print("💀 Batch job failed.")
            print("Error:", status_data.get("error"))

if __name__ == "__main__":
    asyncio.run(main())
```

## Considerations

- **Idempotency**: Memex checks for existing content using a hash of the content and metadata. Re-ingesting the same folder will skip existing documents.
- **Rate Limiting**: Batch jobs are processed in the background by workers. You can configure the number of workers in your `config.yaml` to speed up large imports.
- **Vaults**: Always specify a `vault_id` to keep your project data organized and isolated from other projects.
