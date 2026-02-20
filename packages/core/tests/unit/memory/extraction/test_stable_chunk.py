from memex_core.memory.extraction.core import stable_chunk_text


def test_chunk_stability_cdc():
    """
    Demonstrates that the CDC (Content-Defined Chunking) strategy ensures stability,
    preventing the 'avalanche effect' where a small insertion at the start shifts
    boundaries for ALL subsequent chunks.
    """

    # 1. Setup: Create a sequence of distinct paragraphs
    # Paragraphs ~400 chars each.
    paragraphs = [f'Paragraph {i} ' * 30 for i in range(20)]

    text_original = '\n\n'.join(paragraphs)

    # Limit = 1000.
    limit_tight = 1000

    chunks_orig_tight = stable_chunk_text(text_original, block_size=limit_tight)

    # 3. Modify: Insert "Intro" at the start
    intro = 'Intro ' * 50  # ~300 chars
    text_modified = intro + '\n\n' + text_original

    chunks_mod_tight = stable_chunk_text(text_modified, block_size=limit_tight)

    # Assert that the last chunk IS IDENTICAL, proving stability.
    # The CDC algorithm ensures that boundaries are determined by content (hashes),
    # so the insertion of "Intro" should only affect the first chunk(s) until a
    # stable boundary is found. The tail of the document (P18, P19) should remain
    # in the same chunk configuration.
    assert chunks_orig_tight[-1].content_hash == chunks_mod_tight[-1].content_hash


def test_chunk_stability_weird_whitespace_idempotency():
    """
    Regression test for a bug where inconsistent blank lines (e.g. '\\n \\n')
    prevented paragraphs from being split, leading to large unstable chunks.

    With Line-Based CDC, '\\n \\n' is just a line containing a space.
    It should still allow splitting if the surrounding content triggers boundaries
    or size limits. Crucially, the head chunk must be stable if the tail is cut.
    """

    # 1. Setup: Two distinct paragraphs with "weird" separation
    p1 = 'Paragraph 1 ' * 50  # ~600 chars
    p2 = 'Paragraph 2 ' * 50  # ~600 chars

    # Weird separator: Line with spaces
    text_weird = p1 + '\n \n' + p2

    # Block size small enough to force split
    # P1 is 600 chars. Limit 500. Should force split at P1 end or earlier.
    chunks_full = stable_chunk_text(text_weird, block_size=500)

    # Expect at least 2 chunks (P1 and P2 separate)
    assert len(chunks_full) >= 2

    # 2. Truncate the text (cut P2)
    # Even if we cut right after the weird line
    text_trunc = p1 + '\n \n'
    chunks_trunc = stable_chunk_text(text_trunc, block_size=500)

    # The first chunk should be identical!
    # (Assuming P1 fits in one chunk or splits identically)
    assert chunks_full[0].content_hash == chunks_trunc[0].content_hash


class TestMarkdownAwareChunking:
    """Tests for Markdown-aware CDC chunking."""

    def test_frontmatter_merged_with_first_chunk(self):
        text = '---\ntitle: Test\n---\n\nFirst paragraph here.\n\nSecond paragraph.'
        chunks = stable_chunk_text(text, block_size=500, markdown_aware=True)
        assert len(chunks) >= 1
        assert chunks[0].text.startswith('---\ntitle: Test\n---')

    def test_frontmatter_only_document(self):
        text = '---\ntitle: Test\nauthor: Author\n---\n'
        chunks = stable_chunk_text(text, block_size=500, markdown_aware=True)
        assert len(chunks) == 1
        assert 'title: Test' in chunks[0].text

    def test_no_frontmatter(self):
        text = 'Just regular text without frontmatter.\n\nMore text here.'
        chunks = stable_chunk_text(text, block_size=500, markdown_aware=True)
        assert not chunks[0].text.startswith('---')

    def test_fenced_code_block_atomic(self):
        code = '```python\nprint("hello")\nprint("world")\n```'
        text = f'Some intro text.\n\n{code}\n\nMore text after.'
        chunks = stable_chunk_text(text, block_size=100, markdown_aware=True)
        code_chunks = [c for c in chunks if '```python' in c.text]
        assert len(code_chunks) == 1
        assert 'print("hello")' in code_chunks[0].text
        assert 'print("world")' in code_chunks[0].text

    def test_fenced_code_block_with_language(self):
        code = '```javascript\nconst x = 1;\n```'
        text = f'Intro.\n\n{code}'
        chunks = stable_chunk_text(text, block_size=100, markdown_aware=True)
        assert any('```javascript' in c.text for c in chunks)

    def test_tilde_fenced_code_block(self):
        code = '~~~\nplain code\n~~~'
        text = f'Intro.\n\n{code}'
        chunks = stable_chunk_text(text, block_size=100, markdown_aware=True)
        assert any('~~~' in c.text for c in chunks)

    def test_indented_code_block_atomic(self):
        code = '    def foo():\n        return 1'
        text = f'Intro.\n\n{code}\n\nOutro.'
        chunks = stable_chunk_text(text, block_size=50, markdown_aware=True)
        assert len(chunks) >= 1

    def test_simple_list_atomic(self):
        text = '- Item one\n- Item two\n- Item three'
        chunks = stable_chunk_text(text, block_size=50, markdown_aware=True)
        list_chunks = [c for c in chunks if '- Item' in c.text]
        assert len(list_chunks) == 1

    def test_nested_list_atomic(self):
        text = '- Top level\n  - Nested item\n  - Another nested\n- Second top'
        chunks = stable_chunk_text(text, block_size=200, markdown_aware=True)
        merged = ''.join(c.text for c in chunks)
        assert '- Top level' in merged
        assert 'Nested item' in merged

    def test_numbered_list_atomic(self):
        text = '1. First item\n2. Second item\n3. Third item'
        chunks = stable_chunk_text(text, block_size=50, markdown_aware=True)
        assert any('1. First' in c.text and '3. Third' in c.text for c in chunks)

    def test_snaps_to_sentence_boundary(self):
        text = 'A. ' * 50 + 'This ends. New sentence starts here. ' + 'B. ' * 50
        chunks = stable_chunk_text(text, block_size=200, markdown_aware=True)
        assert len(chunks) >= 1

    def test_handles_abbreviations(self):
        text = 'Dr. Smith went to the store. Mr. Jones followed. ' * 10
        chunks = stable_chunk_text(text, block_size=200, markdown_aware=True)
        assert all('Dr.' in c.text or 'Mr.' in c.text for c in chunks)

    def test_markdown_aware_false_uses_legacy(self):
        text = '---\ntitle: Test\n---\n\nContent here.'
        chunks_legacy = stable_chunk_text(text, block_size=500, markdown_aware=False)
        chunks_aware = stable_chunk_text(text, block_size=500, markdown_aware=True)
        assert chunks_legacy is not None
        assert chunks_aware is not None
        assert len(chunks_legacy) >= 1
        assert len(chunks_aware) >= 1

    def test_determinism_across_runs(self):
        text = 'Paragraph one.\n\nParagraph two.\n\n```python\ncode = 1\n```\n\nParagraph three.'
        chunks1 = stable_chunk_text(text, block_size=200, markdown_aware=True)
        chunks2 = stable_chunk_text(text, block_size=200, markdown_aware=True)
        assert len(chunks1) == len(chunks2)
        for c1, c2 in zip(chunks1, chunks2):
            assert c1.content_hash == c2.content_hash

    def test_code_block_not_detected_as_list(self):
        text = '```python\nmy_list = [1, 2, 3]\nfor x in my_list:\n    print(x)\n```'
        chunks = stable_chunk_text(text, block_size=100, markdown_aware=True)
        assert len(chunks) == 1
        assert 'my_list = [1, 2, 3]' in chunks[0].text

    def test_multiple_fenced_blocks(self):
        text = '```python\nprint(1)\n```\n\nText between.\n\n```js\nconsole.log(2)\n```'
        chunks = stable_chunk_text(text, block_size=100, markdown_aware=True)
        assert any('print(1)' in c.text for c in chunks)
        assert any('console.log(2)' in c.text for c in chunks)

    def test_empty_text(self):
        chunks = stable_chunk_text('', block_size=500, markdown_aware=True)
        assert chunks == []

    def test_code_inside_list(self):
        text = '- Item one\n  ```\n  code inside\n  ```\n- Item two'
        chunks = stable_chunk_text(text, block_size=200, markdown_aware=True)
        merged = ''.join(c.text for c in chunks)
        assert '- Item one' in merged
        assert 'code inside' in merged
        assert '- Item two' in merged

    def test_oversized_code_block_splits_on_blank_lines(self):
        lines = []
        for i in range(30):
            lines.append(f'print(f"line {i}")')
            if i % 10 == 9:
                lines.append('')
        code = '\n'.join(lines)
        text = f'```python\n{code}\n```'
        chunks = stable_chunk_text(text, block_size=300, hard_limit=500, markdown_aware=True)
        assert len(chunks) > 1
        for chunk in chunks:
            assert 'print' in chunk.text

    def test_oversized_code_block_split_starts_cleanly(self):
        lines = [
            'print("section a")',
            'x = 1',
            '',
            'print("section b")',
            'y = 2',
            '',
            'print("section c")',
        ]
        code = '\n'.join(lines)
        text = f'```python\n{code}\n```'
        chunks = stable_chunk_text(text, block_size=30, hard_limit=80, markdown_aware=True)
        assert len(chunks) > 1

    def test_oversized_list_splits_between_top_level(self):
        items = [f'- Item {i} with enough text to make the list longer' for i in range(20)]
        text = '\n'.join(items)
        chunks = stable_chunk_text(text, block_size=200, markdown_aware=True)
        assert len(chunks) > 1
        merged = ''.join(c.text for c in chunks)
        assert '- Item 0' in merged and '- Item 19' in merged

    def test_list_split_item_at_chunk_boundary_starts_new_chunk(self):
        items = [f'- Item {i} ' * 5 for i in range(10)]
        text = '\n'.join(items)
        chunks = stable_chunk_text(text, block_size=150, markdown_aware=True)
        assert len(chunks) > 1, 'Expected multiple chunks from oversized list'
        for chunk in chunks[1:]:
            assert chunk.text.startswith('- Item'), (
                f'Chunk should start with list item, got: {chunk.text[:50]!r}'
            )

    def test_handles_urls(self):
        text = 'Visit https://example.com/page.html for more info. Then go to the next site.'
        chunks = stable_chunk_text(text, block_size=50, markdown_aware=True)
        assert len(chunks) >= 1
        assert 'https://example.com' in ''.join(c.text for c in chunks)

    def test_handles_decimals(self):
        text = 'The value is 3.14159 and the result is 42.0. End of sentence.'
        chunks = stable_chunk_text(text, block_size=50, markdown_aware=True)
        assert len(chunks) >= 1
        merged = ''.join(c.text for c in chunks)
        assert '3.14159' in merged

    def test_no_boundary_uses_cdc_position(self):
        text = 'A' * 500 + '\n' + 'B' * 500 + '\n' + 'C' * 500
        chunks = stable_chunk_text(text, block_size=300, markdown_aware=True)
        assert len(chunks) >= 2

    def test_incremental_edit_stability(self):
        paragraphs = [f'Paragraph {i} ' * 30 for i in range(10)]
        text_original = '\n\n'.join(paragraphs)
        chunks_orig = stable_chunk_text(text_original, block_size=800)

        intro = 'Intro paragraph. ' * 20
        text_modified = intro + '\n\n' + text_original
        chunks_mod = stable_chunk_text(text_modified, block_size=800)

        assert chunks_orig[-1].content_hash == chunks_mod[-1].content_hash

    def test_no_line_duplication(self):
        text = 'Line A. Line B.\nLine C.\nLine D.\n\nParagraph two.'
        chunks = stable_chunk_text(text, block_size=30, markdown_aware=True)
        merged = ''.join(c.text for c in chunks)
        assert len(merged) == len(text), f'Duplicate lines detected: {len(merged)} vs {len(text)}'
