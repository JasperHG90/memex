"""Custom index entry models for Memex"""

import datetime as dt

from pydantic import BaseModel, Field, PrivateAttr, field_serializer


class Manifest(BaseModel):
    """A manifest representing the metadata and content structure of a note in Memex.

    The Manifest serves as the central source of truth for a note's identity,
    versioning, and associated resources (files, tags). It is designed to be
    immutable and idempotent based on the content it describes.
    """

    _manifest_path: str | None = PrivateAttr(default=None)

    version: str = Field(
        default='1.0.0',
        description='Version of the Memex manifest schema.',
    )
    uuid: str = Field(
        ...,
        description='Idempotent unique identifier for the note. Based on the contents of the note and any artifacts.',
    )
    etag: str = Field(..., description='MD5 hash of the note contents')
    date_created: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc),
        description='ISO 8601 timestamp of when the manifest was created.',
    )
    name: str = Field(
        ...,
        description='The name/title of the note.',
    )
    description: str = Field(
        ...,
        description='A brief description or summary of the note.',
    )
    files: list[str] = Field(
        ...,
        description='List of file paths associated with the note.',
    )
    tags: list[str] = Field(
        ...,
        description='List of tags associated with the note for easier retrieval.',
    )
    # TODO: add narrative & entities

    @field_serializer('date_created')
    def serialize_date_created(self, date_created: dt.datetime) -> str:
        return date_created.isoformat()
