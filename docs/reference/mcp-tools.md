# MCP Tools Reference

The Memex MCP server exposes the following tools to AI assistants.

| Tool | Description | Parameters |
| :--- | :--- | :--- |
| `memex_search` | Search memories and documents using TEMPR. | `query`, `limit`, `vault_ids`, `strategies`, `token_budget` |
| `memex_doc_search` | Search for source documents using hybrid retrieval. | `query`, `limit`, `expand_query`, `reason`, `summarize` |
| `memex_read_note` | Retrieve the full content of a note. | `note_id` |
| `memex_get_page_index` | Get the hierarchical page index (table of contents) for a document. | `document_id` |
| `memex_get_node` | Retrieve the full text content of a specific document section. | `node_id` |
| `memex_get_lineage` | Trace the provenance of a memory unit or document. | `unit_id`, `entity_type` |
| `memex_reflect` | Trigger a reflection cycle on an entity or the whole vault. | `entity_id`, `limit`, `vault_id` |
| `memex_add_note` | Save a new note with optional attachments. | `title`, `markdown_content`, `description`, `author`, `tags`, `supporting_files`, `vault_id`, `document_key` |
| `memex_get_template` | Get a markdown template for note creation (e.g., ADR). | `type` |
| `memex_list_assets` | List all files (images, PDFs) attached to a document. | `document_id` |
| `memex_get_resource` | Retrieve the actual content of an asset file. | `path` |
| `memex_batch_ingest` | Asynchronously ingest multiple local files. | `file_paths`, `vault_id`, `batch_size` |
| `memex_get_batch_status` | Check the progress and results of a batch job. | `job_id` |
| `memex_active_vault` | Get the currently active vault for the server session. | None |
