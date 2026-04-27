"""Unit tests for the small pure helpers added by the atomic note-append feature."""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from memex_common.schemas import (
    NoteAppendRequest,
    append_joiner_separator,
)
from memex_core.services.notes import derive_note_uuid_from_key


class TestDeriveNoteUuidFromKey:
    """`derive_note_uuid_from_key` mirrors NoteInput.note_key (api.py)."""

    def test_uuid_string_round_trips(self):
        u = UUID('12345678-1234-1234-1234-123456789012')
        assert derive_note_uuid_from_key(str(u)) == u

    def test_arbitrary_string_hashes_to_md5_uuid(self):
        key = 'session-2026-04-26-jasper'
        expected = UUID(hashlib.md5(key.encode()).hexdigest())
        assert derive_note_uuid_from_key(key) == expected

    def test_same_key_resolves_to_same_uuid(self):
        a = derive_note_uuid_from_key('shared-key')
        b = derive_note_uuid_from_key('shared-key')
        assert a == b

    def test_different_keys_resolve_to_different_uuids(self):
        a = derive_note_uuid_from_key('one')
        b = derive_note_uuid_from_key('two')
        assert a != b


class TestAppendJoinerSeparator:
    @pytest.mark.parametrize(
        'name,expected',
        [
            ('paragraph', '\n\n'),
            ('newline', '\n'),
            ('none', ''),
        ],
    )
    def test_known_joiners(self, name, expected):
        assert append_joiner_separator(name) == expected

    def test_unknown_joiner_raises(self):
        with pytest.raises(ValueError, match='Unknown joiner'):
            append_joiner_separator('crlf')


class TestNoteAppendRequestValidation:
    """Pydantic-level validation rules — caught BEFORE the service call."""

    def _make(self, **overrides):
        defaults = dict(
            delta='something',
            append_id='12345678-1234-1234-1234-123456789012',
        )
        defaults.update(overrides)
        return NoteAppendRequest(**defaults)

    def test_requires_some_identifier(self):
        with pytest.raises(ValueError, match='note_id or note_key'):
            self._make()

    def test_note_key_requires_vault(self):
        with pytest.raises(ValueError, match='vault_id is required'):
            self._make(note_key='abc')

    def test_note_id_alone_is_fine(self):
        req = self._make(note_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
        assert req.note_id is not None

    def test_note_key_with_vault_is_fine(self):
        req = self._make(note_key='abc', vault_id='global')
        assert req.note_key == 'abc'

    def test_rejects_unknown_joiner(self):
        with pytest.raises(ValueError, match='Unknown joiner'):
            self._make(note_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', joiner='crlf')

    def test_rejects_frontmatter_delta(self):
        with pytest.raises(ValueError, match='frontmatter'):
            self._make(
                note_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                delta='---\nkey: val\n---\nbody',
            )

    def test_oversized_delta_rejected(self):
        with pytest.raises(ValueError):
            self._make(
                note_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                delta='x' * 200_001,
            )

    def test_empty_delta_rejected(self):
        with pytest.raises(ValueError):
            self._make(
                note_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                delta='',
            )

    def test_whitespace_only_delta_rejected(self):
        with pytest.raises(ValueError, match='non-whitespace'):
            self._make(
                note_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                delta='   \n\t  ',
            )

    def test_nul_byte_in_delta_rejected(self):
        with pytest.raises(ValueError, match='NUL'):
            self._make(
                note_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                delta='hello\x00world',
            )

    def test_frontmatter_with_trailing_whitespace_rejected(self):
        # `---  \n` (trailing spaces before newline) would still be parsed as
        # the start of YAML frontmatter; reject it the same as `---\n`.
        with pytest.raises(ValueError, match='frontmatter'):
            self._make(
                note_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                delta='---  \nkey: val\n---\nbody',
            )

    def test_frontmatter_with_crlf_rejected(self):
        with pytest.raises(ValueError, match='frontmatter'):
            self._make(
                note_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                delta='---\r\nkey: val\r\n---\r\nbody',
            )

    def test_emoji_heavy_delta_byte_cap_enforced(self):
        # 4-byte chars × 50_001 = 200_004 bytes — over the cap, even though
        # the character count (50_001) is far under Pydantic's old 200_000.
        wide = '\U0001f300' * 50_001
        with pytest.raises(ValueError, match='UTF-8 bytes'):
            self._make(
                note_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                delta=wide,
            )
