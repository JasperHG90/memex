"""Secret redaction walker for serialized config snapshots.

Used by the server's ``GET /api/v1/system/config`` endpoint and the eval
recorder when logging configuration artifacts to MLflow. Defense-in-depth:
Pydantic v2 already serializes ``SecretStr`` to ``'**********'`` in
``model_dump(mode='json')``, but the walker also catches plain ``str``
fields whose names match a secret-bearing pattern, and adds a sibling
``<key>_set`` boolean so reviewers can tell whether a secret was
configured without leaking the value.
"""

from __future__ import annotations

from typing import Any

REDACTED = '<redacted>'

# Case-insensitive substring patterns. A leaf key matching any pattern is
# redacted regardless of value type (str, SecretStr's `'**********'`,
# bytes, anything truthy/falsy).
_SECRET_PATTERNS: tuple[str, ...] = (
    'password',
    'api_key',
    'access_key_id',
    'secret_access_key',
    'token',
    'webhook_secret',
    'dsn',
    'database_url',
)

# Patterns that are too generic for substring matching but appear as
# secret-bearing leaf names in known shapes (e.g. ``ApiKeyConfig.key``
# inside ``AuthConfig.keys: list[ApiKeyConfig]``). Handled via a shape
# check on the parent dict — if the parent has these sibling keys, the
# named field is redacted.
_SHAPE_REDACTIONS: tuple[tuple[frozenset[str], str], ...] = (
    # ApiKeyConfig: dict with both ``key`` and ``policy`` siblings
    (frozenset({'key', 'policy'}), 'key'),
)


def _is_secret_key(name: str) -> bool:
    """Return True if a leaf key name matches a substring pattern."""
    lowered = name.lower()
    return any(p in lowered for p in _SECRET_PATTERNS)


def _shape_secret_keys(d: dict[str, Any]) -> set[str]:
    """Return the set of keys in ``d`` that are secrets per shape rules."""
    keys = set(d.keys())
    out: set[str] = set()
    for required, secret_field in _SHAPE_REDACTIONS:
        if required.issubset(keys):
            out.add(secret_field)
    return out


def _is_set(value: Any) -> bool:
    """Was the original field non-empty (i.e. configured)?

    ``None`` and empty string/list/dict are treated as unset.  The placeholder
    ``'**********'`` (Pydantic's default ``SecretStr`` JSON serialization) is
    treated as set, since the field WAS configured before serialization.
    """
    if value is None:
        return False
    if isinstance(value, (str, list, dict, tuple, bytes)):
        return len(value) > 0
    return True


def redact(payload: Any) -> Any:
    """Walk a JSON-serializable structure and redact secret-bearing leaves.

    For each redacted leaf at key ``k``:
    - the value is replaced with ``REDACTED`` (``'<redacted>'``)
    - a sibling ``f'{k}_set'`` boolean is added (``True`` iff the original
      value was non-empty)

    Recurses into nested ``dict`` and ``list``.  Other types pass through
    unchanged.  Idempotent: re-running on already-redacted output is a
    no-op (the ``<redacted>`` literal is treated as set, the ``_set``
    sibling is preserved).
    """
    if isinstance(payload, dict):
        return _redact_dict(payload)
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


def _redact_dict(d: dict[str, Any]) -> dict[str, Any]:
    secret_keys_by_shape = _shape_secret_keys(d)
    out: dict[str, Any] = {}
    for k, v in d.items():
        if k.endswith('_set') and isinstance(v, bool) and k[:-4] in d:
            # Already-emitted sibling marker from a prior redaction pass.
            # Keep it; it'll be regenerated below if the partner key is a
            # secret, which would just overwrite with the same value.
            out[k] = v
            continue

        is_secret = _is_secret_key(k) or k in secret_keys_by_shape
        if is_secret:
            out[k] = REDACTED
            out[f'{k}_set'] = _is_set(v)
        elif isinstance(v, dict):
            out[k] = _redact_dict(v)
        elif isinstance(v, list):
            out[k] = [redact(item) for item in v]
        else:
            out[k] = v
    return out


__all__ = ['redact', 'REDACTED']
