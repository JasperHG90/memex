"""Domain service classes for MemexAPI decomposition.

Services own focused slices of functionality, while MemexAPI acts as a
backward-compatible facade that delegates to them.
"""

from memex_core.services.base import BaseService
from memex_core.services.entities import EntityService
from memex_core.services.ingestion import IngestionService
from memex_core.services.kv import KVService
from memex_core.services.lineage import LineageService
from memex_core.services.notes import NoteService
from memex_core.services.reflection import ReflectionService
from memex_core.services.search import SearchService
from memex_core.services.stats import StatsService
from memex_core.services.vaults import VaultService

__all__ = [
    'BaseService',
    'EntityService',
    'IngestionService',
    'KVService',
    'LineageService',
    'NoteService',
    'ReflectionService',
    'SearchService',
    'StatsService',
    'VaultService',
]
