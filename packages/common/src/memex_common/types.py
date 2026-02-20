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
    OPINION = 'opinion'
    OBSERVATION = 'observation'


class FactKindTypes(str, enum.Enum):
    """Kinds of extracted facts."""

    EVENT = 'event'
    CONVERSATION = 'conversation'
    OTHER = 'other'


class EvidenceType(enum.Enum):
    """
    Registry of evidence types and their associated weights for Bayesian updates.
    Weights represent the amount of 'mass' added to alpha (success) or beta (failure).
    """

    # --- Successes (Updates Alpha) ---
    USER_VALIDATION = ('user_validation', 10.0, True)  # Absolute truth from user
    EXECUTION_SUCCESS = ('execution_success', 2.0, True)  # Verified by running code
    CORROBORATION = ('corroboration', 1.0, True)  # Found in another reliable source
    LLM_CONSENSUS = ('llm_consensus', 0.5, True)  # Multiple models agree

    # --- Failures (Updates Beta) ---
    USER_REJECTION = ('user_rejection', 10.0, False)  # User says this is wrong
    CATASTROPHIC_FAILURE = ('catastrophic_failure', 5.0, False)  # System crash, data loss
    LOGICAL_CONTRADICTION = ('logical_contradiction', 3.0, False)  # Factually impossible
    EXECUTION_FAILURE = ('execution_failure', 2.0, False)  # Code didn't run
    MINOR_ERROR = ('minor_error', 0.5, False)  # Transient issue, typo

    def __init__(self, key: str, weight: float, is_success: bool):
        self.key = key
        self.weight = weight
        self.is_success = is_success
