# MemexAPI Reference

The `MemexAPI` class (`memex_core.api.MemexAPI`) is the main programmatic interface to Memex. All CLI commands, MCP tools, and REST API endpoints delegate to this class.

```python
from memex_core.api import MemexAPI
```

---

## Initialization

### `__init__`

```python
def __init__(
    self,
    embedding_model: FastEmbedder,
    reranking_model: FastReranker,
    ner_model: FastNERModel,
    metastore: AsyncBaseMetaStoreEngine,
    filestore: BaseAsyncFileStore,
    config: MemexConfig,
) -> None
```

Initialize the Memex API with injected storage engines and models.

### `initialize`

```python
async def initialize(self) -> None
```

Perform async initialization: ensure the global vault and active vault exist.

---

## Search & Retrieval

### `search`

```python
async def search(
    self,
    query: str,
    limit: int = 10,
    vault_ids: list[UUID | str] | None = None,
    token_budget: int | None = None,
    strategies: list[str] | None = None,
    include_stale: bool = False,
    include_superseded: bool = False,
    debug: bool = False,
    after: datetime | None = None,
    before: datetime | None = None,
    tags: list[str] | None = None,
) -> tuple[list[MemoryUnit], Any]
```

Search memory units using TEMPR multi-strategy retrieval with reranking.

### `retrieve`

```python
async def retrieve(self, request: RetrievalRequest) -> tuple[list[MemoryUnit], Any]
```

Retrieve memories using a `RetrievalRequest` object.

### `search_notes`

```python
async def search_notes(
    self,
    query: str,
    limit: int = 10,
    vault_ids: list[UUID | str] | None = None,
    expand_query: bool = False,
    fusion_strategy: str = 'rrf',
    strategies: list[str] | None = None,
    strategy_weights: dict[str, float] | None = None,
    reason: bool = False,
    summarize: bool = False,
    mmr_lambda: float | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    tags: list[str] | None = None,
) -> list[NoteSearchResult]
```

Search source notes using multi-channel fusion (RRF or position-aware blending).

### `summarize_search_results`

```python
async def summarize_search_results(self, query: str, texts: list[str]) -> str
```

Generate an AI summary with citations from search result texts.

### `resolve_source_notes`

```python
async def resolve_source_notes(self, unit_ids: list[UUID]) -> dict[UUID, UUID]
```

Map memory unit IDs to their source note IDs.

### `find_notes_by_title`

```python
async def find_notes_by_title(
    self,
    query: str,
    vault_ids: list[UUID] | None = None,
    limit: int = 5,
    threshold: float = 0.3,
) -> list[dict[str, Any]]
```

Fuzzy-search notes by title using trigram similarity.

### `embed_text`

```python
async def embed_text(self, text: str) -> list[float]
```

Generate an embedding vector for the given text.

---

## Note Management

### `ingest`

```python
async def ingest(
    self,
    note: NoteInput,
    vault_id: UUID | str | None = None,
    event_date: datetime | None = None,
) -> dict[str, Any]
```

Ingest a note into Memex. Handles extraction, entity resolution, and embedding.

### `ingest_from_url`

```python
async def ingest_from_url(
    self,
    url: str,
    vault_id: UUID | str | None = None,
    reflect_after: bool = True,
    assets: dict[str, bytes] | None = None,
) -> dict[str, Any]
```

Scrape and ingest content from a URL.

### `ingest_from_file`

```python
async def ingest_from_file(
    self,
    file_path: str | Path,
    vault_id: UUID | str | None = None,
    reflect_after: bool = True,
) -> dict[str, Any]
```

Ingest content from a local file (PDF, docx, markdown, etc.).

### `ingest_batch_internal`

```python
async def ingest_batch_internal(
    self,
    notes: list[Any],
    vault_id: UUID | str | None = None,
    batch_size: int = 32,
) -> AsyncGenerator[dict[str, Any], None]
```

Batch-ingest multiple notes. Yields cumulative progress updates per chunk (`batch_size` notes), not per individual note.

### `get_note`

```python
async def get_note(self, note_id: UUID) -> dict[str, Any]
```

Retrieve a note by ID.

### `get_note_metadata`

```python
async def get_note_metadata(self, note_id: UUID) -> dict[str, Any] | None
```

Retrieve metadata (title, tags, token count, has_assets) for a note.

### `get_note_page_index`

```python
async def get_note_page_index(self, note_id: UUID) -> dict[str, Any] | None
```

Retrieve the hierarchical page index (table of contents) for a note.

### `get_notes_metadata`

```python
async def get_notes_metadata(self, note_ids: list[UUID]) -> list[dict[str, Any]]
```

Retrieve metadata for multiple notes.

### `list_notes`

```python
async def list_notes(
    self,
    limit: int = 100,
    offset: int = 0,
    vault_id: UUID | None = None,
    vault_ids: list[UUID] | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
) -> list[Any]
```

List notes with pagination and optional date/vault filters.

### `get_recent_notes`

```python
async def get_recent_notes(
    self,
    limit: int = 5,
    vault_id: UUID | None = None,
    vault_ids: list[UUID] | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
) -> list[Any]
```

Get the most recently created notes.

### `set_note_status`

```python
async def set_note_status(
    self,
    note_id: UUID,
    status: str,
    linked_note_id: UUID | None = None,
) -> dict[str, Any]
```

Set a note's lifecycle status (`active`, `superseded`, or `appended`).

### `update_note_title`

```python
async def update_note_title(self, note_id: UUID, new_title: str) -> dict[str, Any]
```

Rename a note. Updates title in metadata, page index, and doc_metadata.

### `update_note_date`

```python
async def update_note_date(self, note_id: UUID, new_date: datetime) -> dict[str, Any]
```

Update a note's publish date and cascade the delta to all memory unit timestamps.

### `delete_note`

```python
async def delete_note(self, note_id: UUID) -> bool
```

Delete a note and all associated data (memory units, chunks, links, assets).

### `migrate_note`

```python
async def migrate_note(self, note_id: UUID, target_vault_id: UUID | str) -> dict[str, Any]
```

Move a note and all associated data to a different vault.

---

## Node Reading

### `get_node`

```python
async def get_node(self, node_id: UUID) -> NodeDTO | None
```

Retrieve a specific page-index node (section) by ID.

### `get_nodes`

```python
async def get_nodes(self, node_ids: list[UUID]) -> list[NodeDTO]
```

Retrieve multiple page-index nodes by ID.

---

## Entity Management

### `list_entities_ranked`

```python
async def list_entities_ranked(
    self,
    limit: int = 100,
    vault_ids: list[UUID] | None = None,
    entity_type: str | None = None,
) -> AsyncGenerator[Any, None]
```

Stream entities ranked by hybrid score (mention count + recency).

### `search_entities`

```python
async def search_entities(
    self,
    query: str,
    limit: int = 10,
    vault_ids: list[UUID] | None = None,
    entity_type: str | None = None,
) -> list[Any]
```

Search entities by name.

### `get_entity`

```python
async def get_entity(self, entity_id: UUID | str, vault_id: UUID | None = None) -> Any | None
```

Get an entity by ID.

### `get_entities`

```python
async def get_entities(self, entity_ids: list[UUID], vault_id: UUID | None = None) -> list[Any]
```

Get multiple entities by ID.

### `get_top_entities`

```python
async def get_top_entities(
    self,
    limit: int = 5,
    vault_ids: list[UUID] | None = None,
    entity_type: str | None = None,
) -> list[Any]
```

Get top entities by mention count.

### `get_entity_mentions`

```python
async def get_entity_mentions(
    self, entity_id: UUID | str, limit: int = 20, vault_ids: list[UUID] | None = None
) -> list[dict[str, Any]]
```

Get memory units and source notes that mention an entity.

### `get_entity_cooccurrences`

```python
async def get_entity_cooccurrences(
    self,
    entity_id: UUID | str,
    vault_ids: list[UUID] | None = None,
    limit: int = 50,
) -> list[Any]
```

Get entities that frequently co-occur with the given entity.

### `get_bulk_cooccurrences`

```python
async def get_bulk_cooccurrences(
    self, entity_ids: list[UUID], vault_ids: list[UUID] | None = None
) -> list[Any]
```

Get co-occurrences between a set of entities.

### `delete_entity`

```python
async def delete_entity(self, entity_id: UUID) -> bool
```

Delete an entity and all associated data (mental models, aliases, links, co-occurrences).

### `delete_mental_model`

```python
async def delete_mental_model(self, entity_id: UUID, vault_id: UUID) -> bool
```

Delete the mental model for an entity in a specific vault.

---

## Memory Units

### `get_memory_unit`

```python
async def get_memory_unit(self, unit_id: UUID | str) -> Any | None
```

Get a memory unit (fact, observation, or event) by ID.

### `delete_memory_unit`

```python
async def delete_memory_unit(self, unit_id: UUID) -> bool
```

Delete a memory unit and all associated data (entity links, memory links, evidence).

---

## Reflection

### `reflect`

```python
async def reflect(self, request: ReflectionRequest) -> ReflectionResult
```

Trigger reflection on a single entity. Synthesizes observations into mental models.

### `reflect_batch`

```python
async def reflect_batch(self, requests: list[ReflectionRequest]) -> list[ReflectionResult]
```

Trigger reflection on multiple entities.

### `background_reflect`

```python
async def background_reflect(self, request: ReflectionRequest) -> None
```

Queue reflection for background processing.

### `background_reflect_batch`

```python
async def background_reflect_batch(self, requests: list[ReflectionRequest]) -> None
```

Queue batch reflection for background processing.

### `get_reflection_queue_batch`

```python
async def get_reflection_queue_batch(
    self,
    limit: int = 10,
    vault_id: UUID | None = None,
    vault_ids: list[UUID] | None = None,
) -> list[Any]
```

Get items from the reflection queue.

### `claim_reflection_queue_batch`

```python
async def claim_reflection_queue_batch(
    self, limit: int = 10, vault_id: UUID | None = None
) -> list[Any]
```

Atomically claim reflection queue items for processing (uses `SELECT ... FOR UPDATE SKIP LOCKED`).

### `get_dead_letter_items`

```python
async def get_dead_letter_items(
    self,
    limit: int = 50,
    offset: int = 0,
    vault_id: UUID | None = None,
) -> list[Any]
```

List dead-lettered reflection tasks that exhausted their retries.

### `retry_dead_letter_item`

```python
async def retry_dead_letter_item(self, item_id: UUID) -> Any
```

Reset a dead-lettered item back to pending for re-processing.

---

## Lineage

### `get_lineage`

```python
async def get_lineage(
    self,
    entity_type: str,
    entity_id: UUID | str,
    direction: LineageDirection = LineageDirection.UPSTREAM,
    depth: int = 3,
    limit: int = 10,
) -> LineageResponse
```

Retrieve the provenance lineage of any entity type (`note`, `entity`, `memory_unit`, `observation`, `mental_model`).

---

## Vault Management

### `create_vault`

```python
async def create_vault(self, name: str, description: str | None = None) -> Any
```

Create a new vault.

### `delete_vault`

```python
async def delete_vault(self, vault_id: UUID) -> bool
```

Delete a vault.

### `list_vaults`

```python
async def list_vaults(self) -> list[Any]
```

List all vaults.

### `list_vaults_with_counts`

```python
async def list_vaults_with_counts(self) -> list[dict[str, Any]]
```

List all vaults with note counts.

### `get_vault_by_name`

```python
async def get_vault_by_name(self, name: str) -> Any | None
```

Get a vault by name. Returns `None` if not found.

### `validate_vault_exists`

```python
async def validate_vault_exists(self, vault_id: UUID) -> bool
```

Check if a vault exists.

### `resolve_vault_identifier`

```python
async def resolve_vault_identifier(self, identifier: UUID | str) -> UUID
```

Resolve a vault name or UUID string to a vault UUID.

---

## KV Store

### `kv_put`

```python
async def kv_put(
    self,
    vault_id: UUID | None,
    key: str,
    value: str,
    embedding: list[float] | None = None,
) -> Any
```

Create or update a key-value entry. Generates an embedding if not provided.

### `kv_get`

```python
async def kv_get(self, key: str, vault_id: UUID | None = None) -> Any | None
```

Get a KV entry by exact key. Checks vault-specific first, then global.

### `kv_search`

```python
async def kv_search(
    self,
    query_embedding: list[float],
    vault_id: UUID | None = None,
    limit: int = 5,
) -> list[Any]
```

Semantic search over KV entries by embedding similarity.

### `kv_delete`

```python
async def kv_delete(self, key: str, vault_id: UUID | None = None) -> bool
```

Delete a KV entry by key.

### `kv_list`

```python
async def kv_list(self, vault_id: UUID | None = None, limit: int = 100) -> list[Any]
```

List KV entries. Without `vault_id`, returns global entries only.

---

## Resources

### `get_resource`

```python
async def get_resource(self, path: str) -> bytes
```

Retrieve a raw resource file (image, PDF, etc.) from the filestore.

### `get_resource_path`

```python
def get_resource_path(self, path: str) -> str | None
```

Return the absolute filesystem path for a resource, or `None` for remote stores.

---

## Statistics

### `get_stats_counts`

```python
async def get_stats_counts(
    self,
    vault_id: UUID | None = None,
    vault_ids: list[UUID] | None = None,
) -> dict[str, int]
```

Get total counts for notes, memory units, entities, and reflection queue.

### `get_daily_token_usage`

```python
async def get_daily_token_usage(self) -> list[dict[str, Any]]
```

Get daily aggregated LLM token usage.
