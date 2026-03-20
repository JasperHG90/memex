/**
 * Enhanced metadata extraction from HTML documents.
 *
 * Priority order:
 * 1. JSON-LD (application/ld+json)
 * 2. Open Graph meta tags
 * 3. Standard meta tags (name="author", name="date", etc.)
 * 4. Schema.org microdata (itemprop)
 * 5. <time> elements with datetime attribute (date only)
 */

export interface ArticleMetadata {
  author: string | null;
  publishedTime: string | null;
}

/**
 * Normalize a date string to YYYY-MM-DD format.
 * Returns null if the date cannot be parsed.
 */
export function normalizeDate(raw: string): string | null {
  if (!raw || !raw.trim()) return null;
  try {
    const d = new Date(raw.trim());
    if (isNaN(d.getTime())) return null;
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  } catch {
    return null;
  }
}

/** Extract author name from a JSON-LD author field (string, object, or array). */
function extractJsonLdAuthor(author: unknown): string | null {
  if (!author) return null;
  if (typeof author === 'string') return author;
  if (Array.isArray(author)) {
    const names = author
      .map((a) => (typeof a === 'string' ? a : a?.name))
      .filter(Boolean);
    return names.length > 0 ? names.join(', ') : null;
  }
  if (typeof author === 'object' && author !== null && 'name' in author) {
    return (author as { name: string }).name || null;
  }
  return null;
}

/** Try to extract metadata from a single JSON-LD object. */
function extractFromJsonLdObject(obj: Record<string, unknown>): {
  author: string | null;
  date: string | null;
} {
  const author = extractJsonLdAuthor(obj.author);
  const date =
    (typeof obj.datePublished === 'string' ? obj.datePublished : null) ||
    (typeof obj.dateCreated === 'string' ? obj.dateCreated : null);
  return { author, date };
}

/** Extract metadata from all JSON-LD blocks in the document. */
function extractFromJsonLd(doc: Document): { author: string | null; date: string | null } {
  const scripts = doc.querySelectorAll('script[type="application/ld+json"]');
  for (const script of scripts) {
    try {
      const data = JSON.parse(script.textContent || '');

      // Handle @graph arrays
      if (data['@graph'] && Array.isArray(data['@graph'])) {
        for (const item of data['@graph']) {
          const result = extractFromJsonLdObject(item);
          if (result.author || result.date) return result;
        }
      }

      // Handle direct object
      const result = extractFromJsonLdObject(data);
      if (result.author || result.date) return result;
    } catch {
      // Malformed JSON-LD — skip this block
    }
  }
  return { author: null, date: null };
}

/** Extract metadata from Open Graph meta tags. */
function extractFromOpenGraph(doc: Document): { author: string | null; date: string | null } {
  const date =
    doc.querySelector('meta[property="article:published_time"]')?.getAttribute('content') || null;
  const author =
    doc.querySelector('meta[property="article:author"]')?.getAttribute('content') || null;
  return { author, date };
}

/** Extract metadata from standard meta tags. */
function extractFromMetaTags(doc: Document): { author: string | null; date: string | null } {
  const authorSelectors = [
    'meta[name="author"]',
    'meta[name="citation_author"]',
    'meta[name="dc.creator"]',
  ];
  const dateSelectors = [
    'meta[name="date"]',
    'meta[name="citation_date"]',
    'meta[name="dc.date"]',
  ];

  let author: string | null = null;
  for (const sel of authorSelectors) {
    const content = doc.querySelector(sel)?.getAttribute('content');
    if (content) {
      author = content;
      break;
    }
  }

  let date: string | null = null;
  for (const sel of dateSelectors) {
    const content = doc.querySelector(sel)?.getAttribute('content');
    if (content) {
      date = content;
      break;
    }
  }

  return { author, date };
}

/** Extract metadata from Schema.org microdata attributes. */
function extractFromMicrodata(doc: Document): { author: string | null; date: string | null } {
  const dateEl = doc.querySelector('[itemprop="datePublished"]');
  const date =
    dateEl?.getAttribute('content') || dateEl?.getAttribute('datetime') || dateEl?.textContent || null;

  const authorEl = doc.querySelector('[itemprop="author"]');
  const author =
    authorEl?.getAttribute('content') || authorEl?.textContent || null;

  return { author, date };
}

/** Extract date from <time> elements with datetime attribute. */
function extractFromTimeElements(doc: Document): string | null {
  const timeEl = doc.querySelector('time[datetime]');
  return timeEl?.getAttribute('datetime') || null;
}

/**
 * Extract author and publish date from an HTML document using
 * multiple extraction strategies in priority order.
 */
export function extractMetadata(doc: Document): ArticleMetadata {
  let author: string | null = null;
  let rawDate: string | null = null;

  // 1. JSON-LD (highest priority)
  const jsonLd = extractFromJsonLd(doc);
  author = jsonLd.author;
  rawDate = jsonLd.date;

  // 2. Open Graph
  const og = extractFromOpenGraph(doc);
  author = author || og.author;
  rawDate = rawDate || og.date;

  // 3. Standard meta tags
  const meta = extractFromMetaTags(doc);
  author = author || meta.author;
  rawDate = rawDate || meta.date;

  // 4. Schema.org microdata
  const microdata = extractFromMicrodata(doc);
  author = author || microdata.author;
  rawDate = rawDate || microdata.date;

  // 5. <time> elements (date only)
  rawDate = rawDate || extractFromTimeElements(doc);

  return {
    author: author?.trim() || null,
    publishedTime: normalizeDate(rawDate || ''),
  };
}
