# Memex API

The Memex API is built with FastAPI.

## Interactive Documentation

When the server is running, visit:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

## Key Endpoints

### Ingestion
- `POST /api/v1/ingest`: Ingest a note.
- `POST /api/v1/ingest/url`: Ingest from URL.
- `POST /api/v1/ingest/file`: Ingest from server-side file.
- `POST /api/v1/ingest/upload`: Ingest from client-side file upload.
- `POST /api/v1/ingest/batch`: Start batch job.
- `GET /api/v1/ingest/batch/{job_id}`: Poll batch job status.

### Retrieval & Memory
- `POST /api/v1/retrieve`: General memory search.
- `POST /api/v1/recall/summary`: Generate an AI summary for search results.
- `GET /api/v1/memories/{unit_id}`: Get memory unit details.
- `DELETE /api/v1/memories/{unit_id}`: Delete a memory unit.

### Documents
- `GET /api/v1/documents`: List documents.
- `POST /api/v1/documents/search`: Search documents.
- `GET /api/v1/documents/recent`: Get recent documents.
- `GET /api/v1/documents/{id}`: Get document details.
- `GET /api/v1/documents/{id}/page_index`: Get the page index (slim tree) for a document.
- `DELETE /api/v1/documents/{id}`: Delete a document.
- `GET /api/v1/nodes/{id}`: Get document node details.

### Entities & Reflection
- `GET /api/v1/entities`: Search entities / stream entities ranked by hybrid score.
- `GET /api/v1/entities/top`: Get top entities by mention count.
- `GET /api/v1/entities/{id}`: Get entity details.
- `GET /api/v1/entities/{id}/mentions`: Get entity mentions.
- `GET /api/v1/entities/{id}/cooccurrences`: Get entity co-occurrences.
- `GET /api/v1/entities/cooccurrences`: Get bulk co-occurrences for entities.
- `DELETE /api/v1/entities/{id}`: Delete an entity.
- `DELETE /api/v1/entities/{id}/mental-model`: Delete a mental model for a specific entity.
- `POST /api/v1/reflect`: Trigger reflection on an entity.
- `POST /api/v1/reflect/batch`: Trigger reflection on a batch of entities.
- `GET /api/v1/reflect/queue`: Fetch items from the reflection queue.
- `POST /api/v1/belief/adjust`: Adjust belief confidence for a memory unit.

### Lineage
- `GET /api/v1/lineage/{entity_type}/{entity_id}`: Retrieve the lineage of an entity.

### Vaults
- `GET /api/v1/vaults`: List vaults.
- `GET /api/v1/vaults/active`: Get active vault.
- `GET /api/v1/vaults/defaults`: Get default vaults.
- `POST /api/v1/vaults`: Create vault.
- `GET /api/v1/vaults/resolve/{identifier}`: Resolve vault identifier to UUID.
- `DELETE /api/v1/vaults/{id}`: Delete vault.

### Stats & Resources
- `GET /api/v1/stats/counts`: Get system statistics counts.
- `GET /api/v1/stats/token-usage`: Get daily token usage.
- `GET /api/v1/resources/{path}`: Retrieve raw resource file.
