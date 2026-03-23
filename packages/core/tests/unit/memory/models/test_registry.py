from memex_core.memory.models.base import MODEL_REGISTRY, ModelSpec


def test_registry_has_all_models() -> None:
    assert set(MODEL_REGISTRY.keys()) == {'embedding', 'reranker', 'ner'}


def test_registry_entries_are_model_specs() -> None:
    for key, spec in MODEL_REGISTRY.items():
        assert isinstance(spec, ModelSpec), f'{key} is not a ModelSpec'
        assert spec.repo_id, f'{key} has empty repo_id'
        assert spec.revision, f'{key} has empty revision'


def test_reranker_uses_v2() -> None:
    assert MODEL_REGISTRY['reranker'].revision == 'v2'
