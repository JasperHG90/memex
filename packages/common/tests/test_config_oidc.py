"""Tests for OIDC configuration models (server providers + client login)."""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from memex_common.config import (
    AuthConfig,
    MemexConfig,
    OidcClientConfig,
    OidcGrantRule,
    OidcProviderConfig,
    Policy,
)


class TestOidcGrantRule:
    def test_minimal_rule(self):
        rule = OidcGrantRule(claim='groups', value='memex-admins', policy='admin')
        assert rule.policy is Policy.ADMIN
        assert rule.vault_ids is None
        assert rule.read_vault_ids is None

    def test_vault_scoping(self):
        rule = OidcGrantRule(
            claim='groups',
            value='team-a',
            policy='writer',
            vault_ids=['vault-a'],
            read_vault_ids=['vault-b'],
        )
        assert rule.vault_ids == ['vault-a']
        assert rule.read_vault_ids == ['vault-b']

    def test_read_vault_ids_requires_vault_ids(self):
        with pytest.raises(ValidationError, match='read_vault_ids cannot be set'):
            OidcGrantRule(
                claim='groups', value='team-a', policy='reader', read_vault_ids=['vault-b']
            )


class TestOidcProviderConfig:
    def test_defaults(self):
        provider = OidcProviderConfig(
            issuer='https://issuer.example',
            audience=['api://memex'],
            grant_rules=[OidcGrantRule(claim='groups', value='admins', policy='admin')],
        )
        assert provider.algorithms == ['RS256', 'ES256']
        assert provider.leeway_seconds == 60
        assert provider.jwks_uri is None
        assert provider.default_policy is None

    def test_default_policy_alone_is_valid(self):
        provider = OidcProviderConfig(
            issuer='https://issuer.example',
            audience=['api://memex'],
            default_policy='reader',
        )
        assert provider.default_policy is Policy.READER

    def test_rejects_no_grant_rules_and_no_default_policy(self):
        with pytest.raises(ValidationError, match='at least one grant_rule or a'):
            OidcProviderConfig(issuer='https://issuer.example', audience=['api://memex'])

    def test_negative_leeway_rejected(self):
        with pytest.raises(ValidationError):
            OidcProviderConfig(
                issuer='https://issuer.example',
                audience=['api://memex'],
                default_policy='reader',
                leeway_seconds=-1,
            )

    def test_empty_audience_rejected(self):
        with pytest.raises(ValidationError):
            OidcProviderConfig(
                issuer='https://issuer.example',
                audience=[],
                default_policy='reader',
            )

    @pytest.mark.parametrize('bad_alg', ['HS256', 'none', 'HS512'])
    def test_symmetric_algorithms_rejected(self, bad_alg):
        with pytest.raises(ValidationError, match='asymmetric'):
            OidcProviderConfig(
                issuer='https://issuer.example',
                audience=['api://memex'],
                default_policy='reader',
                algorithms=['RS256', bad_alg],
            )

    @pytest.mark.parametrize('good_alg', ['RS256', 'ES384', 'PS256', 'EdDSA'])
    def test_asymmetric_algorithms_allowed(self, good_alg):
        provider = OidcProviderConfig(
            issuer='https://issuer.example',
            audience=['api://memex'],
            default_policy='reader',
            algorithms=[good_alg],
        )
        assert provider.algorithms == [good_alg]

    @pytest.mark.parametrize(
        'bad_url', ['http://issuer.example', 'http://evil.example/jwks', 'ftp://issuer.example']
    )
    def test_non_https_issuer_rejected(self, bad_url):
        with pytest.raises(ValidationError, match='must use HTTPS'):
            OidcProviderConfig(
                issuer=bad_url,
                audience=['api://memex'],
                default_policy='reader',
            )

    def test_non_https_jwks_uri_rejected(self):
        with pytest.raises(ValidationError, match='must use HTTPS'):
            OidcProviderConfig(
                issuer='https://issuer.example',
                audience=['api://memex'],
                default_policy='reader',
                jwks_uri='http://evil.example/jwks',
            )

    @pytest.mark.parametrize(
        'loopback', ['http://localhost:8080', 'http://127.0.0.1:5556', 'http://[::1]:9000']
    )
    def test_http_loopback_issuer_allowed(self, loopback):
        provider = OidcProviderConfig(
            issuer=loopback,
            audience=['api://memex'],
            default_policy='reader',
        )
        assert provider.issuer == loopback


class TestAuthConfigOidc:
    def test_oidc_defaults_empty(self):
        auth = AuthConfig(enabled=True)
        assert auth.oidc == []

    def test_oidc_and_keys_coexist(self):
        auth = AuthConfig(
            enabled=True,
            keys=[{'key': 'secret-key', 'policy': 'admin'}],
            oidc=[
                {
                    'issuer': 'https://issuer.example',
                    'audience': ['api://memex'],
                    'default_policy': 'reader',
                }
            ],
        )
        assert len(auth.keys) == 1
        assert len(auth.oidc) == 1
        assert auth.oidc[0].issuer == 'https://issuer.example'


class TestOidcClientConfig:
    def test_defaults(self):
        client = OidcClientConfig(issuer='https://issuer.example', client_id='abc')
        assert client.scopes == ['openid', 'profile', 'email', 'offline_access']
        assert client.client_secret is None

    def test_client_secret_is_secret(self):
        client = OidcClientConfig(
            issuer='https://issuer.example', client_id='abc', client_secret='shh'
        )
        assert client.client_secret.get_secret_value() == 'shh'
        assert 'shh' not in repr(client)

    def test_default_grant_is_interactive(self):
        client = OidcClientConfig(issuer='https://issuer.example', client_id='abc')
        assert client.grant == 'interactive'

    def test_client_credentials_requires_secret(self):
        with pytest.raises(ValidationError, match='requires client_secret'):
            OidcClientConfig(
                issuer='https://issuer.example', client_id='abc', grant='client_credentials'
            )

    def test_client_credentials_valid_with_secret(self):
        client = OidcClientConfig(
            issuer='https://issuer.example',
            client_id='abc',
            grant='client_credentials',
            client_secret='shh',
        )
        assert client.grant == 'client_credentials'

    def test_jwt_profile_requires_key_file(self):
        with pytest.raises(ValidationError, match='requires key_file'):
            OidcClientConfig(issuer='https://issuer.example', client_id='abc', grant='jwt_profile')

    def test_jwt_profile_valid_with_key_file(self):
        client = OidcClientConfig(
            issuer='https://issuer.example',
            client_id='abc',
            grant='jwt_profile',
            key_file='/etc/memex/key.json',
        )
        assert client.key_file == '/etc/memex/key.json'

    @pytest.mark.parametrize('grant', ['interactive', 'client_credentials', 'jwt_profile'])
    def test_secretful_grants_require_client_id(self, grant):
        kwargs = {'client_secret': 'shh', 'key_file': '/k.json'}
        with pytest.raises(ValidationError, match='requires client_id'):
            OidcClientConfig(issuer='https://issuer.example', grant=grant, **kwargs)

    def test_token_file_grant(self):
        client = OidcClientConfig(
            issuer='https://issuer.example',
            grant='token_file',
            token_file='/secrets/nomad_token.jwt',
        )
        # client_id is optional for keyless grants.
        assert client.client_id is None
        assert client.token_file == '/secrets/nomad_token.jwt'

    def test_token_file_requires_token_file(self):
        with pytest.raises(ValidationError, match='requires token_file'):
            OidcClientConfig(issuer='https://issuer.example', grant='token_file')

    def test_token_env_grant(self):
        client = OidcClientConfig(
            issuer='https://issuer.example',
            grant='token_env',
            token_env='NOMAD_TOKEN',
        )
        assert client.token_env == 'NOMAD_TOKEN'

    def test_token_env_requires_token_env(self):
        with pytest.raises(ValidationError, match='requires token_env'):
            OidcClientConfig(issuer='https://issuer.example', grant='token_env')


class TestMemexConfigOidcField:
    """The client `oidc` field must be accepted under extra='forbid'."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self):
        with (
            patch('memex_common.config.GlobalYamlConfigSettingsSource.__call__', return_value={}),
            patch('memex_common.config.LocalYamlConfigSettingsSource.__call__', return_value={}),
            patch.dict(os.environ, {}, clear=False),
        ):
            yield

    def test_client_oidc_accepted(self):
        config = MemexConfig(oidc={'issuer': 'https://issuer.example', 'client_id': 'abc'})
        assert config.oidc is not None
        assert config.oidc.client_id == 'abc'

    def test_client_oidc_defaults_none(self):
        config = MemexConfig()
        assert config.oidc is None
