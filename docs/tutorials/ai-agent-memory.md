# Tutorial: Build a Python Agent with Memex Memory

In this tutorial, we will build a simple Python agent that uses Memex as its long-term memory via the REST API. By the end, you will have a working script that ingests context into Memex, retrieves relevant memories, and uses them to augment an LLM prompt.

## Prerequisites

* A running Memex server (see [Getting Started](getting-started.md))
* Python 3.12+
* `httpx` and `openai` packages installed (`uv add httpx openai`)
* An OpenAI API key (or any OpenAI-compatible provider)

## Step 1: Create the Project

First, let's create a directory for our agent and set up the dependencies:

```bash
mkdir memex-agent && cd memex-agent
```

Let's create a file called `agent.py`. We will build it step by step.

```python
import asyncio
import base64
import json

import httpx

MEMEX_URL = 'http://localhost:8000'
```

This sets up the Memex server URL. The default port is 8000.

## Step 2: Ingest Context into Memex

Now let's write a function that sends a note to Memex. The ingestion endpoint (`POST /api/v1/ingestions`) expects base64-encoded content:

```python
async def ingest_note(
    client: httpx.AsyncClient,
    name: str,
    content: str,
    vault_name: str | None = None,
) -> dict:
    '''Ingest a note into Memex.'''
    payload = {
        'name': name,
        'description': name,
        'content': base64.b64encode(content.encode()).decode(),
        'tags': ['agent'],
    }
    if vault_name:
        payload['vault_id'] = vault_name

    response = await client.post(
        f'{MEMEX_URL}/api/v1/ingestions',
        json=payload,
    )
    response.raise_for_status()
    return response.json()
```

Let's test this by ingesting some knowledge. Add the following at the bottom of the file:

```python
async def main():
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Ingest some context
        result = await ingest_note(
            client,
            name='Python asyncio overview',
            content=(
                'Python asyncio is a library for writing concurrent code '
                'using the async/await syntax. It is used as a foundation '
                'for multiple Python asynchronous frameworks that provide '
                'high-performance network and web servers, database '
                'connection libraries, and distributed task queues.'
            ),
        )
        print(f'Ingested note: {result["note_id"]}')


asyncio.run(main())
```

Run the script:

```bash
python agent.py
```

We should see output like:

```
Ingested note: 7a3b1c2d-...
```

This confirms our note was stored in Memex and is being processed for fact extraction.

## Step 3: Search Relevant Memories

Now let's write a function to search Memex for relevant memories. The search endpoint (`POST /api/v1/memories/search`) streams results as NDJSON (newline-delimited JSON):

```python
async def search_memories(
    client: httpx.AsyncClient,
    query: str,
    limit: int = 5,
) -> list[dict]:
    '''Search Memex for relevant memories.'''
    payload = {
        'query': query,
        'limit': limit,
    }

    response = await client.post(
        f'{MEMEX_URL}/api/v1/memories/search',
        json=payload,
    )
    response.raise_for_status()

    # Parse NDJSON response
    results = []
    for line in response.text.strip().split('\n'):
        if line.strip():
            results.append(json.loads(line))
    return results
```

Let's add a search to our `main` function:

```python
async def main():
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Ingest some context
        result = await ingest_note(
            client,
            name='Python asyncio overview',
            content=(
                'Python asyncio is a library for writing concurrent code '
                'using the async/await syntax. It is used as a foundation '
                'for multiple Python asynchronous frameworks.'
            ),
        )
        print(f'Ingested note: {result["note_id"]}')

        # Search for relevant memories
        memories = await search_memories(client, 'How does Python handle concurrency?')
        print(f'\nFound {len(memories)} relevant memories:')
        for mem in memories:
            print(f'  - [{mem.get("fact_type", "?")}] {mem["text"][:100]}')
```

Run the script again. We should see the extracted facts related to our query.

## Step 4: Use Memories in an LLM Prompt

Now let's put it all together. We will search Memex for relevant context and use it to augment an LLM prompt:

```python
from openai import AsyncOpenAI

openai_client = AsyncOpenAI()


async def ask_with_memory(
    http_client: httpx.AsyncClient,
    question: str,
) -> str:
    '''Ask a question using Memex memories as context.'''
    # Step 1: Retrieve relevant memories
    memories = await search_memories(http_client, question, limit=10)

    # Step 2: Format memories as context
    if memories:
        context_lines = []
        for i, mem in enumerate(memories):
            context_lines.append(f'[{i}] {mem["text"]}')
        context_block = '\n'.join(context_lines)
    else:
        context_block = 'No relevant memories found.'

    # Step 3: Build the augmented prompt
    messages = [
        {
            'role': 'system',
            'content': (
                'You are a helpful assistant. Use the following memories '
                'from your knowledge base to answer the user\'s question. '
                'Cite memories using bracket notation like [0], [1].\n\n'
                f'## Memories\n{context_block}'
            ),
        },
        {'role': 'user', 'content': question},
    ]

    # Step 4: Call the LLM
    response = await openai_client.chat.completions.create(
        model='gpt-4o-mini',
        messages=messages,
    )
    return response.choices[0].message.content
```

## Step 5: Capture the Response

A good memory agent doesn't just read — it also writes back what it learns. Let's capture the LLM's response as a new note:

```python
async def main():
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Ingest initial context
        await ingest_note(
            client,
            name='Python asyncio overview',
            content=(
                'Python asyncio is a library for writing concurrent code '
                'using the async/await syntax. It is used as a foundation '
                'for multiple Python asynchronous frameworks.'
            ),
        )

        # Ask a question with memory-augmented context
        question = 'What is Python asyncio and what is it used for?'
        answer = await ask_with_memory(client, question)
        print(f'Question: {question}')
        print(f'Answer: {answer}')

        # Capture the response back into Memex
        await ingest_note(
            client,
            name=f'Agent response: {question[:50]}',
            content=f'Q: {question}\n\nA: {answer}',
        )
        print('\nResponse captured in Memex for future reference.')


asyncio.run(main())
```

Run the complete script:

```bash
python agent.py
```

We should see the LLM's answer, informed by our Memex memories, followed by confirmation that the response was stored back in Memex.

## Step 6: Verify with a Search

Let's verify everything was captured by searching from the CLI:

```bash
memex memory search "Python asyncio"
```

We should see both the original facts and the agent's response in the search results.

## Conclusion

We have successfully built a Python agent that uses Memex as its long-term memory. The agent can ingest context, retrieve relevant memories to augment LLM prompts, and capture responses for future reference. This is the core loop of a memory-augmented AI agent: **ingest, retrieve, generate, capture**.

## Next Steps

* [Using MCP](../how-to/using-mcp.md) — use the MCP server for direct integration with Claude, Cursor, and other MCP-compatible tools (no HTTP code needed)
* [Batch Ingestion](../how-to/batch-ingestion.md) — import existing documents to bootstrap your agent's memory
* [Doc Search vs Memory Search](../how-to/doc-search-vs-memory-search.md) — choose the right search strategy for your use case
* [REST API Reference](../reference/rest-api.md) — full API documentation for all endpoints
* [Hindsight Framework](../explanation/hindsight-framework.md) — understand how Memex extracts facts and builds mental models
