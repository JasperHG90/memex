# Memex Storage Intelligence Briefing

## 1. Role of This Component (`src/memex/storage`)
This directory implements the core persistence layer for Memex. It provides high-level abstractions for managing note data and its associated metadata, ensuring consistency across disparate storage backends (e.g., local filesystem and PostgreSQL).

## 2. Key Technologies (Component-Specific)
*   **Async File Storage:** Uses `fsspec` and `AsyncFileSystemWrapper` for asynchronous I/O. Supports local storage by default, with an architecture designed for extensibility (e.g., S3, GCS).
*   **Async MetaStore:** Employs `asyncpg` for PostgreSQL interaction, including `pgvector` support for efficient vector similarity searches.
*   **Transactional Coordination:** Features a two-phase commit transaction manager (`AsyncTransaction`) that synchronizes file operations with database state.
*   **Data Modeling:** Leverages `Pydantic` for defining `Manifest` objects, which serve as the immutable source of truth for note metadata and versioning.

## 3. Developer Workflow (Component-Specific)
*   **Backend Extension:** To add support for new storage types, inherit from `BaseAsyncFileStore` (for files) or `AsyncBaseMetaStoreEngine` (for metadata).
*   **Atomic Operations:** ALWAYS use the `AsyncTransaction` context manager when performing operations that update both the file system and the metadata store.
*   **Staging:** File operations within a transaction are automatically staged (suffixed with `.stage_<txn_id>`) and only finalized upon successful database commit.

## 4. Mandatory Rules & Conventions (Component-Specific)
*   **Asynchrony:** All storage-related operations MUST be non-blocking and use `async/await` patterns.
*   **Concurrency Control:** Use the internal semaphores (defaulting to 20) provided by the storage classes to limit concurrent I/O operations and avoid exhausting system resources.
*   **Vector Readiness:** Any MetaStore implementation MUST support vector data types to accommodate future embedding-based retrieval features.
*   **Idempotency:** The `Manifest` UUID should be derived from note content to ensure metadata records are idempotent.
