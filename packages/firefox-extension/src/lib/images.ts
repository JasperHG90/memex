/**
 * Image extraction logic for article content.
 * Finds images in Readability-extracted HTML, downloads them via a provided
 * fetch function, rewrites URLs to local filenames, and converts to Markdown.
 */

import TurndownService from 'turndown';

/** Extract MIME type and base64 payload from a data: URL. Returns null if not a valid base64 data URI. */
export function parseDataUrl(url: string): { mimeType: string; base64: string } | null {
  const match = url.match(/^data:(image\/[^;]+);base64,(.+)$/);
  if (!match) return null;
  return { mimeType: match[1], base64: match[2] };
}

/**
 * URL patterns that indicate non-content images.
 * Uses path-segment anchors to avoid false positives like "/article-icons-of-design/hero.jpg".
 */
const NON_CONTENT_PATTERNS = [
  /[/._-]avatar[/._-s]/i,
  /[/._-]profile[_-]?(pic|img|photo)/i,
  /[/._-]logo[/._-s]/i,
  /[/._-]favicon[/._-]/i,
  /[/._-]badge[/._-s]/i,
  /[/._-]button[/._-s]/i,
  /[/._-]banner[_-]?ad[/._-s]/i,
  /[/._-]sponsor/i,
  /[/._-]tracking[/._-]/i,
  /[/._-]pixel[/._-s.]/i,
  /gravatar\.com\//i,
  /\/1x1\./i,
  /\/spacer\./i,
];

export function isNonContentImage(url: string): boolean {
  return NON_CONTENT_PATTERNS.some((pattern) => pattern.test(url));
}

export function guessImageExtension(url: string): string {
  try {
    const pathname = new URL(url).pathname.toLowerCase();
    if (pathname.endsWith('.png')) return '.png';
    if (pathname.endsWith('.gif')) return '.gif';
    if (pathname.endsWith('.webp')) return '.webp';
    if (pathname.endsWith('.svg')) return '.svg';
    if (pathname.endsWith('.avif')) return '.avif';
  } catch {
    // ignore
  }
  return '.jpg';
}

export function contentTypeToExtension(contentType: string | undefined): string | null {
  if (!contentType) return null;
  const ct = contentType.split(';')[0].trim().toLowerCase();
  const map: Record<string, string> = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/svg+xml': '.svg',
    'image/avif': '.avif',
  };
  return map[ct] ?? null;
}

/** Parse a srcset attribute and return the URL of the largest candidate. */
export function bestSrcsetUrl(srcset: string): string | null {
  let best: { url: string; width: number } | null = null;
  for (const candidate of srcset.split(',')) {
    const parts = candidate.trim().split(/\s+/);
    if (parts.length < 1 || !parts[0]) continue;
    const url = parts[0];
    const descriptor = parts[1] ?? '1x';
    let width = 0;
    if (descriptor.endsWith('w')) {
      width = parseInt(descriptor, 10) || 0;
    } else if (descriptor.endsWith('x')) {
      width = (parseFloat(descriptor) || 1) * 1000;
    }
    if (!best || width > best.width) {
      best = { url, width };
    }
  }
  return best?.url ?? null;
}

export interface DownloadResult {
  ok: boolean;
  base64?: string;
  contentType?: string;
}

export type ImageDownloader = (url: string) => Promise<DownloadResult>;

/**
 * Find the matching key in blobImagesByAlt for a given alt text.
 * Handles deduplication suffixes (e.g. "diagram.png__1") added by the popup.
 */
function findBlobKey(
  alt: string,
  blobImagesByAlt: Record<string, string>,
  alreadyMatched: Set<string>,
): string | null {
  // Exact match (most common case)
  if (alt in blobImagesByAlt && !alreadyMatched.has(alt)) return alt;
  // Find first unmatched key that starts with the alt (handles __N suffixes)
  for (const key of Object.keys(blobImagesByAlt)) {
    if (alreadyMatched.has(key)) continue;
    if (key === alt || key.startsWith(alt + '__')) return key;
  }
  return null;
}

/**
 * Given Readability's article HTML and the page URL, find all content images,
 * download them via the provided downloader, rewrite URLs to local filenames,
 * and return the final markdown + images map.
 *
 * @param blobImagesByAlt - Pre-resolved blob images keyed by alt text.  Readability
 *   strips data: URIs from src attributes, so blob images that were converted to
 *   data: URLs in the page context must be passed separately and matched by alt.
 */
export async function extractArticleImages(
  articleHtml: string,
  pageUrl: string,
  downloadImage: ImageDownloader,
  timeoutMs: number = 30_000,
  blobImagesByAlt: Record<string, string> = {},
): Promise<{ markdown: string; images: Record<string, string> }> {
  const articleDoc = new DOMParser().parseFromString(articleHtml, 'text/html');

  const urlToFilename = new Map<string, string>();
  const dataUrlImages = new Map<string, { base64: string; filename: string }>();
  const seenUrls = new Set<string>();
  let imgIndex = 0;

  function registerImage(rawUrl: string): string | null {
    // Handle data: URLs (e.g. converted from blob: URLs by the in-page resolver)
    const dataUrl = parseDataUrl(rawUrl.trim());
    if (dataUrl) {
      if (seenUrls.has(rawUrl)) return dataUrlImages.get(rawUrl)?.filename ?? null;
      seenUrls.add(rawUrl);
      const ext = contentTypeToExtension(dataUrl.mimeType) ?? '.png';
      const filename = `image-${imgIndex++}${ext}`;
      dataUrlImages.set(rawUrl, { base64: dataUrl.base64, filename });
      return filename;
    }

    let absoluteUrl: string;
    try {
      absoluteUrl = new URL(rawUrl.trim(), pageUrl).href;
    } catch {
      return null;
    }
    if (seenUrls.has(absoluteUrl)) return urlToFilename.get(absoluteUrl) ?? null;
    if (isNonContentImage(absoluteUrl)) return null;
    seenUrls.add(absoluteUrl);
    const ext = guessImageExtension(absoluteUrl);
    const filename = `image-${imgIndex++}${ext}`;
    urlToFilename.set(absoluteUrl, filename);
    return filename;
  }

  // Track blob images matched by alt text (for src-less imgs left by Readability)
  const altToFilename = new Map<string, string>();
  const matchedBlobKeys = new Set<string>();

  // Process <img> elements
  const imgEls = articleDoc.querySelectorAll('img');
  for (const img of imgEls) {
    const src =
      img.getAttribute('src') ||
      img.getAttribute('data-src') ||
      img.getAttribute('data-lazy-src') ||
      img.getAttribute('data-original');

    const srcset = img.getAttribute('srcset') || img.getAttribute('data-srcset');
    const srcsetUrl = srcset ? bestSrcsetUrl(srcset) : null;

    const primaryUrl = srcsetUrl ?? src;
    if (primaryUrl) {
      registerImage(primaryUrl);
    } else {
      // No src — Readability may have stripped a data: URI.  Try matching
      // against pre-resolved blob images by alt text (with dedup suffix support).
      const alt = img.getAttribute('alt') ?? '';
      const blobKey = findBlobKey(alt, blobImagesByAlt, matchedBlobKeys);
      if (blobKey) {
        const dataUrl = blobImagesByAlt[blobKey];
        const parsed = parseDataUrl(dataUrl);
        if (parsed) {
          const ext = contentTypeToExtension(parsed.mimeType) ?? '.png';
          const filename = `image-${imgIndex++}${ext}`;
          altToFilename.set(alt, filename);
          dataUrlImages.set(dataUrl, { base64: parsed.base64, filename });
          matchedBlobKeys.add(blobKey);
        }
      }
    }
  }

  // Process <picture><source> elements
  const sourceEls = articleDoc.querySelectorAll('picture source[srcset]');
  for (const source of sourceEls) {
    const srcset = source.getAttribute('srcset');
    if (!srcset) continue;
    const type = source.getAttribute('type') ?? '';
    if (type && !type.startsWith('image/')) continue;
    const url = bestSrcsetUrl(srcset);
    if (url) registerImage(url);
  }

  // Add data: URL images directly (no download needed)
  const images: Record<string, string> = {};
  for (const [, { base64, filename }] of dataUrlImages) {
    images[filename] = base64;
  }

  // Download all remote images in parallel with a global timeout
  const downloadPromises = Array.from(urlToFilename.entries()).map(async ([url, filename]) => {
    try {
      const resp = await downloadImage(url);
      if (resp?.ok && resp.base64) {
        const actualExt = contentTypeToExtension(resp.contentType);
        const correctedFilename =
          actualExt && !filename.endsWith(actualExt)
            ? filename.replace(/\.[^.]+$/, actualExt)
            : filename;
        if (correctedFilename !== filename) {
          urlToFilename.set(url, correctedFilename);
        }
        images[correctedFilename] = resp.base64;
      }
    } catch {
      // Individual download failed — skip
    }
  });

  await Promise.race([
    Promise.allSettled(downloadPromises),
    new Promise((resolve) => setTimeout(resolve, timeoutMs)),
  ]);

  // Rewrite image references to use local filenames
  for (const img of imgEls) {
    const src =
      img.getAttribute('src') ||
      img.getAttribute('data-src') ||
      img.getAttribute('data-lazy-src') ||
      img.getAttribute('data-original');
    const srcset = img.getAttribute('srcset') || img.getAttribute('data-srcset');
    const srcsetUrl = srcset ? bestSrcsetUrl(srcset) : null;
    const primaryUrl = srcsetUrl ?? src;

    if (!primaryUrl) {
      // No src — check if this is a blob image matched by alt text
      const alt = img.getAttribute('alt') ?? '';
      const blobFilename = altToFilename.get(alt);
      if (blobFilename && images[blobFilename]) {
        img.setAttribute('src', blobFilename);
      }
      continue;
    }

    // Check data URL images first (from resolved blob: URLs)
    const dataEntry = dataUrlImages.get(primaryUrl.trim());
    if (dataEntry && images[dataEntry.filename]) {
      img.setAttribute('src', dataEntry.filename);
      img.removeAttribute('srcset');
      img.removeAttribute('data-srcset');
      img.removeAttribute('data-src');
      img.removeAttribute('data-lazy-src');
      img.removeAttribute('data-original');
      continue;
    }

    let absoluteUrl: string;
    try {
      absoluteUrl = new URL(primaryUrl.trim(), pageUrl).href;
    } catch {
      continue;
    }

    const filename = urlToFilename.get(absoluteUrl);
    if (filename && images[filename]) {
      img.setAttribute('src', filename);
      img.removeAttribute('srcset');
      img.removeAttribute('data-srcset');
      img.removeAttribute('data-src');
      img.removeAttribute('data-lazy-src');
      img.removeAttribute('data-original');
    } else if (src) {
      try {
        img.setAttribute('src', new URL(src.trim(), pageUrl).href);
      } catch {
        // leave as-is
      }
    }
  }

  const turndown = new TurndownService({ headingStyle: 'atx', codeBlockStyle: 'fenced' });
  let markdown = turndown.turndown(articleDoc.body.innerHTML);

  // Append any blob images that Readability dropped entirely (no <img> tag survived).
  for (const [key, dataUrl] of Object.entries(blobImagesByAlt)) {
    if (matchedBlobKeys.has(key)) continue;
    const parsed = parseDataUrl(dataUrl);
    if (!parsed) continue;
    const ext = contentTypeToExtension(parsed.mimeType) ?? '.png';
    const filename = `image-${imgIndex++}${ext}`;
    images[filename] = parsed.base64;
    // Strip the dedup suffix for the alt text
    const alt = key.replace(/__\d+$/, '');
    markdown += `\n\n![${alt}](${filename})`;
  }

  return { markdown, images };
}
