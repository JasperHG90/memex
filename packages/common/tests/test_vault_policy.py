"""Unit tests for vault kind + synthesis policy helpers."""

import pytest
from pydantic import ValidationError

from memex_common.vault_policy import (
    VaultKind,
    VaultPolicy,
    UnknownVaultKind,
    coerce_policy,
    is_content,
    is_system,
    lint_llm_content_enabled,
    reflect_enabled,
    summarize_enabled,
)


class TestKindDefaults:
    def test_content_defaults_both_on(self):
        assert reflect_enabled(VaultKind.CONTENT) is True
        assert summarize_enabled(VaultKind.CONTENT) is True
        assert lint_llm_content_enabled(VaultKind.CONTENT) is True
        assert is_system(VaultKind.CONTENT) is False

    def test_system_defaults_both_off(self):
        assert reflect_enabled(VaultKind.SYSTEM) is False
        assert summarize_enabled(VaultKind.SYSTEM) is False
        assert lint_llm_content_enabled(VaultKind.SYSTEM) is False
        assert is_system(VaultKind.SYSTEM) is True

    def test_string_kind_accepted(self):
        assert reflect_enabled('content') is True
        assert reflect_enabled('system') is False
        assert lint_llm_content_enabled('system') is False
        assert is_system('system') is True


class TestPolicyOverride:
    def test_override_reflect_on_system(self):
        assert reflect_enabled('system', {'reflect': True}) is True
        # summarize still defaults off
        assert summarize_enabled('system', {'reflect': True}) is False

    def test_override_summarize_off_on_content(self):
        assert summarize_enabled('content', {'summarize': False}) is False
        assert reflect_enabled('content', {'summarize': False}) is True

    def test_override_lint_llm_content(self):
        assert lint_llm_content_enabled('system', {'lint_llm_content': True}) is True
        assert lint_llm_content_enabled('content', {'lint_llm_content': False}) is False

    def test_missing_key_falls_back_to_kind(self):
        assert reflect_enabled('content', {}) is True
        assert reflect_enabled('system', {}) is False
        assert lint_llm_content_enabled('content', {}) is True
        assert lint_llm_content_enabled('system', {}) is False

    def test_vault_policy_instance_accepted(self):
        assert reflect_enabled('system', VaultPolicy(reflect=True)) is True
        assert lint_llm_content_enabled('system', VaultPolicy(lint_llm_content=True)) is True


class TestPolicyValidation:
    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            VaultPolicy.model_validate({'reflct': True})

    def test_bool_string_coerced_not_truthy(self):
        # The typed model routes through pydantic; 'false' maps to False,
        # NOT bool('false')==True (the raw footgun this guards against).
        assert VaultPolicy.model_validate({'reflect': 'false'}).reflect is False
        assert VaultPolicy.model_validate({'lint_llm_content': 'false'}).lint_llm_content is False

    def test_non_bool_rejected(self):
        with pytest.raises(ValidationError):
            VaultPolicy.model_validate({'reflect': 'maybe'})
        with pytest.raises(ValidationError):
            VaultPolicy.model_validate({'lint_llm_content': 'maybe'})

    def test_coerce_none(self):
        assert coerce_policy(None) == VaultPolicy()

    def test_coerce_bad_type(self):
        with pytest.raises(TypeError):
            coerce_policy(42)  # type: ignore[arg-type]


class TestUnknownKindFailOpen:
    """Round-3 review (M): an unrecognised kind string must NOT silently
    suppress synthesis. We treat it as content (fail-open) so a corrupt
    row stays visible on browse surfaces, and emit a warning so the
    issue surfaces in logs. The DB CHECK + the CreateVaultRequest
    validator are the load-bearing guards in normal operation."""

    def test_is_system_treats_unknown_as_not_system(self):
        with pytest.warns(UnknownVaultKind):
            assert is_system('syste') is False
        with pytest.warns(UnknownVaultKind):
            assert is_system('archive') is False
        with pytest.warns(UnknownVaultKind):
            assert is_system('') is False

    def test_is_content_treats_unknown_as_content(self):
        with pytest.warns(UnknownVaultKind):
            assert is_content('syste') is True
        with pytest.warns(UnknownVaultKind):
            assert is_content('archive') is True
        with pytest.warns(UnknownVaultKind):
            assert is_content('') is True

    def test_reflect_enabled_unknown_kind_defaults_on(self):
        # Fail-open means synthesis stays on for unknown kinds; a typo
        # in the DB must NOT silently mute reflection for that vault.
        with pytest.warns(UnknownVaultKind):
            assert reflect_enabled('syste') is True

    def test_summarize_enabled_unknown_kind_defaults_on(self):
        with pytest.warns(UnknownVaultKind):
            assert summarize_enabled('syste') is True

    def test_lint_llm_content_enabled_unknown_kind_defaults_on(self):
        with pytest.warns(UnknownVaultKind):
            assert lint_llm_content_enabled('syste') is True

    def test_known_kinds_do_not_warn(self):
        # Sanity: the warning is reserved for the unknown branch.
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter('error', UnknownVaultKind)
            assert is_system('content') is False
            assert is_system('system') is True
            assert is_content('content') is True
            assert is_content('system') is False
