import { describe, it, expect } from 'vitest';
import { buildNoteContent } from '../src/lib/frontmatter';

describe('buildNoteContent', () => {
  it('builds frontmatter with all fields', () => {
    const result = buildNoteContent({
      url: 'https://example.com/article',
      hostname: 'example.com',
      byline: 'Jane Doe',
      siteName: 'Example Blog',
      publishedTime: '2026-03-16T10:00:00Z',
      markdown: '# Hello\n\nWorld',
    });

    expect(result).toBe(
      [
        '---',
        'source_url: https://example.com/article',
        'hostname: example.com',
        'author: Jane Doe',
        'site_name: Example Blog',
        'publish_date: 2026-03-16T10:00:00Z',
        '---',
        '# Hello\n\nWorld',
      ].join('\n'),
    );
  });

  it('omits optional fields when empty', () => {
    const result = buildNoteContent({
      url: 'https://test.com/page',
      hostname: 'test.com',
      markdown: 'Some content',
    });

    expect(result).not.toContain('author:');
    expect(result).not.toContain('site_name:');
    expect(result).not.toContain('publish_date:');
    expect(result).toContain('source_url: https://test.com/page');
    expect(result).toContain('hostname: test.com');
    expect(result).toContain('Some content');
  });

  it('starts with --- and ends with markdown content', () => {
    const result = buildNoteContent({
      url: 'https://x.com',
      hostname: 'x.com',
      markdown: 'Body text here',
    });

    expect(result.startsWith('---\n')).toBe(true);
    expect(result.endsWith('Body text here')).toBe(true);
    const dashes = result.match(/^---$/gm);
    expect(dashes).toHaveLength(2);
  });
});
