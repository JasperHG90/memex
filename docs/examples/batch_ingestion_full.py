"""
Complete example of batch ingestion using the Memex API.
This script demonstrates how to:
1. Prepare documents with metadata (tags, description).
2. Encode content properly for the API.
3. Submit a batch job.
4. Poll for completion.
5. Handle errors and rate limiting.

Usage:
    python batch_ingestion_full.py
"""

import asyncio
import base64
import json
import os
from typing import TypedDict
import httpx

# --- Configuration ---
MEMEX_API_URL = os.getenv('MEMEX_API_URL', 'http://localhost:8000')
VAULT_ID = 'global'  # Target vault (use 'global' or a specific UUID)
BATCH_SIZE = 50  # Number of notes to generate for this example


class NotePayload(TypedDict):
    name: str
    content: str
    description: str | None
    tags: list[str]
    files: dict[str, str]  # filename -> base64_content


async def main():
    print(f'🚀 Starting batch ingestion example against {MEMEX_API_URL}')

    # 1. Generate Dummy Data
    print(f'📦 Generating {BATCH_SIZE} dummy notes...')
    notes: list[NotePayload] = []

    for i in range(BATCH_SIZE):
        # In a real app, you would read these from files
        raw_content = f"""# Note {i}

This is the content for note {i}. It mentions entity-{i % 10}."""

        # Content must be base64 encoded
        b64_content = base64.b64encode(raw_content.encode('utf-8')).decode('utf-8')

        notes.append(
            {
                'name': f'Batch Note {i}',
                'content': b64_content,
                'description': f'Automatically generated note {i}',
                'tags': ['batch-example', f'group-{i % 5}'],
                'files': {},  # Attachments would go here (filename: base64_string)
            }
        )

    # 2. Submit Batch Job
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            print('📤 Submitting batch job...')
            response = await client.post(
                f'{MEMEX_API_URL}/api/v1/ingestions/batch',
                json={
                    'notes': notes,
                    'vault_id': VAULT_ID,
                    'batch_size': 10,  # Process in chunks of 10 internally
                },
            )
            response.raise_for_status()

            job_data = response.json()
            job_id = job_data['job_id']
            print(f'✅ Job submitted! ID: {job_id}')

        except httpx.HTTPStatusError as e:
            print(f'❌ Failed to submit job: {e.response.text}')
            return
        except Exception as e:
            print(f'❌ Connection error: {e}')
            return

        # 3. Poll for Completion
        print('⏳ Polling for status...')
        while True:
            try:
                status_res = await client.get(f'{MEMEX_API_URL}/api/v1/ingestions/{job_id}')
                status_res.raise_for_status()
                status_data = status_res.json()

                status = status_data['status']
                processed = status_data.get('processed_count', 0)
                total = status_data.get('notes_count', BATCH_SIZE)
                progress = (processed / total) * 100 if total > 0 else 0

                # Clear line and update status
                print(
                    f'\rStatus: {status.upper()} | Progress: {processed}/{total} ({progress:.1f}%)',
                    end='',
                )

                if status in ['completed', 'failed']:
                    print('\n')  # New line after loop finishes
                    break

                await asyncio.sleep(1.0)

            except Exception as e:
                print(f'\n❌ Error polling status: {e}')
                break

        # 4. Report Results
        if status_data['status'] == 'completed':
            print('🎉 Batch ingestion completed successfully!')
            print(f'Created {len(status_data.get("document_ids", []))} documents.')
            if status_data.get('failed_count', 0) > 0:
                print(f'⚠️ Warning: {status_data["failed_count"]} documents failed to process.')
                if status_data.get('error_info'):
                    print('Errors:', json.dumps(status_data['error_info'], indent=2))
        else:
            print('💀 Batch job failed.')
            print('Error:', status_data.get('error'))


if __name__ == '__main__':
    asyncio.run(main())
