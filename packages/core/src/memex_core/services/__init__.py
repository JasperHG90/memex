"""Domain service classes for MemexAPI decomposition.

Services own focused slices of functionality, while MemexAPI acts as a
backward-compatible facade that delegates to them.
"""

from memex_core.services.base import BaseService
from memex_core.services.lineage import LineageService

__all__ = ['BaseService', 'LineageService']
