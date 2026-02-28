import logging
import pathlib as plb
from typing import Self, cast
import frontmatter
import asyncio
import asyncstdlib as a

from pydantic import model_validator, field_validator, BaseModel, ConfigDict
import aiofiles
import asyncstdlib.functools


logger = logging.getLogger('memex.core.templates')


class MemexTemplateFromFile(BaseModel):
    model_config = ConfigDict(ignored_types=(asyncstdlib.functools.CachedProperty,))
    path: plb.Path
    filename: str = 'NOTE.md'

    @model_validator(mode='after')
    def validate_template(self) -> Self:
        if not self.path.exists():
            raise ValueError(f'Path does not exist: {self.path}')

        if self.path.is_dir():
            note_file = self.path / self.filename
            if not note_file.exists():
                raise ValueError(
                    f'Directory {self.path} does not contain {self.filename}. Is not a valid {self.filename} template.'
                )
        return self

    @field_validator('path')
    def validate_path_exists(cls, v: plb.Path) -> plb.Path:
        if not v.exists():
            raise ValueError(f'Path does not exist: {v}')
        return v

    @property
    def is_dir(self) -> bool:
        return self.path.is_dir()

    @a.cached_property
    async def content(self) -> bytes:
        """Load the content of the template file."""
        if self.is_dir:
            target = self.path / self.filename
        else:
            target = self.path

        async with aiofiles.open(str(target), 'rb') as f:
            content = await f.read()
        return content

    @a.cached_property
    async def frontmatter(self) -> frontmatter.Post:
        """Load the frontmatter metadata from the template file."""
        content = await self.content
        return await asyncio.to_thread(frontmatter.loads, content.decode('utf-8'))

    @a.cached_property
    async def files(self) -> dict[str, bytes]:
        """Load all files associated with the template."""
        files: dict[str, bytes] = {}

        if not self.is_dir:
            return files

        local_path_root = self.path
        glob = '**/*'

        for filename in local_path_root.glob(glob):
            if filename.is_dir():
                continue
            if filename.name == self.filename:
                continue
            async with aiofiles.open(filename, 'rb') as f:
                content = await f.read()
            relative_path = str(filename.relative_to(local_path_root))
            files[relative_path] = content
        return files

    @a.cached_property
    async def name(self) -> str | None:
        metadata = (await self.frontmatter).metadata
        if metadata is None:
            return None
        return cast(str | None, metadata.get('name', None))

    @a.cached_property
    async def description(self) -> str | None:
        metadata = (await self.frontmatter).metadata
        if metadata is None:
            return None
        return cast(str | None, metadata.get('description', None))
