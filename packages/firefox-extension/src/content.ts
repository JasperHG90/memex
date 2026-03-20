/**
 * Content script: extracts article content via Readability.js and converts to Markdown.
 * Runs immediately on injection — stores result on globalThis for the popup to read.
 */

import { Readability } from '@mozilla/readability';
import TurndownService from 'turndown';
import type { ExtractResult } from './types';

try {
  const documentClone = document.cloneNode(true) as Document;
  const reader = new Readability(documentClone);
  const article = reader.parse();

  if (!article) {
    (globalThis as any).__memexExtract = {
      error: 'Could not extract article from this page. Is it an article?',
    } satisfies ExtractResult;
  } else {
    const turndown = new TurndownService({
      headingStyle: 'atx',
      codeBlockStyle: 'fenced',
    });
    const markdown = turndown.turndown(article.content);

    (globalThis as any).__memexExtract = {
      title: article.title || document.title,
      markdown,
      excerpt: article.excerpt || '',
      byline: article.byline || '',
      siteName: article.siteName || '',
      url: location.href,
      hostname: location.hostname,
    } satisfies ExtractResult;
  }
} catch (err) {
  (globalThis as any).__memexExtract = {
    error: err instanceof Error ? err.message : String(err),
  } satisfies ExtractResult;
}
