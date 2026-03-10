import warnings

from .__about__ import __version__ as __version__

# Suppress Pydantic serializer warnings
warnings.filterwarnings('ignore', message='Pydantic serializer warnings')

__all__ = ['MemexAPI', 'NoteInput', 'MemexConfig']
