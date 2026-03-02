"""Memex custom types."""

import enum


class ReasoningEffort(str, enum.Enum):
    """Enumerate reasoning effort"""

    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'


class MemexTypes(str, enum.Enum):
    """Enumerate memex types.

    The values must match the property names in MemexAPI
    to enable dynamic dispatch.
    """

    NOTE = 'note'
    KNOWLEDGE = 'knowledge'
    REFLECTION = 'reflection'


class NamedEntityEnum(str, enum.Enum):
    PERSON = 'PERSON'
    ORG = 'ORG'
    LOC = 'LOC'
    PRODUCT = 'PRODUCT'
    EVENT = 'EVENT'
    MISC = 'MISC'
    CONCEPT = 'CONCEPT'


class RelationshipTypes(str, enum.Enum):
    ENTITY = 'entity'
    CAUSAL = 'causal'
    TEMPORAL = 'temporal'
    SEMANTIC = 'semantic'


class CausalRelationshipTypes(str, enum.Enum):
    CAUSES = 'causes'
    CAUSED_BY = 'caused_by'
    ENABLES = 'enables'
    PREVENTS = 'prevents'


class FactTypes(str, enum.Enum):
    """Types of extracted facts."""

    WORLD = 'world'
    EXPERIENCE = 'experience'
    OBSERVATION = 'observation'


class FactKindTypes(str, enum.Enum):
    """Kinds of extracted facts."""

    EVENT = 'event'
    CONVERSATION = 'conversation'
    OTHER = 'other'
