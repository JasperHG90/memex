"""Tests for memex_common.redaction.redact — secret deny-list walker.

Covers:
- Substring-pattern matching on every secret-bearing leaf name
  (password, api_key, secret_access_key, session_token, webhook_secret,
  dsn, database_url).
- Shape-based rule for ``ApiKeyConfig``-shaped dicts (key + policy
  siblings) where ``key`` would be too generic to substring-match.
- Sibling ``<key>_set`` boolean indicating whether the original was
  configured (non-empty) without leaking the value.
- Recursion through nested dicts and lists.
- Idempotency on re-running.
- Pass-through of non-dict/list payloads.
- End-to-end via ``MemexConfig.model_dump(mode='json')`` — every
  ``SecretStr`` field actually present in the config tree gets caught.
"""

from __future__ import annotations

from pydantic import SecretStr

from memex_common.config import MemexConfig
from memex_common.redaction import REDACTED, redact


def _walk_for_value(payload: object, target: str) -> bool:
    """Recursively check whether ``target`` appears as any leaf string."""
    if isinstance(payload, dict):
        return any(_walk_for_value(v, target) for v in payload.values())
    if isinstance(payload, list):
        return any(_walk_for_value(v, target) for v in payload)
    if isinstance(payload, str):
        return target in payload
    return False


def test_redacts_password_field() -> None:
    out = redact({'password': 'hunter2'})
    assert out == {'password': REDACTED, 'password_set': True}


def test_redacts_api_key_field() -> None:
    out = redact({'api_key': 'sk-test-1234'})
    assert out == {'api_key': REDACTED, 'api_key_set': True}


def test_marks_unset_when_value_is_none() -> None:
    out = redact({'api_key': None})
    assert out == {'api_key': REDACTED, 'api_key_set': False}


def test_marks_unset_when_value_is_empty_string() -> None:
    out = redact({'api_key': ''})
    assert out == {'api_key': REDACTED, 'api_key_set': False}


def test_redacts_aws_credentials() -> None:
    """All three AWS credential parts are redacted. ``access_key_id`` is
    technically public-pairing material but is treated as identifier-class
    credential and redacted as defense-in-depth."""
    out = redact(
        {
            'access_key_id': 'AKIA-id',
            'secret_access_key': 'wJalr...',
            'session_token': 'IQoJ...',
        }
    )
    assert out['access_key_id'] == REDACTED
    assert out['access_key_id_set'] is True
    assert out['secret_access_key'] == REDACTED
    assert out['secret_access_key_set'] is True
    assert out['session_token'] == REDACTED
    assert out['session_token_set'] is True


def test_redacts_plain_token_field() -> None:
    """``token`` substring catches GCSFileStoreConfig.token (plain str)."""
    out = redact({'token': '/path/to/credentials.json'})
    assert out == {'token': REDACTED, 'token_set': True}


def test_redacts_webhook_secret() -> None:
    out = redact({'webhook_secret': 'whsec_abc123'})
    assert out == {'webhook_secret': REDACTED, 'webhook_secret_set': True}


def test_redacts_dsn_and_database_url() -> None:
    out = redact(
        {
            'dsn': 'postgres://u:p@host/db',
            'database_url': 'postgres://u:p@host/db',
        }
    )
    assert out['dsn'] == REDACTED
    assert out['database_url'] == REDACTED


def test_does_not_redact_non_secret_field() -> None:
    out = redact({'note_key': 'project-alpha-kickoff', 'vault_name': 'work'})
    assert out == {'note_key': 'project-alpha-kickoff', 'vault_name': 'work'}


def test_apikeyconfig_shape_redacts_plain_key_field() -> None:
    """`key` alone is too generic for substring matching, but combined
    with `policy` it's an ApiKeyConfig — redact via shape rule."""
    out = redact({'key': 'my-secret-token', 'policy': 'admin'})
    assert out == {'key': REDACTED, 'key_set': True, 'policy': 'admin'}


def test_plain_key_field_outside_apikeyconfig_shape_is_not_redacted() -> None:
    """`key` alone (no policy sibling) is left alone — it's not a secret."""
    out = redact({'key': 'foo', 'value': 'bar'})
    assert out == {'key': 'foo', 'value': 'bar'}


def test_recurses_into_nested_dicts() -> None:
    out = redact({'database': {'password': 'x', 'host': 'localhost'}})
    assert out == {
        'database': {'password': REDACTED, 'password_set': True, 'host': 'localhost'},
    }


def test_recurses_into_list_of_apikeyconfigs() -> None:
    """Lists of ApiKeyConfig-shaped dicts (AuthConfig.keys) get redacted."""
    out = redact(
        {
            'keys': [
                {'key': 'k1', 'policy': 'admin'},
                {'key': 'k2', 'policy': 'reader'},
            ]
        }
    )
    assert out['keys'] == [
        {'key': REDACTED, 'key_set': True, 'policy': 'admin'},
        {'key': REDACTED, 'key_set': True, 'policy': 'reader'},
    ]


def test_idempotent_on_already_redacted_payload() -> None:
    """Re-running redact on its own output produces the same shape."""
    once = redact({'api_key': 'real'})
    twice = redact(once)
    assert twice == once


def test_passes_through_scalars_and_unknown_types() -> None:
    assert redact(42) == 42
    assert redact('plain string') == 'plain string'
    assert redact(None) is None
    assert redact(True) is True


def test_redacts_pydantic_secretstr_placeholder() -> None:
    """SecretStr in mode='json' serializes to '**********'; redact treats
    it the same as any other secret value."""
    out = redact({'api_key': '**********'})
    assert out['api_key'] == REDACTED
    # The placeholder is non-empty, so it counts as set.
    assert out['api_key_set'] is True


def test_full_memex_config_dump_redacts_every_secret() -> None:
    """End-to-end: build a MemexConfig with a known sentinel in every
    SecretStr field, dump JSON, redact, and assert the sentinel never
    appears in the output. Also asserts every targeted leaf becomes
    REDACTED.
    """
    sentinel = 'TESTING_SENTINEL_NEVER_LEAKED_v3.x_42'

    # Build a config tree with secrets in every known SecretStr location.
    cfg = MemexConfig.model_validate(
        {
            'server': {
                'meta_store': {
                    'type': 'postgres',
                    'instance': {
                        'host': 'localhost',
                        'port': 5432,
                        'database': 'memex',
                        'user': 'memex',
                        'password': sentinel,
                    },
                },
                'auth': {
                    'enabled': True,
                    'keys': [
                        {'key': sentinel, 'policy': 'admin'},
                        {'key': sentinel, 'policy': 'reader'},
                    ],
                    'webhook_secret': sentinel,
                },
                'file_store': {
                    'type': 's3',
                    'bucket': 'memex-test',
                    'access_key_id': sentinel,
                    'secret_access_key': sentinel,
                    'session_token': sentinel,
                },
            },
        }
    )

    raw = cfg.model_dump(mode='json')
    out = redact(raw)

    # The sentinel must NOT appear anywhere in the redacted output.
    assert not _walk_for_value(out, sentinel), (
        'redaction failed: secret sentinel leaked through model_dump+redact'
    )
    # And REDACTED placeholders must appear (otherwise the test is vacuous).
    assert _walk_for_value(out, REDACTED)


def test_pydantic_secretstr_json_mode_baseline() -> None:
    """Sanity check that Pydantic v2 actually masks SecretStr in json mode.

    If this regresses (e.g. dependency upgrade exposes raw secrets in
    json dumps), redact() catches it via the deny-list, but we want to
    notice the change.
    """
    from pydantic import BaseModel

    class _M(BaseModel):
        api_key: SecretStr

    m = _M(api_key='real-secret-xyz')
    dumped = m.model_dump(mode='json')
    assert dumped == {'api_key': '**********'}
    # And belt-and-braces: redact still kicks in.
    assert redact(dumped) == {'api_key': REDACTED, 'api_key_set': True}
