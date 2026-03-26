"""Note creation templates shared by CLI and MCP.

Supports three layers of template discovery:
- Built-in: shipped with the package in prompts/*.toml
- Global: {filestore_root}/templates/*.toml (shared across instances)
- Local: .memex/templates/*.toml (project-scoped)

Later layers override earlier ones on slug collision.
"""

import dataclasses
import enum
import logging
import pathlib
import shutil
import tomllib

import tomli_w

logger = logging.getLogger(__name__)

BUILTIN_PROMPTS_DIR = pathlib.Path(__file__).parent / 'prompts'


# ---------------------------------------------------------------------------
# Backward-compat enum (kept for existing callers)
# ---------------------------------------------------------------------------
class NoteTemplateType(str, enum.Enum):
    TECHNICAL_BRIEF = 'technical_brief'
    GENERAL_NOTE = 'general_note'
    ADR = 'architectural_decision_record'
    RFC = 'request_for_comments'
    QUICK_NOTE = 'quick_note'


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class TemplateInfo:
    """Metadata about a discovered template."""

    slug: str
    display_name: str
    description: str
    source: str  # 'builtin' | 'global' | 'local'


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def _slug_from_filename(filename: str) -> str:
    """Derive a template slug from a .toml filename."""
    stem = pathlib.Path(filename).stem
    return stem.lower().replace('-', '_')


def _parse_toml_template(path: pathlib.Path) -> tuple[TemplateInfo, str] | None:
    """Parse a .toml template file. Returns (info, template_content) or None on error."""
    slug = _slug_from_filename(path.name)
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    except Exception:
        logger.warning('Skipping invalid TOML file: %s', path)
        return None

    template_content = data.get('template')
    if not isinstance(template_content, str):
        logger.warning("Skipping %s: missing or invalid 'template' field", path)
        return None

    display_name = data.get('name') or slug.replace('_', ' ').title()
    description = data.get('description') or 'User-defined template'

    info = TemplateInfo(
        slug=slug,
        display_name=display_name,
        description=description,
        source='',  # caller sets this
    )
    return info, template_content


class TemplateRegistry:
    """Discovers and manages templates from multiple directory layers.

    Parameters
    ----------
    template_dirs : list of (source_label, directory_path) tuples.
        Directories are scanned in order. Later entries override earlier
        ones when slugs collide, so the list should go from lowest
        priority (e.g. ``'builtin'``) to highest (e.g. ``'local'``).
    """

    def __init__(self, template_dirs: list[tuple[str, pathlib.Path]]) -> None:
        self._template_dirs = template_dirs

    # -- Discovery -----------------------------------------------------------

    def _discover(self) -> dict[str, tuple[TemplateInfo, str]]:
        """Scan all directories and return slug → (info, content) mapping."""
        templates: dict[str, tuple[TemplateInfo, str]] = {}
        for source_label, directory in self._template_dirs:
            if not directory.is_dir():
                continue
            for toml_path in sorted(directory.glob('*.toml')):
                parsed = _parse_toml_template(toml_path)
                if parsed is None:
                    continue
                info, content = parsed
                # Replace source with the layer label
                info = dataclasses.replace(info, source=source_label)
                templates[info.slug] = (info, content)
        return templates

    # -- Public API ----------------------------------------------------------

    def list_templates(self) -> list[TemplateInfo]:
        """Return metadata for all discovered templates."""
        return [info for info, _content in self._discover().values()]

    def get_template(self, slug: str) -> str:
        """Return the markdown content for a template slug.

        Raises ``KeyError`` if the slug is not found.
        """
        templates = self._discover()
        if slug not in templates:
            raise KeyError(f'Unknown template: {slug}')
        return templates[slug][1]

    def get_template_info(self, slug: str) -> TemplateInfo:
        """Return metadata for a single template slug.

        Raises ``KeyError`` if the slug is not found.
        """
        templates = self._discover()
        if slug not in templates:
            raise KeyError(f'Unknown template: {slug}')
        return templates[slug][0]

    # -- Mutation -------------------------------------------------------------

    def _resolve_scope_dir(self, scope: str) -> pathlib.Path:
        """Return the directory for the given scope label."""
        for label, directory in self._template_dirs:
            if label == scope:
                return directory
        raise ValueError(
            f'Unknown scope: {scope!r}. Available: {[label for label, _ in self._template_dirs]}'
        )

    def register(self, source_path: pathlib.Path, scope: str = 'global') -> TemplateInfo:
        """Copy a .toml template file into the target scope directory.

        Validates the file before copying. Returns the registered template info.
        """
        parsed = _parse_toml_template(source_path)
        if parsed is None:
            raise ValueError(f'Invalid template file: {source_path}')

        info, content = parsed
        _warn_no_frontmatter(info.slug, content)

        target_dir = self._resolve_scope_dir(scope)
        target_dir.mkdir(parents=True, exist_ok=True)

        dest = target_dir / source_path.name
        shutil.copy2(source_path, dest)

        return dataclasses.replace(info, source=scope)

    def register_from_content(
        self,
        slug: str,
        template: str,
        name: str | None = None,
        description: str | None = None,
        scope: str = 'global',
    ) -> TemplateInfo:
        """Create a .toml template file from inline content.

        Writes the file to the target scope directory. Returns the registered
        template info.
        """
        _warn_no_frontmatter(slug, template)

        target_dir = self._resolve_scope_dir(scope)
        target_dir.mkdir(parents=True, exist_ok=True)

        data: dict[str, str] = {}
        if name:
            data['name'] = name
        if description:
            data['description'] = description
        data['template'] = template

        dest = target_dir / f'{slug}.toml'
        dest.write_bytes(tomli_w.dumps(data).encode('utf-8'))

        display_name = name or slug.replace('_', ' ').title()
        desc = description or 'User-defined template'

        return TemplateInfo(
            slug=slug,
            display_name=display_name,
            description=desc,
            source=scope,
        )

    def delete(self, slug: str, scope: str = 'global') -> None:
        """Delete a template from the specified scope.

        Raises ``ValueError`` if the slug belongs to a built-in template
        (scope ``'builtin'``).
        """
        if scope == 'builtin':
            raise ValueError(f'Cannot delete built-in template: {slug}')

        target_dir = self._resolve_scope_dir(scope)
        path = target_dir / f'{slug}.toml'
        if not path.exists():
            raise KeyError(f'Template not found in {scope} scope: {slug}')
        path.unlink()

    @property
    def user_templates_dir(self) -> pathlib.Path | None:
        """Return the global user templates directory, if configured."""
        for label, directory in self._template_dirs:
            if label == 'global':
                return directory
        return None


# ---------------------------------------------------------------------------
# Frontmatter warning helper
# ---------------------------------------------------------------------------
def _warn_no_frontmatter(slug: str, template_content: str) -> None:
    """Emit a warning if the template markdown has no YAML frontmatter."""
    stripped = template_content.lstrip('\n')
    if not stripped.startswith('---'):
        logger.warning(
            "Template '%s' has no frontmatter. Notes created from this template "
            'may be missing metadata (e.g. dates, tags).',
            slug,
        )


# ---------------------------------------------------------------------------
# Module-level convenience API (backward compatible)
# ---------------------------------------------------------------------------
_default_registry: TemplateRegistry | None = None


def _get_default_registry() -> TemplateRegistry:
    """Return a singleton registry that only knows about built-in templates.

    Use ``TemplateRegistry`` directly for multi-layer discovery.
    """
    global _default_registry  # noqa: PLW0603
    if _default_registry is None:
        _default_registry = TemplateRegistry([('builtin', BUILTIN_PROMPTS_DIR)])
    return _default_registry


def get_template(template_type: 'NoteTemplateType | str') -> str:
    """Return the markdown template for the given type or slug."""
    slug = template_type.value if isinstance(template_type, NoteTemplateType) else template_type
    return _get_default_registry().get_template(slug)


def list_template_types() -> list[str]:
    """Return all available template slugs."""
    return [t.slug for t in _get_default_registry().list_templates()]


def list_templates() -> list[TemplateInfo]:
    """Return metadata for all available templates."""
    return _get_default_registry().list_templates()
