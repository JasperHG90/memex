# Memex API

The Memex API is built with FastAPI.

## Interactive Documentation

When the server is running, visit:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

## Key Endpoints

### Ingestion
- `POST /api/v1/ingestions`: Ingest a note.
- `POST /api/v1/ingestions/url`: Ingest from URL.
- `POST /api/v1/ingestions/file`: Ingest from server-side file.
- `POST /api/v1/ingestions/upload`: Ingest from client-side file upload.
- `POST /api/v1/ingestions/batch`: Start batch job.
- `GET /api/v1/ingestions/{job_id}`: Poll batch job status.

### Memories & Retrieval
- `POST /api/v1/memories/search`: General memory search.
- `POST /api/v1/memories/summary`: Generate an AI summary for search results.
- `PATCH /api/v1/memories/{unit_uuid}/belief`: Adjust belief confidence for a memory unit.
- `GET /api/v1/memories/{unit_id}`: Get memory unit details.
- `DELETE /api/v1/memories/{unit_id}`: Delete a memory unit.

### Notes
- `GET /api/v1/notes`: List notes.
- `POST /api/v1/notes/search`: Search notes.
- `GET /api/v1/notes/{document_id}`: Get note details.
- `GET /api/v1/notes/{document_id}/page-index`: Get the page index (slim tree) for a note.
- `DELETE /api/v1/notes/{document_id}`: Delete a note.
- `GET /api/v1/notes/{id}/lineage`: Retrieve lineage of a note.
- `GET /api/v1/nodes/{id}`: Get note node details.

### Entities
- `GET /api/v1/entities`: List entities / search entities by name / get top entities by mention count (via query params).
- `GET /api/v1/entities/{id}`: Get entity details.
- `GET /api/v1/entities/{id}/mentions`: Get entity mentions.
- `GET /api/v1/entities/{id}/cooccurrences`: Get entity co-occurrences.
- `GET /api/v1/entities/{id}/lineage`: Retrieve lineage of an entity.
- `GET /api/v1/cooccurrences`: Get bulk co-occurrences for entities.
- `DELETE /api/v1/entities/{entity_id}`: Delete an entity.
- `DELETE /api/v1/entities/{entity_id}/mental-model`: Delete a mental model for a specific entity.

### Reflections
- `POST /api/v1/reflections`: Trigger reflection on an entity.
- `POST /api/v1/reflections/batch`: Trigger reflection on a batch of entities.
- `GET /api/v1/reflections`: List reflections / fetch items from reflection queue (via query params).
- `POST /api/v1/reflections/claim`: Claim reflection queue items for processing.

### Vaults
- `GET /api/v1/vaults`: List vaults / get active vault / get default vaults (via query params).
- `POST /api/v1/vaults`: Create vault.
- `GET /api/v1/vaults/{identifier}`: Get vault by ID or resolve vault identifier to UUID.
- `DELETE /api/v1/vaults/{vault_id}`: Delete vault.

### Stats & Resources
- `GET /api/v1/stats/counts`: Get system statistics counts.
- `GET /api/v1/stats/token-usage`: Get daily token usage.
- `GET /api/v1/resources/{path}`: Retrieve raw resource file.
