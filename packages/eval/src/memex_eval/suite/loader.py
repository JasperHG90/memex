"""Filesystem-driven suite discovery and loading.

A Suite is a Python subpackage under ``packages/eval/src/memex_eval/suites/``
that exports a top-level ``SUITE: Suite`` constant.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from memex_eval.suite.base import Suite

logger = logging.getLogger('memex_eval.suite.loader')

_SUITES_PACKAGE = 'memex_eval.suites'


class SuiteNotFound(Exception):
    pass


def discover_suite_names() -> list[str]:
    """Walk ``memex_eval.suites/`` and return every importable subpackage name."""
    try:
        pkg = importlib.import_module(_SUITES_PACKAGE)
    except ModuleNotFoundError:
        return []
    names: list[str] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.ispkg and not info.name.startswith('_'):
            names.append(info.name)
    return sorted(names)


def load_suite(name_or_path: str | Path) -> Suite:
    """Load a Suite by name (subpackage of memex_eval.suites) or by directory path.

    Validates referential integrity at load time (Suite._validate_referential_integrity).
    """
    if isinstance(name_or_path, Path) or (
        isinstance(name_or_path, str) and ('/' in name_or_path or name_or_path.startswith('.'))
    ):
        # Path-based load — import from filesystem
        path = Path(name_or_path).resolve()
        if not (path / '__init__.py').is_file():
            raise SuiteNotFound(f'No __init__.py at {path}')
        spec = importlib.util.spec_from_file_location(  # type: ignore[attr-defined]
            f'_loaded_suite.{path.name}', path / '__init__.py'
        )
        if spec is None or spec.loader is None:
            raise SuiteNotFound(f'Could not import suite from {path}')
        mod = importlib.util.module_from_spec(spec)  # type: ignore[attr-defined]
        spec.loader.exec_module(mod)
    else:
        try:
            mod = importlib.import_module(f'{_SUITES_PACKAGE}.{name_or_path}')
        except ModuleNotFoundError as e:
            raise SuiteNotFound(f'Suite {name_or_path!r} not found') from e

    suite = getattr(mod, 'SUITE', None)
    if not isinstance(suite, Suite):
        raise SuiteNotFound(
            f'{getattr(mod, "__name__", name_or_path)} does not export a `SUITE: Suite` constant'
        )
    return suite


def discover_suites() -> list[Suite]:
    """Load every suite found under memex_eval.suites/."""
    out: list[Suite] = []
    for name in discover_suite_names():
        try:
            out.append(load_suite(name))
        except Exception as e:
            logger.warning('Failed to load suite %r: %s', name, e)
    return out


__all__ = ['SuiteNotFound', 'load_suite', 'discover_suites', 'discover_suite_names']
