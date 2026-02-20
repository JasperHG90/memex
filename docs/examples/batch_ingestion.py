import asyncio
import httpx
from typing import Any

# Configuration
BASE_URL = 'http://localhost:8000/api/v1'
VAULT_ID = '00000000-0000-0000-0000-000000000000'  # Replace with a real vault ID


async def run_batch_ingestion():
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Prepare batch of documents
        # Note: Content-addressability is deterministic based on original_text and metadata (excluding uuid/date_created)
        # We must base64 encode the content for NoteDTO
        import base64

        notes = []
        raw_docs: list[dict[str, Any]] = [
            {
                'name': 'Intro Note',
                'content': 'Memex is a long-term memory system for LLMs.',
                'description': 'Introduction to Memex',
                'tags': ['intro'],
            },
            {
                'name': 'Batch Ingestion',
                'content': 'Batch ingestion allows for processing many documents asynchronously.',
                'description': 'Documentation for batching',
            },
            {
                'name': 'Lineage',
                'content': 'The lineage endpoint helps trace the origin of mental models.',
                'description': 'Lineage docs',
            },
        ]

        for doc in raw_docs:
            b64_content = base64.b64encode(doc['content'].encode('utf-8')).decode('utf-8')
            notes.append(
                {
                    'name': doc['name'],
                    'content': b64_content,
                    'description': doc['description'],
                    'tags': doc.get('tags', []),
                    'files': {},
                }
            )

        payload = {'notes': notes, 'vault_id': VAULT_ID}

        print(f'Submitting {len(notes)} documents for batch ingestion...')

        # 2. Submit the batch
        response = await client.post(f'{BASE_URL}/ingest/batch', json=payload)

        if response.status_code != 202:
            print(f'Error submitting batch: {response.status_code}')
            print(response.text)
            return

        job_data = response.json()
        job_id = job_data['job_id']
        print(f'Batch submitted successfully. Job ID: {job_id}')

        # 3. Poll for status
        print('Polling for job status...')
        while True:
            status_response = await client.get(f'{BASE_URL}/ingest/batch/{job_id}')
            if status_response.status_code != 200:
                print(f'Error checking status: {status_response.status_code}')
                break

            status_data = status_response.json()
            status = status_data['status']
            processed = status_data.get('processed_count', 0)
            total = len(notes)

            print(f'Status: {status} ({processed}/{total} processed)')

            if status == 'completed':
                print('Batch ingestion completed successfully!')
                print(f'Processed documents: {status_data.get("document_ids", [])}')
                break
            elif status == 'failed':
                print(f'Batch ingestion failed: {status_data.get("error", "Unknown error")}')
                break

            await asyncio.sleep(2)  # Wait before next poll


if __name__ == '__main__':
    try:
        asyncio.run(run_batch_ingestion())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f'An unexpected error occurred: {e}')
