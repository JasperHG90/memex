"""Suite discovery and loading.

A Suite resolves from one of three sources, in priority order:

1. Built-in subpackage of ``memex_eval.suites`` (in-tree)
2. A ``memex_eval.suites`` entry-point published by a separate
   pip-installed package (out-of-tree plugin)
3. An absolute or relative filesystem path to a directory exporting
   ``SUITE: Suite``

Plugin packages declare suites in their ``pyproject.toml``::

    [project.entry-points."memex_eval.suites"]
    acme_retrieval = "acme_eval_suites.acme_retrieval"
    acme_lint = "acme_eval_suites.acme_lint"

Each entry-point target must be a Python module that exposes
``SUITE: Suite``. Same shape as built-in suites.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import pkgutil
from pathlib import Path
from types import ModuleType

from memex_eval.suite.base import Suite

logger = logging.getLogger('memex_eval.suite.loader')

_SUITES_PACKAGE = 'memex_eval.suites'
_ENTRY_POINT_GROUP = 'memex_eval.suites'


class SuiteNotFound(Exception):
    pass


def _entry_point_map() -> dict[str, importlib.metadata.EntryPoint]:
    """Return ``{ep.name: ep}`` for every plugin-published suite."""
    try:
        eps = importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:
        # Python <3.10 EntryPoints API. Defensive — we require >=3.12 anyway.
        eps = importlib.metadata.entry_points().get(_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
    return {ep.name: ep for ep in eps}


def discover_suite_names() -> list[str]:
    """Return every discoverable suite name (built-in + entry-point plugins)."""
    names: set[str] = set()
    try:
        pkg = importlib.import_module(_SUITES_PACKAGE)
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.ispkg and not info.name.startswith('_'):
                names.add(info.name)
    except ModuleNotFoundError:
        pass
    names.update(_entry_point_map().keys())
    return sorted(names)


def _load_module_from_path(path: Path) -> ModuleType:
    if not (path / '__init__.py').is_file():
        raise SuiteNotFound(f'No __init__.py at {path}')
    spec = importlib.util.spec_from_file_location(
        f'_loaded_suite.{path.name}', path / '__init__.py'
    )
    if spec is None or spec.loader is None:
        raise SuiteNotFound(f'Could not import suite from {path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_suite(name_or_path: str | Path) -> Suite:
    """Load a Suite by name (built-in or entry-point) or by directory path.

    Resolution order for a string ``name``: built-in subpackage → entry-point
    plugin → SuiteNotFound. A string with a slash or leading dot, or a
    ``Path``, is treated as a filesystem location.
    """
    if isinstance(name_or_path, Path) or (
        isinstance(name_or_path, str) and ('/' in name_or_path or name_or_path.startswith('.'))
    ):
        mod = _load_module_from_path(Path(name_or_path).resolve())
    else:
        # Try built-in subpackage first
        mod = None
        try:
            mod = importlib.import_module(f'{_SUITES_PACKAGE}.{name_or_path}')
        except ModuleNotFoundError:
            # Fall back to entry-point plugin
            ep = _entry_point_map().get(str(name_or_path))
            if ep is None:
                raise SuiteNotFound(f'Suite {name_or_path!r} not found') from None
            try:
                mod = ep.load()
            except Exception as e:
                raise SuiteNotFound(
                    f'Entry-point {name_or_path!r} ({ep.value}) failed to import: {e}'
                ) from e

    suite = getattr(mod, 'SUITE', None)
    if not isinstance(suite, Suite):
        raise SuiteNotFound(
            f'{getattr(mod, "__name__", name_or_path)} does not export a `SUITE: Suite` constant'
        )
    return suite


def discover_suites() -> list[Suite]:
    """Load every discoverable suite. Failures are logged and skipped."""
    out: list[Suite] = []
    for name in discover_suite_names():
        try:
            out.append(load_suite(name))
        except Exception as e:
            logger.warning('Failed to load suite %r: %s', name, e)
    return out


__all__ = [
    'SuiteNotFound',
    'load_suite',
    'discover_suites',
    'discover_suite_names',
]
