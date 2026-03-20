import { describe, it, expect } from 'vitest';
import { extractMetadata, normalizeDate } from '../src/lib/metadata';

function makeDoc(html: string): Document {
  const parser = new DOMParser();
  return parser.parseFromString(html, 'text/html');
}

describe('normalizeDate', () => {
  it('normalizes ISO date string', () => {
    expect(normalizeDate('2025-12-09')).toBe('2025-12-09');
  });

  it('normalizes ISO datetime string', () => {
    expect(normalizeDate('2025-12-09T14:30:00Z')).toBe('2025-12-09');
  });

  it('normalizes human-readable date', () => {
    expect(normalizeDate('December 9, 2025')).toBe('2025-12-09');
  });

  it('normalizes date with timezone offset', () => {
    const result = normalizeDate('2025-12-09T14:30:00+05:00');
    expect(result).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('returns null for empty string', () => {
    expect(normalizeDate('')).toBeNull();
  });

  it('returns null for garbage input', () => {
    expect(normalizeDate('not-a-date')).toBeNull();
  });

  it('returns null for whitespace', () => {
    expect(normalizeDate('   ')).toBeNull();
  });
});

describe('extractMetadata', () => {
  describe('JSON-LD extraction', () => {
    it('extracts datePublished and author object', () => {
      const doc = makeDoc(`<html><head>
        <script type="application/ld+json">{
          "@type": "Article",
          "datePublished": "2025-12-09",
          "author": { "@type": "Person", "name": "Alice Smith" }
        }</script>
      </head><body></body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('Alice Smith');
      expect(result.publishedTime).toBe('2025-12-09');
    });

    it('extracts author as plain string', () => {
      const doc = makeDoc(`<html><head>
        <script type="application/ld+json">{
          "@type": "Article",
          "datePublished": "2025-06-15",
          "author": "Bob Jones"
        }</script>
      </head><body></body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('Bob Jones');
    });

    it('extracts author from array', () => {
      const doc = makeDoc(`<html><head>
        <script type="application/ld+json">{
          "@type": "Article",
          "datePublished": "2025-01-01",
          "author": [
            { "@type": "Person", "name": "Alice" },
            { "@type": "Person", "name": "Bob" }
          ]
        }</script>
      </head><body></body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('Alice, Bob');
    });

    it('handles @graph array', () => {
      const doc = makeDoc(`<html><head>
        <script type="application/ld+json">{
          "@context": "https://schema.org",
          "@graph": [
            { "@type": "WebPage" },
            { "@type": "Article", "datePublished": "2025-03-20", "author": "Graph Author" }
          ]
        }</script>
      </head><body></body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('Graph Author');
      expect(result.publishedTime).toBe('2025-03-20');
    });

    it('skips malformed JSON-LD gracefully', () => {
      const doc = makeDoc(`<html><head>
        <script type="application/ld+json">NOT VALID JSON{{{</script>
        <meta property="article:author" content="Fallback Author">
      </head><body></body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('Fallback Author');
    });
  });

  describe('Open Graph fallback', () => {
    it('extracts from OG meta tags', () => {
      const doc = makeDoc(`<html><head>
        <meta property="article:published_time" content="2025-11-01T10:00:00Z">
        <meta property="article:author" content="OG Author">
      </head><body></body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('OG Author');
      expect(result.publishedTime).toBe('2025-11-01');
    });
  });

  describe('meta tag fallback', () => {
    it('extracts from name="author" and name="date"', () => {
      const doc = makeDoc(`<html><head>
        <meta name="author" content="Meta Author">
        <meta name="date" content="2025-08-15">
      </head><body></body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('Meta Author');
      expect(result.publishedTime).toBe('2025-08-15');
    });

    it('extracts from citation_author and citation_date', () => {
      const doc = makeDoc(`<html><head>
        <meta name="citation_author" content="Dr. Citation">
        <meta name="citation_date" content="2025-04-10">
      </head><body></body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('Dr. Citation');
      expect(result.publishedTime).toBe('2025-04-10');
    });

    it('extracts from dc.creator and dc.date', () => {
      const doc = makeDoc(`<html><head>
        <meta name="dc.creator" content="Dublin Core Author">
        <meta name="dc.date" content="2025-02-28">
      </head><body></body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('Dublin Core Author');
      expect(result.publishedTime).toBe('2025-02-28');
    });
  });

  describe('microdata fallback', () => {
    it('extracts from itemprop attributes', () => {
      const doc = makeDoc(`<html><head></head><body>
        <span itemprop="author">Microdata Author</span>
        <time itemprop="datePublished" datetime="2025-07-04">July 4, 2025</time>
      </body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('Microdata Author');
      expect(result.publishedTime).toBe('2025-07-04');
    });
  });

  describe('<time> element fallback', () => {
    it('extracts date from time element', () => {
      const doc = makeDoc(`<html><head></head><body>
        <time datetime="2025-09-15">September 15, 2025</time>
      </body></html>`);
      const result = extractMetadata(doc);
      expect(result.publishedTime).toBe('2025-09-15');
      expect(result.author).toBeNull();
    });
  });

  describe('priority ordering', () => {
    it('JSON-LD takes priority over OG', () => {
      const doc = makeDoc(`<html><head>
        <script type="application/ld+json">{
          "@type": "Article",
          "datePublished": "2025-01-01",
          "author": "JSON-LD Author"
        }</script>
        <meta property="article:published_time" content="2025-12-31">
        <meta property="article:author" content="OG Author">
      </head><body></body></html>`);
      const result = extractMetadata(doc);
      expect(result.author).toBe('JSON-LD Author');
      expect(result.publishedTime).toBe('2025-01-01');
    });
  });

  describe('no metadata', () => {
    it('returns nulls when no metadata found', () => {
      const doc = makeDoc('<html><head></head><body><p>Hello world</p></body></html>');
      const result = extractMetadata(doc);
      expect(result.author).toBeNull();
      expect(result.publishedTime).toBeNull();
    });
  });
});
