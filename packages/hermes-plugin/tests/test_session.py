"""Tests for session note key generation."""

from __future__ import annotations

from datetime import datetime, timezone

from memex_hermes_plugin.memex.session import (
    SESSION_KEY_PREFIX,
    is_session_note_key,
    make_session_note_key,
)


def test_key_is_prefixed():
    key = make_session_note_key()
    assert key.startswith(SESSION_KEY_PREFIX)


def test_key_is_deterministic_for_fixed_time():
    t = datetime(2026, 4, 21, 12, 0, 0, 123000, tzinfo=timezone.utc)
    key = make_session_note_key(t)
    assert key == 'hermes:session:2026-04-21T12:00:00.123Z'


def test_is_session_note_key():
    key = make_session_note_key()
    assert is_session_note_key(key)
    assert not is_session_note_key('some-other-key')
    assert not is_session_note_key('hermes:user:foo')


def test_keys_in_same_second_differ_by_ms():
    t1 = datetime(2026, 4, 21, 12, 0, 0, 100000, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 21, 12, 0, 0, 200000, tzinfo=timezone.utc)
    assert make_session_note_key(t1) != make_session_note_key(t2)
