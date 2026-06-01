"""Unit tests for the per-section image-reference parser."""

from __future__ import annotations

from memex_core.memory.extraction.pipeline.asset_parser import extract_image_refs


def _paths(text: str) -> list[str]:
    return [r['path'] for r in extract_image_refs(text)]


class TestMarkdownImages:
    def test_with_alt(self):
        (ref,) = extract_image_refs('![a cat](img/cat.png)')
        assert ref == {
            'path': 'img/cat.png',
            'alt_text': 'a cat',
            'filename': 'cat.png',
        }

    def test_empty_alt_is_none(self):
        (ref,) = extract_image_refs('![](x.png)')
        assert ref['alt_text'] is None
        assert ref['path'] == 'x.png'

    def test_trailing_title_ignored(self):
        (ref,) = extract_image_refs('![a](b.png "the title")')
        assert ref['path'] == 'b.png'
        assert ref['alt_text'] == 'a'

    def test_absolute_path(self):
        (ref,) = extract_image_refs('![x](/assets/y.png)')
        assert ref['path'] == '/assets/y.png'
        assert ref['filename'] == 'y.png'

    def test_whitespace_only_alt_is_none(self):
        (ref,) = extract_image_refs('![   ](z.png)')
        assert ref['alt_text'] is None


class TestWikiImages:
    def test_basic(self):
        (ref,) = extract_image_refs('![[dir/e.jpg]]')
        assert ref['path'] == 'dir/e.jpg'
        assert ref['alt_text'] is None
        assert ref['filename'] == 'e.jpg'

    def test_alias_splits_path_and_alt(self):
        (ref,) = extract_image_refs('![[diagram.png|My diagram]]')
        assert ref['path'] == 'diagram.png'
        assert ref['filename'] == 'diagram.png'
        assert ref['alt_text'] == 'My diagram'


class TestHtmlImages:
    def test_src_then_alt(self):
        (ref,) = extract_image_refs('<img src="q.gif" alt="z">')
        assert ref['path'] == 'q.gif'
        assert ref['alt_text'] == 'z'

    def test_alt_then_src(self):
        (ref,) = extract_image_refs('<img alt="z" src="q.gif">')
        assert ref['path'] == 'q.gif'
        assert ref['alt_text'] == 'z'

    def test_single_quotes(self):
        (ref,) = extract_image_refs("<img src='a.png' alt='b'>")
        assert ref['path'] == 'a.png'
        assert ref['alt_text'] == 'b'

    def test_self_closing(self):
        (ref,) = extract_image_refs('<img src="a.png" />')
        assert ref['path'] == 'a.png'
        assert ref['alt_text'] is None

    def test_missing_src_skipped(self):
        assert extract_image_refs('<img alt="no source">') == []

    def test_extra_attributes(self):
        (ref,) = extract_image_refs('<img width="50" src="a.png" height="50" alt="c">')
        assert ref['path'] == 'a.png'
        assert ref['alt_text'] == 'c'


class TestExternalUrlsSkipped:
    def test_markdown_external(self):
        assert extract_image_refs('![x](https://host.com/a.png)') == []

    def test_http_external(self):
        assert extract_image_refs('![x](http://host.com/a.png)') == []

    def test_wiki_external(self):
        assert extract_image_refs('![[https://host.com/a.png]]') == []

    def test_html_external(self):
        assert extract_image_refs('<img src="https://host.com/a.png">') == []


class TestCodeRegionsSkipped:
    def test_fenced_block(self):
        assert extract_image_refs('```\n![x](in.png)\n```') == []

    def test_inline_code(self):
        assert extract_image_refs('use `![y](inl.png)` here') == []

    def test_real_image_outside_code_block_survives(self):
        text = '```\n![x](in.png)\n```\n\n![real](out.png)'
        assert _paths(text) == ['out.png']

    def test_unterminated_fence_suppresses_to_eof(self):
        # A truncated/streamed note may leave a fence open; its body must not
        # leak phantom assets.
        assert extract_image_refs('```\n![x](in.png)') == []

    def test_tilde_fence(self):
        assert extract_image_refs('~~~\n![x](in.png)\n~~~') == []

    def test_inline_fence_token_in_prose_is_not_a_code_block(self):
        # A ``` token mid-sentence must not be treated as a fence opener and
        # swallow a real image that follows.
        text = 'type three backticks ``` to open code, then ![real](r.png)'
        assert _paths(text) == ['r.png']


class TestDataUrisSkipped:
    def test_markdown_data_uri(self):
        assert extract_image_refs('![x](data:image/png;base64,AAAA)') == []

    def test_html_data_uri(self):
        assert extract_image_refs('<img src="data:image/gif;base64,BBBB">') == []


class TestMultipleAndDedup:
    def test_multiple_images(self):
        assert _paths('![a](1.png) text ![b](2.png)') == ['1.png', '2.png']

    def test_duplicate_path_collapsed_first_alt_wins(self):
        refs = extract_image_refs('![first](p.png) ![second](p.png)')
        assert len(refs) == 1
        assert refs[0]['alt_text'] == 'first'

    def test_mixed_syntaxes(self):
        text = '![md](a.png)\n![[b.png]]\n<img src="c.png" alt="html">'
        assert _paths(text) == ['a.png', 'b.png', 'c.png']

    def test_document_order_across_syntaxes(self):
        # HTML img appears first, then markdown — order must follow position,
        # not parse-pass order.
        text = '<img src="first.png">\nthen ![second](second.png)'
        assert _paths(text) == ['first.png', 'second.png']


class TestEdgeCases:
    def test_empty_string(self):
        assert extract_image_refs('') == []

    def test_no_images(self):
        assert extract_image_refs('plain text, a [link](page.md) but no image') == []

    def test_link_not_image(self):
        # [text](url) without leading ! is a link, not an image.
        assert extract_image_refs('[click](file.png)') == []
