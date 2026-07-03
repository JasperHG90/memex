# Build an agent on the Memex REST API

This tutorial walks you through wiring a small Python agent to a running Memex server over HTTP. By the end you will have a script that ingests a note, searches for it, traces an entity, and records an outcome on what came back — the round-trip a memory-augmented agent makes every turn.

You will write the script in two passes. The first pass uses `curl` so you see the raw wire shape. The second pass uses the typed Python client that ships with Memex (`RemoteMemexAPI`). Each step ends with the HTTP response you should see, so you can stop and fix things if reality drifts from the page.

Plan on 20 minutes.

## Prerequisites

- **A running Memex server.** Follow [Get started with Memex](getting-started.md) first. The server should respond to `curl http://localhost:8000/api/v1/health` with `{"status":"ok"}`.
- **An API key issued by that server.** Step 1 walks you through enabling auth and minting one. If you already turned auth on, have the key handy.
- **Python 3.12 or newer**, and `uv` to manage dependencies.
- **`curl`** on your `$PATH`. Most systems already have it.

This tutorial uses no LLM API keys. Search, entity walking, and outcome recording all work without an LLM. Fact extraction runs in the background on the server and uses whatever LLM you have configured there.

## Step 1: Issue an API key

Memex disables authentication by default for localhost. To exercise the REST surface the way a real agent will use it, turn auth on and mint a key.

Open the server config (`~/.config/memex/config.yaml` on Linux, `~/Library/Application Support/memex/config.yaml` on macOS — run `memex config show` if you are unsure) and add an `auth` block under `server`:

```yaml
server:
  auth:
    enabled: true
    keys:
      - key: "env:MEMEX_AGENT_KEY"
        policy: admin
        description: "tutorial agent"
```

The `env:` prefix tells Memex to read the secret from an environment variable rather than commit it to the file.

Generate a key and export it:

```bash
export MEMEX_AGENT_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
echo "$MEMEX_AGENT_KEY"
```

Copy the printed value somewhere safe. Restart the server so it picks up the new config:

```bash
memex server stop
memex server start
```

Confirm auth is on by hitting a protected endpoint without the header:

```bash
curl -i http://localhost:8000/api/v1/stats/counts
```

You should see:

```
HTTP/1.1 401 Unauthorized
content-type: application/json

{"detail":"Missing API key. Provide X-API-Key header."}
```

If you see a 200 instead, auth did not load — check the YAML indentation and try again.

## Step 2: Install the client libraries

You can talk to Memex with anything that speaks HTTP. This tutorial uses two libraries:

- `httpx` for the raw-request examples. Pulls in async support and matches the patterns Memex uses internally.
- `memex-common` for the typed client. Gives you `RemoteMemexAPI`, a thin async wrapper that returns Pydantic models instead of raw dicts.

Install both into a fresh project:

```bash
mkdir memex-agent && cd memex-agent
uv init --python 3.12
uv add httpx memex-common
```

Confirm the import works:

```bash
uv run python -c "from memex_common.client import RemoteMemexAPI; print('ok')"
```

You should see `ok`. If you see an `ImportError`, run `uv sync` and try again.

## Step 3: Send an authenticated request

Every request to a protected endpoint needs an `X-API-Key` header. The header value is the raw token you generated in Step 1.

Sanity-check with `curl`:

```bash
curl -s http://localhost:8000/api/v1/stats/counts \
  -H "X-API-Key: $MEMEX_AGENT_KEY"
```

You should see a JSON object with note and entity counts, something like:

```json
{"notes":0,"memories":0,"entities":0,"reflection_queue":0}
```

A 403 means the key value did not match what the server loaded. Re-export `MEMEX_AGENT_KEY` and restart the server.

Now create `agent.py` with the same shape in Python. The typed client takes a pre-configured `httpx.AsyncClient` so the auth header is set in one place:

```python
import asyncio
import os

import httpx
from memex_common.client import RemoteMemexAPI

MEMEX_URL = 'http://localhost:8000'
API_KEY = os.environ['MEMEX_AGENT_KEY']


async def main() -> None:
    headers = {'X-API-Key': API_KEY}
    base_url = f'{MEMEX_URL.rstrip("/")}/api/v1/'
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60.0) as client:
        api = RemoteMemexAPI(client)
        counts = await api.get_stats_counts()
        print(counts)


asyncio.run(main())
```

Run it:

```bash
uv run python agent.py
```

You should see a printed `SystemStatsCountsDTO` with the same numbers as the `curl` response.

## Step 4: Ingest a note

The ingestion endpoint is `POST /api/v1/ingestions`. The body is a `NoteCreateDTO` with the note content **Base64-encoded**. Encoding is required because the same endpoint accepts binary uploads and Pydantic's `Base64Bytes` type round-trips through JSON safely.

With `curl`:

```bash
NOTE_CONTENT=$(printf 'Python asyncio is a standard-library framework for writing concurrent code with async/await. Maintainers: Guido van Rossum and the asyncio working group.' | base64 -w0)

curl -s http://localhost:8000/api/v1/ingestions \
  -H "X-API-Key: $MEMEX_AGENT_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"Python asyncio overview\",\"description\":\"Reference notes on the asyncio standard-library framework.\",\"content\":\"$NOTE_CONTENT\",\"tags\":[\"reference\",\"python\"]}"
```

You should see an `IngestResponse`:

```json
{
  "status": "success",
  "note_id": "a4b1...",
  "unit_ids": ["c901...", "f2e3..."],
  "reason": null,
  "overlapping_notes": []
}
```

Hold on to the `note_id` and `unit_ids` for the next steps. If `status` is `skipped` and `reason` is `idempotency_check`, you already ingested an identical note in this vault — change the text or pass a fresh `note_key` and retry.

The Python version uses the typed `NoteCreateDTO` so you do not have to remember the base64 wrapping yourself. Add this to `agent.py`:

```python
from memex_common.schemas import NoteCreateDTO

CONTENT = (
    'Python asyncio is a standard-library framework for writing '
    'concurrent code with async/await. Maintainers: Guido van Rossum '
    'and the asyncio working group.'
)


async def ingest(api: RemoteMemexAPI) -> str:
    note = NoteCreateDTO(
        name='Python asyncio overview',
        description='Reference notes on the asyncio standard-library framework.',
        content=CONTENT.encode('utf-8'),
        tags=['reference', 'python'],
    )
    result = await api.ingest(note)
    print(f'Ingested note {result.note_id} with {len(result.unit_ids)} units')
    return result.note_id
```

Wire `ingest(api)` into `main()` and run the script again. You should see one line ending in a UUID and a count of extracted units.

Fact extraction runs in a background task on the server. If `unit_ids` is empty on the first call, that just means extraction has not finished yet — it does not mean ingestion failed. Search will still find the note's chunks while extraction is in flight.

## Step 5: Search for the note

Memory search runs against the memory units extracted from your notes. The endpoint is `POST /api/v1/memories/search` and it streams **newline-delimited JSON** (NDJSON), one `MemoryUnitDTO` per line. The streaming shape matters because retrieval can return dozens of units and the server starts sending them as soon as the first ranked batch is ready.

With `curl`:

```bash
curl -s http://localhost:8000/api/v1/memories/search \
  -H "X-API-Key: $MEMEX_AGENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What standard-library framework does Python use for concurrent code?","limit":5}'
```

You should see one JSON object per line, each looking roughly like:

```json
{"id":"c901...","note_id":"a4b1...","text":"Python asyncio is a standard-library framework ...","score":0.87,"confidence":0.92, ...}
```

If the response is empty, give the server a few seconds — extraction has not produced any units yet — and retry. If you still see nothing, run `memex memory search "asyncio"` from the CLI to confirm the server is processing your vault.

The Python client hides the NDJSON parsing for you:

```python
async def search(api: RemoteMemexAPI) -> list:
    results = await api.search(
        query='What standard-library framework does Python use for concurrent code?',
        limit=5,
    )
    for unit in results:
        print(f'[{unit.score:.2f}] {unit.text}')
    return results
```

Call `await search(api)` from `main()` after `ingest`. You should see one line per ranked unit, each starting with a score and ending with the extracted text.

Phrase the query as a real question, not a keyword list. The retrieval pipeline runs five strategies in parallel — semantic, keyword, graph, temporal, mental-model — and natural-language queries land in more of them than bag-of-words does.

## Step 6: Walk an entity

When Memex extracts facts, it also pulls out named entities (people, technologies, places) and links them to the units they appear in. The entity graph is how an agent moves from "find me text about X" to "tell me what else lives near X".

List the entities the server has extracted so far. The endpoint is `GET /api/v1/entities` and it also streams NDJSON:

```bash
curl -s "http://localhost:8000/api/v1/entities?limit=10&sort=-mentions" \
  -H "X-API-Key: $MEMEX_AGENT_KEY"
```

You should see one entity per line:

```json
{"id":"e7a4...","name":"Python","mention_count":2,"vault_id":"...","entity_type":"Technology"}
{"id":"5c10...","name":"Guido van Rossum","mention_count":1,"vault_id":"...","entity_type":"Person"}
```

Pick one of the IDs and fetch its co-occurrences — the other entities that show up in the same units. The endpoint is `GET /api/v1/entities/{id}/cooccurrences`:

```bash
ENTITY_ID=e7a4...  # paste the id you want
curl -s "http://localhost:8000/api/v1/entities/$ENTITY_ID/cooccurrences?limit=5" \
  -H "X-API-Key: $MEMEX_AGENT_KEY"
```

You should see one NDJSON record per neighbour:

```json
{"entity_id_1":"e7a4...","entity_id_2":"5c10...","entity_1_name":"Python","entity_2_name":"Guido van Rossum","cooccurrence_count":1, ...}
```

`cooccurrence_count` is how many units cite both entities together. Stronger edges mean tighter relationships in the vault.

The Python flow streams entities through an async generator, so you can stop after the first one:

```python
async def walk_entities(api: RemoteMemexAPI) -> None:
    async for entity in api.list_entities_ranked(limit=10):
        print(f'{entity.name} ({entity.mention_count} mentions)')
        edges = await api.get_entity_cooccurrences(entity.id, limit=5)
        for edge in edges:
            other = (
                edge['entity_2_name']
                if str(edge['entity_id_1']) == str(entity.id)
                else edge['entity_1_name']
            )
            print(f'  - {other} (count: {edge["cooccurrence_count"]})')
        break  # demo: only walk the top-ranked entity
```

Call it from `main()` and you should see the top-ranked entity printed, followed by its co-occurring neighbours indented underneath.

## Step 7: Record an outcome

A memory-augmented agent does not just read — it tells Memex which retrieved units actually helped. That signal feeds the Memory Worth score the next time the same unit is a candidate.

The endpoint is `POST /api/v1/outcomes/record`. The required body shape is `{"units": [{"unit_id": ..., "verb": "helpful" | "not_helpful" | "not_used", "reason": ...}], "vault_id": ...}`. The bare `{"success": true}` shape is legacy — it still works but the server emits a `FutureWarning` and you lose per-unit granularity.

Pick a unit ID from the search results in Step 5. You also need the vault the unit belongs to — every `MemoryUnitDTO` already carries a `vault_id` field, so paste that one. If you do not have it, ask the server for the active vault: `GET /api/v1/vaults?state=active` returns it as a one-line NDJSON stream.

With `curl`:

```bash
UNIT_ID=c901...   # paste a real id from the search
VAULT_ID=ab12...  # paste the vault_id field from the same search result

curl -s http://localhost:8000/api/v1/outcomes/record \
  -H "X-API-Key: $MEMEX_AGENT_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"units\":[{\"unit_id\":\"$UNIT_ID\",\"verb\":\"helpful\",\"reason\":\"answered a question about Python concurrency\"}],\"vault_id\":\"$VAULT_ID\"}"
```

You should see a JSON envelope reporting how many rows the server updated:

```json
{"units_updated":1,"entities_updated":1,"models_updated":0,"audit_log_id":"...","verb_counts":{"helpful":1,"not_helpful":0,"not_used":0},"coverage_ratio":null}
```

A 400 complaining about a missing `reason` means you sent a `helpful` or `not_helpful` verb without one — those two verbs require a non-empty reason so the audit trail stays useful. A 400 mentioning `source_memory_units` means you picked a `unit_id` that points at a read-only mental-model observation; pick one of the IDs from the `source_memory_units` list instead.

In Python the call is one line:

```python
async def record(api: RemoteMemexAPI, unit_id: str, vault_id: str) -> None:
    result = await api.record_outcome(
        units=[
            {
                'unit_id': unit_id,
                'verb': 'helpful',
                'reason': 'answered a question about Python concurrency',
            }
        ],
        vault_id=vault_id,
    )
    print(result)
```

Grab a unit and vault id from the previous steps' return values, call `await record(api, ...)`, and run the script. The printed dict should match the curl response.

## What you built

You wired an agent against Memex over HTTP. It authenticates with an API key, ingests notes, searches for memory units, walks the entity graph, and records outcomes on the units it used. That is the full ingest-retrieve-attribute loop a memory-augmented agent needs.

The two paths through the tutorial — `curl` and `RemoteMemexAPI` — are interchangeable in production. The typed client handles base64 wrapping, NDJSON parsing, and Pydantic validation for you; the raw `httpx` path stays useful when you are debugging a wire-shape mismatch or scripting from a non-Python runtime.

The script you wrote is the smallest version that exercises every verb a real agent needs. Real agents add a system prompt, an LLM call, multi-turn memory, and per-turn outcome scoring. The shape stays the same.

## Next steps

- [Tutorial: Get started with Memex](getting-started.md) — if you skipped ahead, the install-and-first-search walkthrough is the canonical starting point.
- [How-to: Use Memex over MCP](../how-to/integrations/hermes-plugin.md) — skip the HTTP code entirely if your agent runs in Claude, Cursor, or another MCP host.
- [Reference: REST API](../reference/api-routes.md) — the full endpoint catalogue, including every parameter on the search and ingestion routes.
- [Explanation: the Hindsight Framework](../explanation/how-memex-works/high-level-architecture.md) — why Memex stores facts the way it does, and how reflection turns them into mental models.
