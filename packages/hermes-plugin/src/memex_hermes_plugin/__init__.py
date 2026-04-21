"""memex-hermes-plugin — Memex memory provider for Hermes Agent.

The actual plugin directory lives at ``memex_hermes_plugin/memex``. It is copied
or symlinked into ``$HERMES_HOME/plugins/memex/`` by ``memex hermes install``.
"""

from pathlib import Path

try:
    from .__about__ import __version__ as __version__
except ModuleNotFoundError:
    __version__ = '0.0.0.dev0'

# Filesystem path to the bundled Hermes plugin directory.
PLUGIN_DIR: Path = Path(__file__).parent / 'memex'
