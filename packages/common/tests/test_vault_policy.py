"""Unit tests for vault kind + synthesis policy helpers."""

import pytest
from pydantic import ValidationError

from memex_common.vault_policy import (
    VaultKind,
    VaultPolicy,
    coerce_policy,
    is_system,
    reflect_enabled,
    summarize_enabled,
)


class TestKindDefaults:
    def test_content_defaults_both_on(self):
        assert reflect_enabled(VaultKind.CONTENT) is True
        assert summarize_enabled(VaultKind.CONTENT) is True
        assert is_system(VaultKind.CONTENT) is False

    def test_system_defaults_both_off(self):
        assert reflect_enabled(VaultKind.SYSTEM) is False
        assert summarize_enabled(VaultKind.SYSTEM) is False
        assert is_system(VaultKind.SYSTEM) is True

    def test_string_kind_accepted(self):
        assert reflect_enabled('content') is True
        assert reflect_enabled('system') is False
        assert is_system('system') is True


class TestPolicyOverride:
    def test_override_reflect_on_system(self):
        assert reflect_enabled('system', {'reflect': True}) is True
        # summarize still defaults off
        assert summarize_enabled('system', {'reflect': True}) is False

    def test_override_summarize_off_on_content(self):
        assert summarize_enabled('content', {'summarize': False}) is False
        assert reflect_enabled('content', {'summarize': False}) is True

    def test_missing_key_falls_back_to_kind(self):
        assert reflect_enabled('content', {}) is True
        assert reflect_enabled('system', {}) is False

    def test_vault_policy_instance_accepted(self):
        assert reflect_enabled('system', VaultPolicy(reflect=True)) is True


class TestPolicyValidation:
    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            VaultPolicy.model_validate({'reflct': True})

    def test_bool_string_coerced_not_truthy(self):
        # The typed model routes through pydantic; 'false' maps to False,
        # NOT bool('false')==True (the raw footgun this guards against).
        assert VaultPolicy.model_validate({'reflect': 'false'}).reflect is False

    def test_non_bool_rejected(self):
        with pytest.raises(ValidationError):
            VaultPolicy.model_validate({'reflect': 'maybe'})

    def test_coerce_none(self):
        assert coerce_policy(None) == VaultPolicy()

    def test_coerce_bad_type(self):
        with pytest.raises(TypeError):
            coerce_policy(42)  # type: ignore[arg-type]
