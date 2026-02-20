"""Memex lifecycle model and response models"""

from typing import TYPE_CHECKING, List

from pydantic import BaseModel, Field, PrivateAttr

from memex_common.config import MemexConfig
from memex_core.storage.filestore import FileStore
from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine

if TYPE_CHECKING:
    from memex_core.api import MemexAPI
    from memex_common.client import RemoteMemexAPI


class AppContext(BaseModel):
    """Application context for the Memex MCP server."""

    config: MemexConfig = Field(..., description='The Memex configuration settings.')
    _file_store: FileStore = PrivateAttr()
    _meta_store_engine: AsyncPostgresMetaStoreEngine = PrivateAttr()
    _api: 'MemexAPI | RemoteMemexAPI' = PrivateAttr()

    model_config = {'arbitrary_types_allowed': True}


class NERModel(BaseModel):
    """A named entity recognized in a note."""

    text: str = Field(..., description='The recognized entity text.')
    type: str = Field(..., description='The type of entity (e.g., PERSON, ORG).')


class FactItem(BaseModel):
    """A fact item in a narrative."""

    statement: str
    confidence: float
    note_uuid: str


class ExperienceItem(BaseModel):
    """An experience item in a narrative."""

    statement: str
    note_uuid: str


class OpinionItem(BaseModel):
    """An opinion item in a narrative."""

    statement: str
    reasoning: str
    note_uuid: str


class Narrative(BaseModel):
    """The model-generated narrative extracted from the note."""

    fact: List[FactItem] = Field(default_factory=list)
    experience: List[ExperienceItem] = Field(default_factory=list)
    opinion: List[OpinionItem] = Field(default_factory=list)
    user_opinion: List[OpinionItem] = Field(default_factory=list)
