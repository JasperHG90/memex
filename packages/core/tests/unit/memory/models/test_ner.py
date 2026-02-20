from unittest.mock import MagicMock, patch
import pytest
import numpy as np
from memex_core.memory.models.ner import sanitize_entity, merge_entities, FastNERModel


class TestNERUtils:
    def test_sanitize_entity(self):
        assert sanitize_entity('  Hello,  ') == 'Hello'
        assert sanitize_entity('.World.') == 'World'
        assert (
            sanitize_entity('##Apple') == 'Apple'
        )  # sanitize removes leading punctuation including #
        assert sanitize_entity('test-case') == 'test-case'

    def test_merge_entities_basic(self):
        entities = [
            {'word': 'App', 'type': 'ORG', 'start': 0, 'end': 3, 'score': 0.9},
            {'word': '##le', 'type': 'ORG', 'start': 3, 'end': 5, 'score': 0.95},
        ]
        merged = merge_entities(entities)
        assert len(merged) == 1
        assert merged[0]['word'] == 'Apple'
        assert merged[0]['type'] == 'ORG'
        assert merged[0]['start'] == 0
        assert merged[0]['end'] == 5

    def test_merge_entities_adjacent_words(self):
        entities = [
            {'word': 'San', 'type': 'LOC', 'start': 0, 'end': 3, 'score': 0.9},
            {'word': 'Francisco', 'type': 'LOC', 'start': 4, 'end': 13, 'score': 0.9},
        ]
        merged = merge_entities(entities)
        assert len(merged) == 1
        assert merged[0]['word'] == 'San Francisco'
        # Gap was 1 (end 3 -> start 4), so space inserted

    def test_merge_entities_different_types(self):
        entities = [
            {'word': 'Apple', 'type': 'ORG', 'start': 0, 'end': 5, 'score': 0.9},
            {'word': 'California', 'type': 'LOC', 'start': 6, 'end': 16, 'score': 0.9},
        ]
        merged = merge_entities(entities)
        # Current implementation merges adjacent entities regardless of type
        assert len(merged) == 1
        assert merged[0]['word'] == 'Apple California'

    def test_merge_entities_large_gap(self):
        entities = [
            {'word': 'Apple', 'type': 'ORG', 'start': 0, 'end': 5, 'score': 0.9},
            {'word': 'Inc', 'type': 'ORG', 'start': 10, 'end': 13, 'score': 0.9},
        ]
        merged = merge_entities(entities)
        assert (
            len(merged) == 2
        )  # Gap > 1, should not merge (unless logic changed to ignore gap for same type?)
        # Current logic: if is_adjacent or is_subword. is_adjacent = gap <= 1.
        # 10 - 5 = 5. Gap > 1. Not adjacent.

    def test_merge_entities_subword_start(self):
        # Case where first entity starts with ## (should be cleaned)
        entities = [
            {'word': '##The', 'type': 'MISC', 'start': 0, 'end': 3, 'score': 0.5},
        ]
        merged = merge_entities(entities)
        assert len(merged) == 1
        assert merged[0]['word'] == 'The'


class TestFastNERModel:
    @pytest.fixture
    def mock_deps(self):
        with (
            patch('memex_core.memory.models.ner.Tokenizer') as mock_tokenizer_cls,
            patch('memex_core.memory.models.ner.ort.InferenceSession') as mock_session_cls,
            patch('pathlib.Path.read_text') as mock_read_text,
            patch('pathlib.Path.exists') as mock_exists,
        ):
            mock_exists.return_value = True
            mock_read_text.return_value = (
                '{"0": "O", "1": "B-PER", "2": "I-PER", "3": "B-ORG", "4": "I-ORG"}'
            )

            mock_tokenizer = MagicMock()
            mock_tokenizer_cls.from_file.return_value = mock_tokenizer

            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session

            yield mock_tokenizer, mock_session

    def test_initialization(self, mock_deps):
        mock_tokenizer, mock_session = mock_deps
        model = FastNERModel(model_dir='/fake/path')
        assert model.tokenizer is not None
        assert model.session is not None
        assert model.id2label == {0: 'O', 1: 'B-PER', 2: 'I-PER', 3: 'B-ORG', 4: 'I-ORG'}

    def test_predict(self, mock_deps):
        mock_tokenizer, mock_session = mock_deps
        model = FastNERModel(model_dir='/fake/path')

        text = 'Alice works at Acme'

        # Mock Tokenizer Output
        mock_encoding = MagicMock()
        mock_encoding.ids = [101, 1001, 2001, 3001, 4001, 102]  # CLS, Alice, works, at, Acme, SEP
        mock_encoding.attention_mask = [1, 1, 1, 1, 1, 1]
        # word_ids maps tokens to word indices: None (CLS), 0 (Alice), 1 (works), 2 (at), 3 (Acme), None (SEP)
        mock_encoding.word_ids = [None, 0, 1, 2, 3, None]
        mock_encoding.offsets = [(0, 0), (0, 5), (6, 11), (12, 14), (15, 19), (0, 0)]

        mock_tokenizer.encode.return_value = mock_encoding

        # Mock ONNX Session Output
        # Shape: [batch_size=1, seq_len=6, num_labels=5]
        # We need logits that produce the correct argmax
        # 0:O, 1:B-PER, 2:I-PER, 3:B-ORG, 4:I-ORG
        # Tokens: CLS(O), Alice(B-PER), works(O), at(O), Acme(B-ORG), SEP(O)
        # Indices: 0, 1, 0, 0, 3, 0

        logits = np.zeros((1, 6, 5), dtype=np.float32)
        logits[0, 1, 1] = 10.0  # Alice -> B-PER
        logits[0, 4, 3] = 10.0  # Acme -> B-ORG

        mock_session.run.return_value = [logits]

        entities = model.predict(text)

        assert len(entities) == 2

        alice = entities[0]
        assert alice['word'] == 'Alice'
        assert alice['type'] == 'PER'

        acme = entities[1]
        assert acme['word'] == 'Acme'
        assert acme['type'] == 'ORG'

    def test_predict_merging(self, mock_deps):
        mock_tokenizer, mock_session = mock_deps
        model = FastNERModel(model_dir='/fake/path')

        # text = 'New York'

        # Mock Tokenizer
        mock_encoding = MagicMock()
        # CLS, New, York, SEP
        mock_encoding.ids = [101, 2001, 2002, 102]
        mock_encoding.word_ids = [None, 0, 1, None]
        mock_encoding.offsets = [(0, 0), (0, 3), (4, 8), (0, 0)]
        mock_tokenizer.encode.return_value = mock_encoding

        # Mock ONNX: New(B-LOC), York(I-LOC)
        # Assuming label map has LOC. Let's update label map via id2label injection for this test or rely on generic split
        # The code does `label.split('-')[-1]`.
        # Let's say id 1 is B-LOC, 2 is I-LOC (reusing PER indices from fixture but pretending they are LOC for simplicity or updating fixture?)
        # Actually, let's just stick to what the fixture provides: PER/ORG.
        # Let's use "John Doe" (PER)

        logits = np.zeros((1, 4, 5), dtype=np.float32)
        logits[0, 1, 1] = 10.0  # New -> B-PER (Simulated)
        logits[0, 2, 2] = 10.0  # York -> I-PER (Simulated)

        mock_session.run.return_value = [logits]

        entities = model.predict('New York')

        assert len(entities) == 1
        assert entities[0]['word'] == 'New York'
        assert entities[0]['type'] == 'PER'
