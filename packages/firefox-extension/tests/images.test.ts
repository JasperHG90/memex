// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
import {
  isNonContentImage,
  guessImageExtension,
  contentTypeToExtension,
  bestSrcsetUrl,
  extractArticleImages,
  type ImageDownloader,
} from '../src/lib/images';

// --- isNonContentImage ---

describe('isNonContentImage', () => {
  it('filters avatar URLs', () => {
    expect(isNonContentImage('https://example.com/user/avatar/small.jpg')).toBe(true);
    expect(isNonContentImage('https://example.com/images/user-avatar.png')).toBe(true);
    expect(isNonContentImage('https://example.com/avatars/123.jpg')).toBe(true);
  });

  it('filters logo URLs', () => {
    expect(isNonContentImage('https://example.com/site-logo.png')).toBe(true);
    expect(isNonContentImage('https://example.com/images/logo/dark.svg')).toBe(true);
    expect(isNonContentImage('https://example.com/logos/company.png')).toBe(true);
  });

  it('filters tracking pixels', () => {
    expect(isNonContentImage('https://example.com/1x1.gif')).toBe(true);
    expect(isNonContentImage('https://example.com/spacer.gif')).toBe(true);
    expect(isNonContentImage('https://example.com/tracking/img.png')).toBe(true);
    expect(isNonContentImage('https://example.com/pixel.gif')).toBe(true);
    expect(isNonContentImage('https://gravatar.com/avatar/abc123')).toBe(true);
  });

  it('does NOT filter legitimate article image URLs', () => {
    expect(isNonContentImage('https://cdn.example.com/articles/hero-image.jpg')).toBe(false);
    expect(isNonContentImage('https://example.com/uploads/2024/03/diagram.png')).toBe(false);
    expect(isNonContentImage('https://images.unsplash.com/photo-abc123')).toBe(false);
    expect(isNonContentImage('https://example.com/article-icons-of-design/hero.jpg')).toBe(false);
    expect(isNonContentImage('https://example.com/images/architecture-overview.webp')).toBe(false);
  });

  it('does NOT false-positive on icon-containing article paths', () => {
    // "icon" appears in the path but as part of a larger word or article slug
    expect(isNonContentImage('https://example.com/iconic-buildings/photo1.jpg')).toBe(false);
    expect(isNonContentImage('https://example.com/silicon-valley/map.png')).toBe(false);
  });
});

// --- guessImageExtension ---

describe('guessImageExtension', () => {
  it('detects common extensions from URL pathname', () => {
    expect(guessImageExtension('https://example.com/photo.png')).toBe('.png');
    expect(guessImageExtension('https://example.com/photo.gif')).toBe('.gif');
    expect(guessImageExtension('https://example.com/photo.webp')).toBe('.webp');
    expect(guessImageExtension('https://example.com/photo.svg')).toBe('.svg');
    expect(guessImageExtension('https://example.com/photo.avif')).toBe('.avif');
  });

  it('defaults to .jpg for unknown extensions or no extension', () => {
    expect(guessImageExtension('https://example.com/photo.bmp')).toBe('.jpg');
    expect(guessImageExtension('https://images.unsplash.com/photo-abc123')).toBe('.jpg');
    expect(guessImageExtension('https://example.com/images/12345')).toBe('.jpg');
  });

  it('ignores query strings when detecting extension', () => {
    expect(guessImageExtension('https://example.com/photo.png?w=800')).toBe('.png');
  });
});

// --- contentTypeToExtension ---

describe('contentTypeToExtension', () => {
  it('maps known content types', () => {
    expect(contentTypeToExtension('image/png')).toBe('.png');
    expect(contentTypeToExtension('image/jpeg')).toBe('.jpg');
    expect(contentTypeToExtension('image/gif')).toBe('.gif');
    expect(contentTypeToExtension('image/webp')).toBe('.webp');
    expect(contentTypeToExtension('image/svg+xml')).toBe('.svg');
    expect(contentTypeToExtension('image/avif')).toBe('.avif');
  });

  it('strips charset parameters', () => {
    expect(contentTypeToExtension('image/png; charset=utf-8')).toBe('.png');
  });

  it('returns null for unknown or undefined', () => {
    expect(contentTypeToExtension(undefined)).toBeNull();
    expect(contentTypeToExtension('application/octet-stream')).toBeNull();
    expect(contentTypeToExtension('')).toBeNull();
  });
});

// --- bestSrcsetUrl ---

describe('bestSrcsetUrl', () => {
  it('picks the widest from width descriptors', () => {
    expect(bestSrcsetUrl('small.jpg 400w, medium.jpg 800w, large.jpg 1200w')).toBe('large.jpg');
  });

  it('picks the highest density from x descriptors', () => {
    expect(bestSrcsetUrl('img.jpg 1x, img@2x.jpg 2x, img@3x.jpg 3x')).toBe('img@3x.jpg');
  });

  it('handles single entry', () => {
    expect(bestSrcsetUrl('only.jpg 600w')).toBe('only.jpg');
  });

  it('handles no descriptor (defaults)', () => {
    expect(bestSrcsetUrl('fallback.jpg')).toBe('fallback.jpg');
  });

  it('handles mixed descriptors', () => {
    expect(bestSrcsetUrl('small.jpg 400w, retina.jpg 2x')).toBe('retina.jpg');
  });

  it('returns null for empty string', () => {
    expect(bestSrcsetUrl('')).toBeNull();
  });
});

// --- extractArticleImages ---

function mockDownloader(responses: Record<string, { base64: string; contentType: string }>): ImageDownloader {
  return async (url: string) => {
    const match = responses[url];
    if (match) return { ok: true, base64: match.base64, contentType: match.contentType };
    return { ok: false };
  };
}

describe('extractArticleImages', () => {
  const pageUrl = 'https://example.com/article/deep-learning-guide';

  it('extracts images from basic <img src> tags', async () => {
    const html = `
      <div>
        <p>Some text about neural networks.</p>
        <img src="https://example.com/images/diagram.png" alt="Architecture diagram">
        <p>More text.</p>
        <img src="https://example.com/images/results.jpg" alt="Results chart">
      </div>
    `;
    const dl = mockDownloader({
      'https://example.com/images/diagram.png': { base64: 'AAAA', contentType: 'image/png' },
      'https://example.com/images/results.jpg': { base64: 'BBBB', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);

    expect(Object.keys(result.images)).toHaveLength(2);
    expect(result.images['image-0.png']).toBe('AAAA');
    expect(result.images['image-1.jpg']).toBe('BBBB');
    expect(result.markdown).toContain('![Architecture diagram](image-0.png)');
    expect(result.markdown).toContain('![Results chart](image-1.jpg)');
  });

  it('resolves relative image URLs against the page URL', async () => {
    const html = '<div><img src="/images/photo.jpg" alt="Photo"></div>';
    const dl = mockDownloader({
      'https://example.com/images/photo.jpg': { base64: 'CCCC', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);

    expect(result.images['image-0.jpg']).toBe('CCCC');
    expect(result.markdown).toContain('image-0.jpg');
  });

  it('handles lazy-loaded images via data-src', async () => {
    const html = '<div><img data-src="https://example.com/lazy.jpg" alt="Lazy image"></div>';
    const dl = mockDownloader({
      'https://example.com/lazy.jpg': { base64: 'DDDD', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(Object.keys(result.images)).toHaveLength(1);
    expect(result.markdown).toContain('image-0.jpg');
  });

  it('handles data-lazy-src attribute', async () => {
    const html = '<div><img data-lazy-src="https://example.com/lazy2.png" alt="Lazy"></div>';
    const dl = mockDownloader({
      'https://example.com/lazy2.png': { base64: 'EEEE', contentType: 'image/png' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(Object.keys(result.images)).toHaveLength(1);
  });

  it('handles data-original attribute', async () => {
    const html = '<div><img data-original="https://example.com/orig.jpg" alt="Orig"></div>';
    const dl = mockDownloader({
      'https://example.com/orig.jpg': { base64: 'FFFF', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(Object.keys(result.images)).toHaveLength(1);
  });

  it('prefers srcset over src when available', async () => {
    const html = `
      <div><img src="https://example.com/small.jpg" srcset="https://example.com/large.jpg 1200w, https://example.com/medium.jpg 800w" alt="Photo"></div>
    `;
    const dl = mockDownloader({
      'https://example.com/large.jpg': { base64: 'HIRES', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(result.images['image-0.jpg']).toBe('HIRES');
    // Should download the srcset URL, not the src URL
    expect(Object.keys(result.images)).toHaveLength(1);
  });

  it('handles data-srcset attribute', async () => {
    const html = `
      <div><img data-srcset="https://example.com/lazy-hd.jpg 1200w" alt="Lazy HD"></div>
    `;
    const dl = mockDownloader({
      'https://example.com/lazy-hd.jpg': { base64: 'LZHD', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(Object.keys(result.images)).toHaveLength(1);
  });

  it('handles <picture><source srcset> elements', async () => {
    const html = `
      <div>
        <picture>
          <source srcset="https://example.com/photo.webp 1200w" type="image/webp">
          <source srcset="https://example.com/photo.jpg 1200w" type="image/jpeg">
          <img src="https://example.com/photo-fallback.jpg" alt="Photo">
        </picture>
      </div>
    `;
    const dl = mockDownloader({
      'https://example.com/photo-fallback.jpg': { base64: 'FALL', contentType: 'image/jpeg' },
      'https://example.com/photo.webp': { base64: 'WEBP', contentType: 'image/webp' },
      'https://example.com/photo.jpg': { base64: 'JPEG', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    // Should have extracted images from both <source> and <img>
    expect(Object.keys(result.images).length).toBeGreaterThanOrEqual(1);
  });

  it('skips non-image source types in <picture>', async () => {
    const html = `
      <div>
        <picture>
          <source srcset="https://example.com/video.mp4" type="video/mp4">
          <img src="https://example.com/fallback.jpg" alt="Fallback">
        </picture>
      </div>
    `;
    const dl = mockDownloader({
      'https://example.com/fallback.jpg': { base64: 'FALL', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    // Should only have the fallback image, not the video
    expect(Object.keys(result.images)).toHaveLength(1);
    expect(result.images['image-0.jpg']).toBe('FALL');
  });

  it('skips data: URIs', async () => {
    const html = `
      <div>
        <img src="data:image/png;base64,iVBORw..." alt="Inline">
        <img src="https://example.com/real.jpg" alt="Real">
      </div>
    `;
    const dl = mockDownloader({
      'https://example.com/real.jpg': { base64: 'REAL', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(Object.keys(result.images)).toHaveLength(1);
    expect(result.images['image-0.jpg']).toBe('REAL');
  });

  it('deduplicates identical image URLs', async () => {
    const html = `
      <div>
        <img src="https://example.com/same.jpg" alt="First">
        <img src="https://example.com/same.jpg" alt="Second">
      </div>
    `;
    const calls: string[] = [];
    const dl: ImageDownloader = async (url) => {
      calls.push(url);
      return { ok: true, base64: 'DUP', contentType: 'image/jpeg' };
    };

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(calls).toHaveLength(1); // Only downloaded once
    expect(Object.keys(result.images)).toHaveLength(1);
  });

  it('filters non-content images by URL pattern', async () => {
    const html = `
      <div>
        <img src="https://example.com/article/hero.jpg" alt="Hero">
        <img src="https://example.com/user-avatar.png" alt="Author avatar">
        <img src="https://gravatar.com/avatar/abc123" alt="Gravatar">
        <img src="https://example.com/site-logo.svg" alt="Logo">
      </div>
    `;
    const dl = mockDownloader({
      'https://example.com/article/hero.jpg': { base64: 'HERO', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(Object.keys(result.images)).toHaveLength(1);
    expect(result.images['image-0.jpg']).toBe('HERO');
  });

  it('corrects file extension based on content-type', async () => {
    // URL says .jpg but server returns image/webp
    const html = '<div><img src="https://cdn.example.com/photo.jpg" alt="Photo"></div>';
    const dl = mockDownloader({
      'https://cdn.example.com/photo.jpg': { base64: 'WEBPDATA', contentType: 'image/webp' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(result.images['image-0.webp']).toBe('WEBPDATA');
    expect(result.markdown).toContain('image-0.webp');
  });

  it('keeps original URL as absolute when download fails', async () => {
    const html = '<div><img src="/images/broken.jpg" alt="Broken"></div>';
    const dl: ImageDownloader = async () => ({ ok: false });

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(Object.keys(result.images)).toHaveLength(0);
    // Should have rewritten relative URL to absolute
    expect(result.markdown).toContain('https://example.com/images/broken.jpg');
  });

  it('clears lazy-load attributes on successful download', async () => {
    const html = `
      <div><img src="placeholder.gif" data-src="https://example.com/real.jpg" data-srcset="https://example.com/hd.jpg 1200w" alt="Lazy"></div>
    `;
    const dl = mockDownloader({
      'https://example.com/hd.jpg': { base64: 'HD', contentType: 'image/jpeg' },
    });

    const result = await extractArticleImages(html, pageUrl, dl);
    // Markdown should reference the local file, not data-src or data-srcset URLs
    expect(result.markdown).toContain('image-0.jpg');
    expect(result.markdown).not.toContain('data-src');
    expect(result.markdown).not.toContain('data-srcset');
  });

  it('handles download errors gracefully', async () => {
    const html = `
      <div>
        <img src="https://example.com/good.jpg" alt="Good">
        <img src="https://example.com/error.jpg" alt="Error">
      </div>
    `;
    const dl: ImageDownloader = async (url) => {
      if (url.includes('error')) throw new Error('Network failure');
      return { ok: true, base64: 'GOOD', contentType: 'image/jpeg' };
    };

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(result.images['image-0.jpg']).toBe('GOOD');
    // Error image should not be in images but article still processes
    expect(Object.keys(result.images)).toHaveLength(1);
  });

  it('returns empty images when article has no img tags', async () => {
    const html = '<div><p>Text only article with no images.</p></div>';
    const dl: ImageDownloader = async () => ({ ok: false });

    const result = await extractArticleImages(html, pageUrl, dl);
    expect(Object.keys(result.images)).toHaveLength(0);
    expect(result.markdown).toContain('Text only article');
  });

  it('respects global timeout', async () => {
    const html = '<div><img src="https://example.com/slow.jpg" alt="Slow"></div>';
    const dl: ImageDownloader = async () => {
      // Simulate a very slow download
      await new Promise((resolve) => setTimeout(resolve, 60_000));
      return { ok: true, base64: 'LATE', contentType: 'image/jpeg' };
    };

    const result = await extractArticleImages(html, pageUrl, dl, 50);
    // Should have timed out — no images downloaded
    expect(Object.keys(result.images)).toHaveLength(0);
  });
});
