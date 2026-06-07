"""CLI-wide convention fences.

Walk the entire lazy-loaded command tree and assert the option conventions hold,
so new commands cannot silently drift from them:

- one long option carries the same short flag (or none) at every site it appears;
- short flags are unique within a single command;
- shared flags carry their canonical help string;
- every option/argument help string ends with a period.
"""

from __future__ import annotations

import importlib

import click
import pytest
from typer.main import get_command as typer_get_command

from memex_cli.utils import LAZY_SUBCOMMANDS

# Long options whose help text is intentionally non-uniform (distinct semantics
# per command) and therefore exempt from the canonical-help check.
_HELP_EXEMPT = {'--vault'}

_CANONICAL_HELP = {
    '--json': 'Output as JSON.',
    '--force': 'Skip confirmation.',
}

# (leaf-command-name, flag) pairs whose help is intentionally distinct.
_CANONICAL_HELP_EXEMPT = {
    # The lint catalogue dump emits more than a table; its help says so.
    ('actions', '--json'),
}


def _iter_commands(cmd: click.Command, path: tuple[str, ...] = ()):
    """Yield (path, command) for every leaf command in the tree."""
    if isinstance(cmd, click.Group):
        ctx = click.Context(cmd)
        for name in cmd.list_commands(ctx):
            sub = cmd.get_command(ctx, name)
            if sub is None:
                continue
            yield from _iter_commands(sub, path + (name,))
    else:
        yield path, cmd


def _all_leaf_commands():
    leaves = []
    for import_path in LAZY_SUBCOMMANDS.values():
        modname, app_name = import_path.split(':')
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue  # optional-extra group not installed in this env
        root = typer_get_command(getattr(mod, app_name))
        leaves.extend(_iter_commands(root))
    return leaves


def _option_params(cmd: click.Command):
    for p in cmd.params:
        if isinstance(p, click.Option):
            yield p


def test_long_option_has_consistent_short_flag():
    """The same --long-flag must map to the same short flag everywhere (or none)."""
    long_to_shorts: dict[str, set[str | None]] = {}
    for path, cmd in _all_leaf_commands():
        for opt in _option_params(cmd):
            longs = [o for o in opt.opts if o.startswith('--')]
            shorts = [o for o in opt.opts if o.startswith('-') and not o.startswith('--')]
            short = shorts[0] if shorts else None
            for long in longs:
                long_to_shorts.setdefault(long, set()).add(short)

    drifted = {long: shorts for long, shorts in long_to_shorts.items() if len(shorts) > 1}
    assert not drifted, f'long options shorthanded inconsistently across commands: {drifted}'


def test_short_flags_unique_within_command():
    for path, cmd in _all_leaf_commands():
        seen: dict[str, str] = {}
        for opt in _option_params(cmd):
            for o in opt.opts:
                if o.startswith('-') and not o.startswith('--'):
                    assert o not in seen, (
                        f'{" ".join(path)}: short flag {o} reused by {seen.get(o)} and {opt.name}'
                    )
                    seen[o] = opt.name


def test_shared_flags_use_canonical_help():
    for path, cmd in _all_leaf_commands():
        leaf = path[-1] if path else ''
        for opt in _option_params(cmd):
            for o in opt.opts:
                if o in _CANONICAL_HELP and o not in _HELP_EXEMPT:
                    if (leaf, o) in _CANONICAL_HELP_EXEMPT:
                        continue
                    assert opt.help == _CANONICAL_HELP[o], (
                        f'{" ".join(path)}: {o} help is {opt.help!r}, '
                        f'expected {_CANONICAL_HELP[o]!r}'
                    )


def test_help_strings_end_with_period():
    offenders = []
    for path, cmd in _all_leaf_commands():
        for p in cmd.params:
            help_text = getattr(p, 'help', None)
            if help_text and not help_text.rstrip().endswith(('.', ')', ':')):
                offenders.append(f'{" ".join(path)}:{p.name} -> {help_text!r}')
    assert not offenders, 'help strings missing trailing period:\n' + '\n'.join(offenders)


def test_renamed_command_groups():
    """The V8 group renames are reflected in the lazy registry."""
    top_level = set(LAZY_SUBCOMMANDS)
    assert 'system' not in top_level
    assert 'stats' in top_level


@pytest.mark.parametrize('removed', ['--token-budget', '--top-n', '--yes'])
def test_renamed_options_are_gone(removed):
    """The V8 option renames must not leave the old long flags anywhere."""
    for _path, cmd in _all_leaf_commands():
        for opt in _option_params(cmd):
            assert removed not in opt.opts
