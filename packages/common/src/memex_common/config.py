"""Configuration for Memex based on Persona library"""

import logging
from enum import Enum
from typing import Literal, Self, Union, Annotated, Any, TypeAlias
import pathlib as plb
import os
import re
import warnings
import yaml
from uuid import UUID

logger = logging.getLogger('memex.common.config')

from platformdirs import user_cache_dir, user_config_dir, user_data_dir, user_log_dir
from pydantic import BaseModel, Field, SecretStr, HttpUrl, field_serializer, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, PydanticBaseSettingsSource

from memex_common.types import ReasoningEffort

# Deterministic UUID for the Global Vault (namespace: memex:global)
GLOBAL_VAULT_ID = UUID('ac9b6a45-d388-5ddb-9fa9-50d4e5bca511')
GLOBAL_VAULT_NAME = 'global'

# Local config filenames to search for in CWD
LOCAL_CONFIG_NAMES = ['memex_core.yaml', '.memex.yaml', 'memex_core.config.yaml']

# Approximate characters per token for converting between char and token units.
CHARS_PER_TOKEN = 4


def deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Recursive merge of two dictionaries."""
    for k, v in update.items():
        if isinstance(v, dict) and k in base and isinstance(base[k], dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class GlobalYamlConfigSettingsSource(PydanticBaseSettingsSource):
    """
    Loads configuration from the global user config directory.
    e.g. ~/.config/memex/config.yaml
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        # Allow disabling global config search (useful for tests)
        if os.getenv('MEMEX_LOAD_GLOBAL_CONFIG', 'true').lower() == 'false':
            return {}

        config_path = plb.Path(user_config_dir('memex', appauthor=False)) / 'config.yaml'
        if config_path.exists() and config_path.is_file():
            try:
                with open(config_path, 'r') as f:
                    return yaml.safe_load(f) or {}
            except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
                logger.debug('Failed to load global config from %s: %s', config_path, e)
                return {}
        return {}


class LocalYamlConfigSettingsSource(PydanticBaseSettingsSource):
    """
    Loads configuration from the local project directory (CWD).
    e.g. .memex.yaml in the current folder.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        # 1. Explicit path via Env
        env_path = os.getenv('MEMEX_CONFIG_PATH')
        if env_path:
            config_path = plb.Path(env_path)
            if config_path.exists() and config_path.is_file():
                try:
                    with open(config_path, 'r') as f:
                        return yaml.safe_load(f) or {}
                except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
                    logger.debug('Failed to load config from %s: %s', config_path, e)
                    return {}
            return {}

        # 0. Allow disabling local config search (useful for tests)
        if os.getenv('MEMEX_LOAD_LOCAL_CONFIG', 'true').lower() == 'false':
            return {}

        # 2. Search in CWD and parents
        cwd = plb.Path.cwd()
        search_paths = [cwd, *cwd.parents]

        for directory in search_paths:
            for name in LOCAL_CONFIG_NAMES:
                p = directory / name
                if p.exists() and p.is_file():
                    try:
                        with open(p, 'r') as f:
                            return yaml.safe_load(f) or {}
                    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
                        logger.debug('Failed to load local config from %s: %s', p, e)
                        return {}

        return {}


class ConfigWithRoot(BaseModel):
    """Settings shared by a root folder."""

    root: str = Field(
        default_factory=lambda: user_data_dir('memex'),
        description='The root directory for storing data.',
    )


class FileStoreConfig(ConfigWithRoot):
    """Base configuration for file stores."""

    max_concurrent_connections: int = Field(
        default=10,
        description='Maximum number of concurrent connections to the file store. Defaults to 10.',
    )

    @property
    def notes_dir(self) -> str:
        return f'{self.root.rstrip("/")}/notes'


class LocalFileStoreConfig(FileStoreConfig):
    type: Literal['local'] = 'local'

    @property
    def root_normalized(self) -> str:
        """Get the normalized root path."""
        return str(plb.Path(self.root).expanduser().resolve())


class S3FileStoreConfig(FileStoreConfig):
    """Configuration for S3-compatible file stores (AWS S3, MinIO, etc.)."""

    type: Literal['s3'] = 's3'
    bucket: str = Field(..., description='S3 bucket name.')
    root: str = Field(default='', description='Key prefix inside the bucket.')
    region: str | None = Field(default=None, description='AWS region name.')
    endpoint_url: str | None = Field(
        default=None, description='Custom endpoint URL (e.g. for MinIO).'
    )
    access_key_id: SecretStr | None = Field(default=None, description='AWS access key ID.')
    secret_access_key: SecretStr | None = Field(default=None, description='AWS secret access key.')
    session_token: SecretStr | None = Field(default=None, description='AWS session token.')


class GCSFileStoreConfig(FileStoreConfig):
    """Configuration for Google Cloud Storage file stores."""

    type: Literal['gcs'] = 'gcs'
    bucket: str = Field(..., description='GCS bucket name.')
    root: str = Field(default='', description='Key prefix inside the bucket.')
    project: str | None = Field(default=None, description='GCP project ID.')
    token: str | None = Field(
        default=None,
        description='Path to JSON service account key, or "google_default" / "anon".',
    )
    endpoint_url: str | None = Field(
        default=None, description='Custom endpoint URL (e.g. for GCS emulator).'
    )


FileStoreBackend = Annotated[
    Union[LocalFileStoreConfig, S3FileStoreConfig, GCSFileStoreConfig],
    Field(discriminator='type'),
]


class PostgresInstanceConfig(BaseModel):
    """Configuration for a PostgreSQL instance."""

    host: str = Field(
        ...,
        description='Hostname or IP address of the PostgreSQL server.',
    )
    port: int = Field(
        5432,
        description='Port number on which the PostgreSQL server is listening. Defaults to 5432.',
    )
    database: str = Field(
        ...,
        description='Name of the PostgreSQL database to connect to.',
    )
    user: str = Field(
        ...,
        description='Username for authenticating with the PostgreSQL database.',
    )
    password: SecretStr = Field(
        ...,
        description='Password for authenticating with the PostgreSQL database. Can be provided as an environment variable.',
    )

    @property
    def connection_string(self) -> str:
        """Get the connection string for the instance."""
        return (
            f'postgresql+asyncpg://{self.user}:'
            f'{self.password.get_secret_value()}@'
            f'{self.host}:{self.port}/{self.database}'
        )


class PostgresMetaStoreConfig(BaseModel):
    """Metadata store configuration for PostgreSQL."""

    type: Literal['postgres'] = 'postgres'

    instance: PostgresInstanceConfig = Field(
        ...,
        description='Configuration for the PostgreSQL instance.',
    )

    pool_size: int = Field(
        default=20,
        description='The size of the connection pool. Defaults to 20.',
    )
    max_overflow: int = Field(
        default=30,
        description='The maximum overflow size of the connection pool. Defaults to 30.',
    )
    statement_timeout_ms: int = Field(
        default=30000,
        description='Statement timeout in milliseconds for queries. Defaults to 30000 (30s).',
    )


MetaStoreBackend = Annotated[PostgresMetaStoreConfig, Field(discriminator='type')]


class ModelConfig(BaseModel):
    """Configuration for a specific LLM model."""

    model: str = Field(
        ...,
        description="The full model identifier string (e.g. 'ollama_chat/llama3', 'gemini/gemini-3-flash-preview')",
    )
    base_url: HttpUrl | None = Field(
        default=None,
        description='Base URL for the API (e.g. for OLLAMA or local inference).',
    )
    api_key: SecretStr | None = Field(default=None, description='API Key for the model provider.')
    max_tokens: int | None = Field(default=None, description='Maximum tokens to generate.')
    temperature: float | None = Field(default=None, description='Sampling temperature.')
    reasoning_effort: ReasoningEffort | None = Field(
        default=None, description='Reasoning effort of the model (if supported)'
    )
    timeout: int = Field(
        default=120,
        ge=10,
        description=(
            'Per-request timeout in seconds for LLM calls. Prevents hanging on '
            'slow providers. Increase for large models on remote endpoints '
            '(e.g. ollama.com). Default: 120s.'
        ),
    )
    num_retries: int = Field(
        default=3,
        ge=1,
        description='Number of retries for LLM calls on failure (e.g. schema validation errors). Default: 3.',
    )

    @field_serializer('reasoning_effort')
    def serialize_reasoning_effort(self, value: ReasoningEffort | None) -> str | None:
        if value is None:
            return None
        else:
            return value.value


# ---------------------------------------------------------------------------
# Inference model backend configs (embedding, reranking)
# ---------------------------------------------------------------------------


class OnnxBackend(BaseModel):
    """Use the built-in fine-tuned ONNX model (default)."""

    type: Literal['onnx'] = 'onnx'


class LitellmEmbeddingBackend(BaseModel):
    """Use any litellm-supported embedding provider.

    Examples: ``openai/text-embedding-3-small``, ``gemini/text-embedding-004``,
    ``ollama/nomic-embed-text``, ``cohere/embed-english-v3.0``,
    ``bedrock/amazon.titan-embed-text-v2:0``.
    """

    type: Literal['litellm'] = 'litellm'
    model: str = Field(
        ...,
        description=(
            "LiteLLM model string, e.g. 'openai/text-embedding-3-small', "
            "'gemini/text-embedding-004', 'ollama/nomic-embed-text'."
        ),
    )
    api_base: HttpUrl | None = Field(
        default=None,
        description='API base URL. Required for self-hosted providers (Ollama, TEI, vLLM). '
        'Omit for cloud providers that use standard endpoints.',
    )
    api_key: SecretStr | None = Field(
        default=None,
        description='API key. Can also be set via provider env vars '
        '(OPENAI_API_KEY, GEMINI_API_KEY, etc.).',
    )
    dimensions: int | None = Field(
        default=None,
        description='Requested output dimensions (for Matryoshka / dimension-reduction models). '
        'Must match the DB vector column width or a migration is required.',
    )


class LitellmRerankerBackend(BaseModel):
    """Use any litellm-supported reranking provider.

    Examples: ``cohere/rerank-v3.5``, ``together_ai/Salesforce/Llama-Rank-V1``,
    ``voyage/rerank-2``.
    """

    type: Literal['litellm'] = 'litellm'
    model: str = Field(
        ...,
        description=(
            "LiteLLM rerank model string, e.g. 'cohere/rerank-v3.5', "
            "'together_ai/Salesforce/Llama-Rank-V1', 'voyage/rerank-2'."
        ),
    )
    api_base: HttpUrl | None = Field(
        default=None,
        description='API base URL for self-hosted reranking servers.',
    )
    api_key: SecretStr | None = Field(
        default=None,
        description='API key for the reranker provider.',
    )


class DisabledBackend(BaseModel):
    """Explicitly disable this model."""

    type: Literal['disabled'] = 'disabled'


EmbeddingBackend: TypeAlias = Annotated[
    Union[OnnxBackend, LitellmEmbeddingBackend],
    Field(discriminator='type'),
]

RerankerBackend: TypeAlias = Annotated[
    Union[OnnxBackend, LitellmRerankerBackend, DisabledBackend],
    Field(discriminator='type'),
]


class SearchStrategiesConfig(BaseModel):
    """Default enabled search strategies for memory retrieval."""

    semantic: bool = Field(default=True, description='Enable semantic (vector) search strategy.')
    keyword: bool = Field(default=True, description='Enable keyword (BM25) search strategy.')
    graph: bool = Field(default=True, description='Enable graph (entity) search strategy.')
    temporal: bool = Field(default=True, description='Enable temporal search strategy.')
    mental_model: bool = Field(
        default=True, description='Enable mental model search strategy (memory search only).'
    )


class DocSearchStrategiesConfig(BaseModel):
    """Default enabled search strategies for document search."""

    semantic: bool = Field(default=True, description='Enable semantic (vector) search strategy.')
    keyword: bool = Field(default=True, description='Enable keyword (BM25) search strategy.')
    graph: bool = Field(default=True, description='Enable graph (entity) search strategy.')
    temporal: bool = Field(default=True, description='Enable temporal search strategy.')


class ReflectionConfig(BaseModel):
    """Configuration for the Hindsight Reflection Engine."""

    weight_urgency: float = Field(
        default=0.5,
        ge=0,
        description='Weight for Accumulated Evidence (Urgency) in priority calculation.',
    )
    weight_importance: float = Field(
        default=0.2,
        ge=0,
        description='Weight for Global Frequency (Importance) in priority calculation.',
    )
    weight_resonance: float = Field(
        default=0.3,
        ge=0,
        description='Weight for User Retrieval (Resonance) in priority calculation.',
    )
    max_concurrency: int = Field(
        default=3, gt=1, description='Maximum concurrent entities to reflect on in a single batch.'
    )
    model: ModelConfig | None = Field(
        default=None,
        description='Optional override model for reflection. If None, uses extraction model.',
    )
    search_limit: int = Field(
        default=10,
        ge=0,
        description='Number of candidates to retrieve in the Hunt phase.',
    )
    similarity_threshold: float = Field(
        default=0.6,
        ge=0,
        description='Minimum similarity score for retrieving evidence.',
    )
    background_reflection_enabled: bool = Field(
        default=True,
        description='Whether to run the periodic reflection loop in the background.',
    )
    enrichment_enabled: bool = Field(
        default=True,
        description='Run Phase 6 enrichment after reflection to evolve contributing memory units.',
    )
    background_reflection_interval_seconds: int = Field(
        default=600,
        ge=10,
        description='Interval in seconds between background reflection runs.',
    )
    background_reflection_batch_size: int = Field(
        default=10,
        gt=0,
        description='Number of entities to process in each background reflection batch.',
    )
    tail_sampling_rate: float = Field(
        default=0.05,
        description='Rate for tail sampling of traces/memories (0.0 to 1.0). Defaults to 5%.',
        ge=0,
        le=1,
    )
    min_priority: float = Field(
        default=0.3,
        ge=0,
        le=1.0,
        description='Minimum priority score required for an entity to be selected for reflection.',
    )
    stale_processing_timeout_seconds: int = Field(
        default=1800,
        ge=60,
        description=(
            'Seconds after which a PROCESSING item is considered stale and reset to PENDING. '
            'Prevents items from being stuck forever when reflection fails mid-flight.'
        ),
    )

    @model_validator(mode='after')
    def _validate_weight_scores(v: 'ReflectionConfig'):
        """Assert that weights add up to 1"""
        weight = v.weight_urgency + v.weight_importance + v.weight_resonance
        if weight > 1:
            raise ValueError(
                "'Urgency', 'resonance', and 'importance' weights should count up to 1 exactly."
            )
        return v


ExtractionStrategy: TypeAlias = Literal['simple', 'page_index']


class SimpleTextSplitting(BaseModel):
    """Text splitting config for the simple (flat CDC chunking) strategy."""

    strategy: Literal['simple'] = 'simple'
    chunk_size_tokens: int = Field(
        default=1000,
        description='Target size for content-defined blocks in tokens.',
    )
    chunk_overlap_tokens: int = Field(
        default=50,
        description='Number of overlapping tokens between chunks.',
    )


class PageIndexTextSplitting(BaseModel):
    """Text splitting config for the page_index (hierarchical) strategy."""

    strategy: Literal['page_index'] = 'page_index'
    scan_chunk_size_tokens: int = Field(
        default=20_000,
        description='Max tokens per LLM scan call. Documents under this limit are scanned in one call.',
    )
    block_token_target: int = Field(
        default=2000,
        description='Target token count per block.',
    )
    short_doc_threshold_tokens: int = Field(
        default=500,
        description='Documents below this token count with no headers bypass PageIndex.',
    )
    max_node_length_tokens: int = Field(
        default=1250,
        description='Max tokens per node before triggering refinement.',
    )
    min_node_tokens: int = Field(
        default=0,
        description='Nodes with this many tokens or fewer are skipped during indexing. '
        'Set to e.g. 25 to drop trivially short sections.',
    )
    model: ModelConfig | None = Field(
        default=None,
        description='Model for PageIndex LLM calls. If None, uses server default.',
    )
    scan_max_concurrency: int = Field(
        default=5,
        ge=1,
        description='Max concurrent LLM scan calls during page_index extraction. '
        'Reduce on memory-constrained hosts (e.g. set to 1 on a Jetson Orin Nano) '
        'to prevent GPU/RAM exhaustion. Separate from ExtractionConfig.max_concurrency, '
        'which governs fact extraction.',
    )
    refine_max_concurrency: int = Field(
        default=5,
        ge=1,
        description='Max concurrent _refine_tree_recursively LLM calls. '
        'Mirrors scan_max_concurrency. Reduce on memory-constrained hosts.',
    )
    summarize_max_concurrency: int = Field(
        default=5,
        ge=1,
        description='Max concurrent leaf, parent, and block summary LLM calls. '
        'Shared across the three summary fan-outs '
        '(_generate_summaries_parallel leaf + parent, _generate_block_summaries). '
        'Reduce on memory-constrained hosts.',
    )
    gap_rescan_threshold_tokens: int = Field(
        default=2000,
        ge=500,
        description='Minimum gap size (in tokens) between detected headers that '
        'triggers a secondary LLM re-scan to recover omitted headers. '
        'Gaps larger than scan_chunk_size_tokens are sub-chunked using the same '
        'chunking logic as the primary scan.',
    )


TextSplitting = Annotated[
    SimpleTextSplitting | PageIndexTextSplitting, Field(discriminator='strategy')
]


class ExtractionConfig(BaseModel):
    """Configuration for data extraction."""

    model: ModelConfig | None = Field(
        default=None,
        description='Model for data extraction. If None, uses server default.',
    )

    text_splitting: TextSplitting = Field(
        default_factory=PageIndexTextSplitting,
        description='Text splitting strategy and its configuration.',
    )

    max_concurrency: int = Field(
        default=5,
        description='Maximum number of concurrent LLM calls for fact extraction.',
    )

    wedge_watchdog_seconds: int | None = Field(
        default=None,
        ge=1,
        description='Opt-in wedge watchdog. When set, an OS-thread watchdog '
        'fires once and dumps all-thread tracebacks via faulthandler when no '
        'extraction stage has decremented within this many seconds while at '
        'least one in-flight gauge is > 0. None (default) disables it.',
    )

    @property
    def active_strategy(self) -> ExtractionStrategy:
        """Return the active extraction strategy name."""
        return self.text_splitting.strategy


class RelationConfig(BaseModel):
    """Configuration for note/unit relationship enrichment in search results."""

    top_k_related: int = Field(
        default=3,
        description='Max related notes returned per search result. 0 = disable related notes.',
    )
    max_shared_entities: int = Field(
        default=0,
        description='Max entity names included per related note (for explainability). '
        '0 = omit shared_entities from results (saves tokens).',
    )
    entity_fanout_cap: int = Field(
        default=50,
        description='Entities with mention_count above this are excluded from relation queries '
        '(too generic to be informative).',
    )
    max_links: int = Field(
        default=3,
        description='Max contradiction links inlined per search result. '
        '0 = omit all inline links (including contradictions).',
    )


class RetrievalConfig(BaseModel):
    """Configuration for retrieval settings."""

    token_budget: int = Field(
        default=1000,
        description='Maximum token budget for retrieval results (greedy packing).',
    )

    graph_retriever_type: str = Field(
        default='entity_cooccurrence',
        description='Graph retrieval strategy type: "entity_cooccurrence", "causal", or "link_expansion".',
    )

    retrieval_strategies: SearchStrategiesConfig = Field(
        default_factory=SearchStrategiesConfig,
        description='Default enabled search strategies for memory retrieval.',
    )

    similarity_threshold: float = Field(
        default=0.3,
        description='Minimum pg_trgm similarity score for entity name matching in graph strategies.',
    )
    temporal_decay_days: float = Field(
        default=30.0,
        description='Half-life in days for temporal decay scoring.',
    )
    temporal_decay_base: float = Field(
        default=2.0,
        description='Base for temporal decay exponential (score = base ^ (-days / decay_days)).',
    )
    rrf_k: int = Field(
        default=60,
        description='Reciprocal Rank Fusion constant (higher = more uniform blending).',
    )
    candidate_pool_size: int = Field(
        default=60,
        description='Number of candidates per strategy in multi-strategy RRF retrieval.',
    )
    mmr_lambda: float | None = Field(
        default=0.9,
        description='MMR diversity lambda. None=disabled, 0.9=conservative.',
    )
    mmr_embedding_weight: float = Field(
        default=0.6,
        description='Embedding cosine weight in hybrid similarity kernel.',
    )
    mmr_entity_weight: float = Field(
        default=0.4,
        description='Entity Jaccard weight in hybrid similarity kernel.',
    )
    superseded_threshold: float = Field(
        default=0.3,
        description='Confidence below this marks a unit as superseded. Used by contradiction detection.',
    )
    temporal_extraction_enabled: bool = Field(
        default=True,
        description='Enable NLP-based temporal constraint extraction from queries using dateparser.',
    )
    temporal_concretization_enabled: bool = Field(
        default=True,
        description=(
            'Enable LLM-assisted fallback for temporal expressions that the regex '
            'extractor cannot resolve (e.g. "during the onboarding").'
        ),
    )
    fact_type_partitioned_rrf: bool = Field(
        default=False,
        description='Run RRF independently per fact type, then interleave results.',
    )
    fact_type_budget: int = Field(
        default=20,
        description='Per-type candidate limit when fact_type_partitioned_rrf is enabled.',
    )
    reranking_recency_alpha: float = Field(
        default=0.2,
        description='Multiplicative recency boost strength for cross-encoder reranking. '
        '0 = no boost (backward compatible).',
    )
    reranking_temporal_alpha: float = Field(
        default=0.2,
        description='Multiplicative temporal proximity boost strength for cross-encoder reranking. '
        '0 = no boost (backward compatible).',
    )
    reranker: RerankerBackend = Field(
        default_factory=OnnxBackend,
        description='Reranker model backend. Default: built-in ONNX cross-encoder.',
    )
    reranker_batch_size: int = Field(
        default=0,
        description='Max documents per ONNX reranker inference call. '
        '0 = all at once (no batching). Lower values reduce peak GPU memory.',
    )
    causal_weight_threshold: float = Field(
        default=0.3,
        description='Minimum link weight for causal graph expansion in memory_links.',
    )
    graph_semantic_seeding: bool = Field(
        default=True,
        description='Enable semantic seeding for graph retrieval strategies.',
    )
    graph_semantic_seed_top_k: int = Field(
        default=5,
        description='Number of top-K memory units for semantic seed entity discovery.',
    )
    graph_semantic_seed_weight: float = Field(
        default=0.7,
        description='Weight for semantic seed entities (lower than NER weight of 1.0).',
    )
    link_expansion_causal_threshold: float = Field(
        default=0.3,
        description='Minimum weight for causal links in link-expansion graph strategy.',
    )
    relations: RelationConfig = Field(
        default_factory=RelationConfig,
        description='Settings for note/unit relationship enrichment in search results.',
    )


class ContradictionConfig(BaseModel):
    """Configuration for retain-time contradiction detection."""

    enabled: bool = Field(
        default=True,
        description='Enable contradiction detection after extraction.',
    )
    alpha: float = Field(
        default=0.1,
        description='Hindsight step size for confidence adjustment.',
    )
    similarity_threshold: float = Field(
        default=0.5,
        description='Min cosine similarity for candidate retrieval.',
    )
    max_candidates_per_unit: int = Field(
        default=15,
        description='Max candidates per flagged unit.',
    )
    superseded_threshold: float = Field(
        default=0.3,
        description='Confidence below this = superseded.',
    )
    model: ModelConfig | None = Field(
        default=None,
        description='LLM model for classification. None = use extraction model.',
    )


class Permission(str, Enum):
    """Granular permissions for API key access control."""

    READ = 'read'
    WRITE = 'write'
    DELETE = 'delete'


class Policy(str, Enum):
    """Built-in access policies that map to permission sets."""

    READER = 'reader'
    WRITER = 'writer'
    ADMIN = 'admin'


POLICY_PERMISSIONS: dict[Policy, frozenset[Permission]] = {
    Policy.READER: frozenset({Permission.READ}),
    Policy.WRITER: frozenset({Permission.READ, Permission.WRITE}),
    Policy.ADMIN: frozenset({Permission.READ, Permission.WRITE, Permission.DELETE}),
}


class ApiKeyConfig(BaseModel):
    """An API key bound to a policy with optional vault scoping.

    The ``key`` field supports an ``env:VAR_NAME`` prefix to resolve the
    secret from an environment variable instead of storing it in the YAML
    config file.  Example::

        keys:
          - key: "env:MEMEX_ADMIN_KEY"
            policy: admin
    """

    key: SecretStr = Field(
        description=(
            'The API key secret, or "env:VAR_NAME" to read from an environment variable. '
            'Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        ),
    )
    policy: Policy = Field(description='Access policy: reader, writer, or admin.')
    vault_ids: list[str] | None = Field(
        default=None,
        description='Vault IDs or names this key is scoped to. None = all vaults.',
    )
    read_vault_ids: list[str] | None = Field(
        default=None,
        description=(
            'Additional vault IDs or names this key may read from (read-only). '
            'Effective read scope = vault_ids + read_vault_ids. '
            'Only valid when vault_ids is set.'
        ),
    )
    description: str | None = Field(
        default=None,
        description='Human-readable label for this key.',
    )

    @model_validator(mode='after')
    def validate_read_vault_ids(self) -> Self:
        """Reject read_vault_ids when vault_ids is None (unrestricted)."""
        if self.vault_ids is None and self.read_vault_ids is not None:
            raise ValueError(
                'read_vault_ids cannot be set when vault_ids is None '
                '(key already has unrestricted access to all vaults). '
                'If you need read-only access to specific vaults, use a '
                'separate key with policy: reader and vault_ids instead.'
            )
        return self

    @model_validator(mode='before')
    @classmethod
    def resolve_env_key(cls, data: Any) -> Any:
        """Resolve ``env:VAR_NAME`` references in the key field."""
        if isinstance(data, dict):
            raw_key = data.get('key')
            if isinstance(raw_key, str) and raw_key.startswith('env:'):
                var_name = raw_key[4:]
                value = os.environ.get(var_name)
                if not value:
                    raise ValueError(
                        f'Environment variable {var_name!r} is not set '
                        f'(referenced by key "env:{var_name}").'
                    )
                data = {**data, 'key': value}
        return data


class AuthConfig(BaseModel):
    """Authentication configuration for API key-based auth."""

    enabled: bool = Field(
        default=False,
        description='Enable API key authentication. Disabled by default for localhost.',
    )
    keys: list[ApiKeyConfig] = Field(
        default_factory=list,
        description=(
            'API keys with associated policies. '
            'Generate keys with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        ),
    )
    exempt_paths: list[str] = Field(
        default_factory=lambda: ['/api/v1/health', '/api/v1/ready', '/api/v1/metrics'],
        description='Paths that do not require authentication.',
    )
    webhook_secret: SecretStr | None = Field(
        default=None,
        description=(
            'Shared secret for HMAC-SHA256 webhook signature validation. '
            'Callers must send X-Webhook-Signature header with '
            'hex(HMAC-SHA256(secret, request_body)).'
        ),
    )

    @model_validator(mode='before')
    @classmethod
    def reject_legacy_api_keys(cls, data: Any) -> Any:
        """Reject the old flat api_keys format with a clear migration message."""
        if isinstance(data, dict) and 'api_keys' in data:
            raise ValueError(
                'The "api_keys" field has been replaced by "keys". '
                'Each key must now specify a policy. Example:\n'
                '  keys:\n'
                '    - key: "your-secret-key"\n'
                '      policy: admin\n'
                '      description: "My admin key"'
            )
        return data


class CorsConfig(BaseModel):
    """Configuration for CORS (Cross-Origin Resource Sharing)."""

    origins: list[str] = Field(
        default_factory=lambda: ['http://localhost:5173', 'http://localhost:3000', 'null'],
        description='Allowed origins for CORS requests.',
    )
    allow_credentials: bool = Field(
        default=True,
        description='Whether to allow credentials (cookies, auth headers) in CORS requests.',
    )
    allow_methods: list[str] = Field(
        default_factory=lambda: ['*'],
        description='HTTP methods allowed in CORS requests.',
    )
    allow_headers: list[str] = Field(
        default_factory=lambda: ['*'],
        description='HTTP headers allowed in CORS requests.',
    )
    allow_origin_regex: str | None = Field(
        default=r'(moz|chrome)-extension://.*',
        description=(
            'Regex pattern for additional allowed CORS origins '
            '(matched with re.fullmatch). Default allows browser extensions.'
        ),
    )


class RateLimitConfig(BaseModel):
    """Configuration for API rate limiting."""

    enabled: bool = Field(
        default=False,
        description='Enable rate limiting. Disabled by default.',
    )
    ingestion: str = Field(
        default='10/minute',
        description='Rate limit for ingestion endpoints.',
    )
    search: str = Field(
        default='60/minute',
        description='Rate limit for search endpoints.',
    )
    batch: str = Field(
        default='5/minute',
        description='Rate limit for batch endpoints.',
    )
    default: str = Field(
        default='120/minute',
        description='Default rate limit for all other endpoints.',
    )


class LoggingConfig(BaseModel):
    """Configuration for logging."""

    log_file: str = Field(
        default_factory=lambda: str(plb.Path(user_log_dir('memex', appauthor=False)) / 'memex.log'),
        description='Path to the log file.',
    )
    level: str = Field(
        default='WARNING',
        description='Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).',
    )
    json_output: bool = Field(
        default=False,
        description='Output logs as JSON (for production log aggregators).',
    )


class CircuitBreakerConfig(BaseModel):
    """Configuration for the LLM call circuit breaker."""

    enabled: bool = Field(
        default=True,
        description='Whether the circuit breaker is enabled.',
    )
    failure_threshold: int = Field(
        default=5,
        description='Number of consecutive failures before opening the circuit.',
        ge=1,
    )
    reset_timeout_seconds: float = Field(
        default=60.0,
        description='Seconds to stay open before allowing a probe request.',
        gt=0,
    )


class TracingConfig(BaseModel):
    """Configuration for OpenTelemetry tracing (e.g. Arize Phoenix, Jaeger, Grafana Tempo)."""

    enabled: bool = Field(
        default=False,
        description='Whether OpenTelemetry tracing is enabled.',
    )
    endpoint: str = Field(
        default='http://localhost:6006/v1/traces',
        description='OTLP HTTP endpoint to send traces to.',
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description='Optional headers for the OTLP exporter (e.g. auth tokens).',
    )
    service_name: str = Field(
        default='memex',
        description='Service name reported in traces.',
    )


class MemoryConfig(BaseModel):
    """Configuration for memory subsystems."""

    extraction: ExtractionConfig = Field(
        default_factory=ExtractionConfig,
        description='Configuration for fact extraction settings.',
    )

    reflection: ReflectionConfig = Field(
        default_factory=ReflectionConfig, description='Configuration for reflection settings.'
    )

    retrieval: RetrievalConfig = Field(
        default_factory=RetrievalConfig,
        description='Configuration for retrieval settings.',
    )

    contradiction: ContradictionConfig = Field(
        default_factory=ContradictionConfig,
        description='Configuration for contradiction detection.',
    )

    circuit_breaker: CircuitBreakerConfig = Field(
        default_factory=CircuitBreakerConfig,
        description='Configuration for the LLM call circuit breaker.',
    )


class DocumentConfig(BaseModel):
    """Configuration for document search and processing."""

    model: ModelConfig | None = Field(
        default=None,
        description='Model for skeleton-tree reasoning & answer synthesis. If None, uses server default.',
    )
    search_strategies: DocSearchStrategiesConfig = Field(
        default_factory=DocSearchStrategiesConfig,
        description='Default enabled search strategies for document search.',
    )
    mmr_lambda: float | None = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description=(
            'Default MMR lambda for document search. '
            '1.0 = pure relevance, 0.0 = max diversity. '
            'None disables MMR. Overridden by per-request mmr_lambda.'
        ),
    )


class VaultSummaryConfig(BaseModel):
    """Configuration for vault summary generation.

    Vault summaries are updated periodically (time-based) by checking for
    new notes since the last update. Full regeneration is available on demand.
    """

    enabled: bool = Field(
        default=True,
        description='Enable periodic vault summary generation via the scheduler.',
    )
    interval_seconds: int = Field(
        default=3600,
        ge=60,
        description='Interval in seconds between vault summary update checks. Default: 1 hour.',
    )
    model: ModelConfig | None = Field(
        default=None,
        description='Model for vault summary LLM calls. If None, uses server default.',
    )
    batch_size: int = Field(
        default=50,
        ge=10,
        le=200,
        description='Hard note-count cap per batch (safety limit). Token budget is the primary '
        'batching control; this prevents any single batch from exceeding this many notes.',
    )
    max_batch_tokens: int = Field(
        default=8000,
        ge=1000,
        le=100000,
        description='Token budget per batch for vault summary LLM calls. Estimated from the '
        'serialized metadata payload (title, tags, chunk summaries, etc.).',
    )
    max_patch_log: int = Field(
        default=20,
        ge=1,
        le=100,
        description='Maximum number of entries in the update log.',
    )
    max_narrative_tokens: int = Field(
        default=200,
        ge=50,
        le=500,
        description='Maximum token count for the vault narrative text.',
    )
    dormant_threshold_days: int = Field(
        default=30,
        ge=1,
        description='Age in days since the most recent note past which all theme trends '
        'are forced to "dormant" on read, independent of the cached LLM value. '
        'Time-sensitive inventory fields (recent_activity, last_activity_at, '
        'days_since_last_note) are always recomputed on read; this threshold '
        'governs only the trend demotion.',
    )


class ServerConfig(BaseModel):
    """Configuration for the Memex API Server."""

    host: str = Field(
        default='127.0.0.1',
        description='Host to bind the API server to.',
    )
    port: int = Field(
        default=8000,
        description='Port to bind the API server to.',
    )
    workers: int = Field(
        default=4,
        description='Number of worker processes.',
    )
    allow_insecure: bool = Field(
        default=False,
        description=(
            'Allow binding to non-localhost addresses without authentication. '
            'When False (default), the server refuses to start on a non-localhost '
            'address unless auth is enabled.'
        ),
    )

    default_model: ModelConfig = Field(
        default_factory=lambda: ModelConfig(model='gemini/gemini-3-flash-preview'),
        description='System-wide default model. Sub-configs with model=None inherit this value.',
    )

    embedding_model: EmbeddingBackend = Field(
        default_factory=OnnxBackend,
        description='Embedding model backend. Default: built-in ONNX model. '
        'Set type=litellm to use any litellm-supported provider.',
    )

    embedding_batch_size: int = Field(
        default=0,
        description='Max texts per ONNX embedding inference call. '
        '0 = all at once (no batching). Lower values reduce peak GPU memory.',
    )

    reranker_max_concurrency: int = Field(
        default=4,
        ge=1,
        le=4,
        description=(
            'Max concurrent reranker score calls. Shared across both reranker '
            'asyncio.to_thread sites (memory/retrieval/document_search.py:243 + '
            'memory/retrieval/engine.py:1086) — one reranker model = one '
            'capacity budget. Reduce on memory-constrained hosts (e.g. set to '
            '2 on a Jetson Orin Nano 8 GiB to coexist with reranker_batch_size; '
            'see docs/how-to/memory-budget.md). Sister lever to '
            'reranker_batch_size — both must be tuned together to avoid the '
            'cuDNN allocation failure that neighboured the wedge in issue #50.'
        ),
    )
    embedding_max_concurrency: int = Field(
        default=4,
        ge=1,
        le=4,
        description=(
            'Max concurrent embedding model calls. Shared across all three '
            'embedding asyncio.to_thread sites (api.py:1287 + '
            'memory/retrieval/document_search.py:130 + '
            'memory/retrieval/engine.py:208) — one embedding model = one '
            'capacity budget. Reduce on memory-constrained hosts; see '
            'docs/how-to/memory-budget.md.'
        ),
    )
    ner_max_concurrency: int = Field(
        default=4,
        ge=1,
        le=4,
        description=(
            'Max concurrent NER model calls (memory/retrieval/engine.py:322). '
            'Reduce on memory-constrained hosts (e.g. set to 2 on a Jetson '
            'Orin Nano 8 GiB); see docs/how-to/memory-budget.md.'
        ),
    )
    reranker_call_timeout: int = Field(
        default=30,
        ge=1,
        description=(
            'Per-call timeout (seconds) for reranker model calls before the '
            'awaiting coroutine raises TimeoutError. Note: the underlying '
            'thread keeps running on timeout — the cap (reranker_max_concurrency) '
            'is what prevents thread accumulation.'
        ),
    )
    embedding_call_timeout: int = Field(
        default=30,
        ge=1,
        description=(
            'Per-call timeout (seconds) for embedding model calls. Same '
            'thread-keeps-running caveat as reranker_call_timeout.'
        ),
    )
    ner_call_timeout: int = Field(
        default=30,
        ge=1,
        description=(
            'Per-call timeout (seconds) for NER model calls. Same '
            'thread-keeps-running caveat as reranker_call_timeout.'
        ),
    )

    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description='Configuration for logging.',
    )

    auth: AuthConfig = Field(
        default_factory=AuthConfig,
        description='API key authentication. Disabled by default.',
    )

    cors: CorsConfig = Field(
        default_factory=CorsConfig,
        description='CORS (Cross-Origin Resource Sharing) configuration.',
    )

    rate_limit: RateLimitConfig = Field(
        default_factory=RateLimitConfig,
        description='Configuration for API rate limiting. Disabled by default.',
    )

    default_active_vault: str = Field(
        default=GLOBAL_VAULT_NAME,
        description='Server default vault for writes when no client preference is set.',
    )

    default_reader_vault: str = Field(
        default=GLOBAL_VAULT_NAME,
        description='Server default vault for reads when no client preference is set.',
    )

    file_store: FileStoreBackend = Field(
        default_factory=lambda: LocalFileStoreConfig(),
        description='Configuration for the file storage backend. Defaults to local file storage.',
    )
    meta_store: MetaStoreBackend = Field(
        default_factory=lambda: PostgresMetaStoreConfig(
            instance=PostgresInstanceConfig(
                host='localhost',
                database='postgres',
                user='postgres',
                port=5432,
                password=SecretStr('postgres'),
            )
        ),
        description='Configuration for the metadata storage backend.',
    )

    memory: MemoryConfig = Field(
        default_factory=MemoryConfig,
        description='Configuration for memory subsystems.',
    )

    document: DocumentConfig = Field(
        default_factory=DocumentConfig,
        description='Configuration for document search and processing.',
    )

    vault_summary: VaultSummaryConfig = Field(
        default_factory=VaultSummaryConfig,
        description='Configuration for vault summary generation.',
    )

    tracing: TracingConfig = Field(
        default_factory=TracingConfig,
        description='OpenTelemetry tracing configuration. Disabled by default.',
    )

    cache_dir: str = Field(
        default_factory=lambda: user_cache_dir('memex'),
        description='Directory for caching ML models and other artifacts.',
    )

    @model_validator(mode='after')
    def _validate_vault_names(self) -> 'ServerConfig':
        """Warn if vault names look like typos."""
        for label, name in [
            ('default_active_vault', self.default_active_vault),
            ('default_reader_vault', self.default_reader_vault),
        ]:
            if len(name) > 50:
                warnings.warn(
                    f'{label} name is suspiciously long ({len(name)} chars): "{name[:30]}..."',
                    UserWarning,
                    stacklevel=2,
                )
            if re.search(r'[^a-zA-Z0-9_\-.]', name):
                warnings.warn(
                    f'{label} "{name}" contains special characters. '
                    'Vault names typically use only alphanumeric characters, '
                    'hyphens, underscores, and dots.',
                    UserWarning,
                    stacklevel=2,
                )
        return self

    @model_validator(mode='after')
    def _check_default_db_password(self) -> 'ServerConfig':
        """Reject default database password in production mode."""
        if not isinstance(self.meta_store, PostgresMetaStoreConfig):
            return self
        pw = self.meta_store.instance.password.get_secret_value()
        is_production = os.getenv('MEMEX_ENV', '').lower() == 'production'
        if pw == 'postgres' and is_production:
            raise ValueError(
                'Default database password "postgres" is not allowed '
                'when MEMEX_ENV=production. Set a secure password via '
                'server.meta_store.instance.password or '
                'MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD.'
            )
        return self

    @model_validator(mode='after')
    def sync_default_model(self) -> 'ServerConfig':
        """Propagate default_model to sub-configs where model is None."""
        dm = self.default_model
        if self.memory.extraction.model is None:
            self.memory.extraction.model = dm
        ts = self.memory.extraction.text_splitting
        if isinstance(ts, PageIndexTextSplitting) and ts.model is None:
            ts.model = dm
        if self.memory.reflection.model is None:
            self.memory.reflection.model = dm
        if self.memory.contradiction.model is None:
            self.memory.contradiction.model = dm
        if self.document.model is None:
            self.document.model = dm
        if self.vault_summary.model is None:
            self.vault_summary.model = dm
        return self


class VaultConfig(BaseModel):
    """Client-side vault preferences.

    Controls which vault CLI/MCP writes to and searches.
    Separate from ServerConfig defaults, which are the server's own fallback.
    """

    active: str | None = Field(
        default=None,
        description='Active vault for writes. Falls back to server.default_active_vault if None.',
    )
    search: list[str] | None = Field(
        default=None,
        description='Vaults to search/read. Falls back to [active] or server default if None.',
    )


class MemexConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix='MEMEX_', env_nested_delimiter='__', extra='forbid'
    )

    server_url: str = Field(
        default='',
        description='URL of the Memex Core server used by clients (CLI, MCP). '
        'If empty, derived from server.host and server.port.',
    )

    api_key: SecretStr | None = Field(
        default=None,
        description='API key for authenticating with the Memex server. Used by CLI and MCP clients.',
    )

    vault: VaultConfig = Field(
        default_factory=VaultConfig,
        description='Client-side vault preferences (CLI, MCP). Overrides server defaults when set.',
    )

    server: ServerConfig = Field(
        default_factory=ServerConfig,
        description='Configuration for the API server.',
    )

    @property
    def write_vault(self) -> str:
        """Resolved write vault for clients: vault.active > server.default_active_vault."""
        return self.vault.active or self.server.default_active_vault

    @property
    def read_vaults(self) -> list[str]:
        """Resolved read vaults for clients: vault.search > [vault.active] > server default."""
        if self.vault.search is not None:
            return self.vault.search
        if self.vault.active is not None:
            return [self.vault.active]
        return [self.server.default_reader_vault]

    @model_validator(mode='after')
    def sync_derived_settings(self) -> 'MemexConfig':
        """Derive server_url from server.host and server.port when not explicitly set."""
        if not self.server_url:
            self.server_url = f'http://{self.server.host}:{self.server.port}'
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            LocalYamlConfigSettingsSource(settings_cls),
            GlobalYamlConfigSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


def parse_memex_config(data: dict | None = None) -> MemexConfig:
    """
    Parse memex config.
    If data is provided, it acts as overrides (via init_settings).
    Otherwise, it loads from Env -> File -> Defaults.
    """
    if data:
        return MemexConfig(**data)
    return MemexConfig()


__all__ = [
    'AuthConfig',
    'CorsConfig',
    'ConfigWithRoot',
    'FileStoreConfig',
    'LocalFileStoreConfig',
    'S3FileStoreConfig',
    'GCSFileStoreConfig',
    'FileStoreBackend',
    'PostgresInstanceConfig',
    'PostgresMetaStoreConfig',
    'MetaStoreBackend',
    'ExtractionStrategy',
    'SimpleTextSplitting',
    'PageIndexTextSplitting',
    'TextSplitting',
    'ModelConfig',
    'SearchStrategiesConfig',
    'DocSearchStrategiesConfig',
    'DocumentConfig',
    'ReflectionConfig',
    'ExtractionConfig',
    'RetrievalConfig',
    'ContradictionConfig',
    'MemoryConfig',
    'TracingConfig',
    'ServerConfig',
    'CHARS_PER_TOKEN',
    'MemexConfig',
    'parse_memex_config',
    'GLOBAL_VAULT_ID',
    'GLOBAL_VAULT_NAME',
    'VaultConfig',
    'SecretStr',
    'deep_merge',
    'GlobalYamlConfigSettingsSource',
    'LocalYamlConfigSettingsSource',
]
