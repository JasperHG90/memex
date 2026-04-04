"""Unit test for DocumentConfig.mmr_lambda default value."""

from memex_common.config import DocumentConfig


class TestDocumentConfigMmrDefault:
    def test_mmr_lambda_defaults_to_0_8(self):
        """MMR lambda should default to 0.8 for diversity filtering."""
        config = DocumentConfig()
        assert config.mmr_lambda == 0.8

    def test_mmr_lambda_can_be_overridden(self):
        """Per-request mmr_lambda should override the default."""
        config = DocumentConfig(mmr_lambda=0.5)
        assert config.mmr_lambda == 0.5

    def test_mmr_lambda_can_be_disabled(self):
        """Setting mmr_lambda to None should disable MMR."""
        config = DocumentConfig(mmr_lambda=None)
        assert config.mmr_lambda is None
