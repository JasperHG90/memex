/**
 * Popup logic: extract article from active tab, let user edit metadata, save to Memex.
 */

import { Readability } from '@mozilla/readability';
import TurndownService from 'turndown';
import type { ExtractResult } from '../types';
import { fetchVaults, saveNote, uploadFile, canonicalizeUrl } from '../lib/memex-api';
import { loadApiKey } from '../lib/key-store';
import { buildNoteContent } from '../lib/frontmatter';
import { extractArticleImages } from '../lib/images';
import { extractMetadata } from '../lib/metadata';

const titleEl = document.getElementById('title') as HTMLInputElement;
const userNotesEl = document.getElementById('user-notes') as HTMLTextAreaElement;
const vaultEl = document.getElementById('vault') as HTMLSelectElement;
const tagsEl = document.getElementById('tags') as HTMLInputElement;
const authorEl = document.getElementById('author') as HTMLInputElement;
const publishDateEl = document.getElementById('publish-date') as HTMLInputElement;
const urlPreviewEl = document.getElementById('url-preview')!;
const saveBtn = document.getElementById('save-btn') as HTMLButtonElement;
const statusEl = document.getElementById('status')!;
const settingsLink = document.getElementById('open-settings')!;

let extractedData: ExtractResult | null = null;
let pdfMode: { url: string; filename: string } | null = null;

async function isPdf(url: string): Promise<boolean> {
  try {
    if (new URL(url).pathname.toLowerCase().endsWith('.pdf')) return true;
  } catch {
    return false;
  }
  // Fallback: HEAD request to check content-type (handles arxiv /pdf/XXXX URLs)
  try {
    const resp = await fetch(url, { method: 'HEAD' });
    return (resp.headers.get('content-type') ?? '').includes('application/pdf');
  } catch {
    return false;
  }
}

// --- Init ---

async function init(): Promise<void> {
  const settings = await loadSettings();
  extractArticle();
  loadVaults(settings);
}
init();

// --- Extract article from active tab ---

async function extractArticle(): Promise<void> {
  try {
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) {
      showStatus('No active tab found.', 'error');
      return;
    }

    const tabUrl = tab.url ?? '';
    urlPreviewEl.textContent = tabUrl;

    // Use tab.title as immediate fallback
    titleEl.value = tab.title ?? '';

    // PDF mode: skip extraction, will upload raw PDF on save
    if (await isPdf(tabUrl)) {
      const filename = new URL(tabUrl).pathname.split('/').pop() || 'document.pdf';
      pdfMode = { url: tabUrl, filename };
      extractedData = {
        title: tab.title ?? filename,
        url: tabUrl,
        hostname: new URL(tabUrl).hostname,
      };
      saveBtn.disabled = false;
      return;
    }

    // Resolve blob: image URLs in-page and extract their data separately.
    // Blob URLs are ephemeral and page-scoped — they can't be fetched from the
    // background script.  We also can't embed data: URLs in the DOM because
    // Readability strips them.  Instead we return the image data as a side-channel
    // keyed by alt text, and merge them after Readability runs.
    let blobImages: Record<string, string> = {};
    try {
      const blobResults = await browser.scripting.executeScript({
        target: { tabId: tab.id },
        func: async () => {
          const imgs = document.querySelectorAll<HTMLImageElement>('img[src^="blob:"]');
          const TIMEOUT = 10_000;
          const resolved: Record<string, string> = {};
          // Track keys to disambiguate duplicate alt texts
          const altCounts: Record<string, number> = {};
          await Promise.all(
            Array.from(imgs).map(async (img) => {
              try {
                // Wait for the image to finish loading if it hasn't yet
                if (!img.complete) {
                  await Promise.race([
                    new Promise<void>((resolve) => {
                      img.addEventListener('load', () => resolve(), { once: true });
                      img.addEventListener('error', () => resolve(), { once: true });
                    }),
                    new Promise<void>((resolve) => setTimeout(resolve, TIMEOUT)),
                  ]);
                }

                // Try fetch+FileReader first (preserves original bytes).
                // Fall back to canvas if the blob has been revoked.
                let dataUrl: string | null = null;
                try {
                  const resp = await fetch(img.src);
                  const blob = await resp.blob();
                  dataUrl = await new Promise<string>((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result as string);
                    reader.onerror = () => reject(reader.error);
                    reader.readAsDataURL(blob);
                  });
                } catch {
                  // Blob may have been revoked — fall back to canvas
                  if (img.naturalWidth > 0 && img.naturalHeight > 0) {
                    const canvas = document.createElement('canvas');
                    canvas.width = img.naturalWidth;
                    canvas.height = img.naturalHeight;
                    const ctx = canvas.getContext('2d');
                    if (ctx) {
                      ctx.drawImage(img, 0, 0);
                      try {
                        dataUrl = canvas.toDataURL('image/png');
                      } catch {
                        // Canvas tainted by CORS — give up on this image
                      }
                    }
                  }
                }

                if (!dataUrl) return;

                // Build a unique key: prefer alt text, deduplicate with suffix
                const baseKey = img.alt || img.dataset.fileid || 'blob-image';
                const count = altCounts[baseKey] ?? 0;
                altCounts[baseKey] = count + 1;
                const key = count === 0 ? baseKey : `${baseKey}__${count}`;
                resolved[key] = dataUrl;
              } catch {
                // Failed to convert — skip this image
              }
            }),
          );
          return resolved;
        },
      });
      blobImages = (blobResults?.[0]?.result as Record<string, string>) ?? {};
    } catch {
      // Non-fatal — blob images will just be missing
    }

    // Grab page HTML via executeScript
    const results = await browser.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => document.documentElement.outerHTML,
    });

    const html = results?.[0]?.result as string | undefined;
    if (!html) {
      showStatus('Could not read page content.', 'error');
      saveBtn.disabled = false;
      extractedData = {
        title: tab.title ?? '',
        markdown: '',
        url: tabUrl,
        hostname: new URL(tabUrl).hostname,
      };
      return;
    }

    // Parse with Readability + Turndown in popup context
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, 'text/html');
    // Set the base URL so relative links resolve
    const base = doc.createElement('base');
    base.href = tab.url ?? '';
    doc.head.prepend(base);

    // Extract metadata from a separate copy (Readability mutates the DOM)
    const metadataDoc = parser.parseFromString(html, 'text/html');
    const metadata = extractMetadata(metadataDoc);

    const reader = new Readability(doc);
    const article = reader.parse();

    if (!article) {
      // Readability couldn't extract — fall back to tab title
      extractedData = {
        title: tab.title ?? '',
        markdown: '',
        url: tab.url ?? '',
        hostname: new URL(tab.url ?? '').hostname,
      };
      showStatus('Could not extract article. You can still save with just the title.', 'error');
      saveBtn.disabled = false;
      return;
    }

    // Extract and download article images (non-fatal — article saves regardless)
    let images: Record<string, string> = {};
    let articleMarkdown: string;
    try {
      const downloader = async (url: string) => {
        const resp = await browser.runtime.sendMessage({ action: 'downloadImage', url });
        return resp as { ok: boolean; base64?: string; contentType?: string };
      };
      const result = await extractArticleImages(
        article.content, tab.url ?? '', downloader, 30_000, blobImages,
      );
      images = result.images;
      articleMarkdown = result.markdown;
    } catch {
      // Image extraction failed — fall back to plain markdown with original URLs
      const turndown = new TurndownService({ headingStyle: 'atx', codeBlockStyle: 'fenced' });
      articleMarkdown = turndown.turndown(article.content);
    }

    const mergedAuthor = metadata.author || article.byline || '';
    const mergedPublishedTime = metadata.publishedTime || article.publishedTime || '';

    extractedData = {
      title: article.title || tab.title || '',
      markdown: articleMarkdown,
      excerpt: article.excerpt || '',
      byline: mergedAuthor,
      siteName: article.siteName || '',
      publishedTime: mergedPublishedTime,
      url: tab.url ?? '',
      hostname: new URL(tab.url ?? '').hostname,
      images: Object.keys(images).length > 0 ? images : undefined,
    };

    titleEl.value = extractedData.title ?? '';
    authorEl.value = mergedAuthor;
    publishDateEl.value = mergedPublishedTime;
    saveBtn.disabled = false;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    showStatus('Extraction failed: ' + msg, 'error');
  }
}

// --- Load vaults ---

async function loadVaults(settings: { serverUrl: string; apiKey: string }): Promise<void> {
  try {
    const vaults = await fetchVaults(settings.serverUrl, settings.apiKey);
    vaultEl.innerHTML = '';

    for (const vault of vaults) {
      const opt = document.createElement('option');
      opt.value = vault.id;
      opt.textContent = vault.name + (vault.is_active ? ' (active)' : '');
      if (vault.is_active) opt.selected = true;
      vaultEl.appendChild(opt);
    }

    if (vaults.length === 0) {
      vaultEl.innerHTML = '<option value="">No vaults found</option>';
    }
  } catch {
    vaultEl.innerHTML = '<option value="">Failed to load vaults</option>';
    showStatus('Could not connect to Memex server. Check settings.', 'error');
  }
}

// --- Save ---

saveBtn.addEventListener('click', async () => {
  if (!extractedData) return;

  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving...';

  try {
    const settings = await loadSettings();
    const tags = tagsEl.value
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);

    if (pdfMode) {
      // PDF: fetch the raw file and upload it
      const pdfResp = await fetch(pdfMode.url);
      const pdfBytes = await pdfResp.arrayBuffer();
      await uploadFile(settings.serverUrl, settings.apiKey, {
        bytes: pdfBytes,
        filename: pdfMode.filename,
        contentType: 'application/pdf',
        vaultId: vaultEl.value || undefined,
        noteKey: canonicalizeUrl(pdfMode.url),
        userNotes: userNotesEl.value || undefined,
      });
    } else {
      const fullContent = extractedData.markdown
        ? buildNoteContent({
            url: extractedData.url ?? '',
            hostname: extractedData.hostname ?? '',
            byline: authorEl.value || extractedData.byline,
            siteName: extractedData.siteName,
            publishedTime: publishDateEl.value || extractedData.publishedTime,
            markdown: extractedData.markdown,
          })
        : '';

      await saveNote(settings.serverUrl, settings.apiKey, {
        name: titleEl.value || extractedData.title || 'Untitled',
        description: '',
        content: fullContent,
        tags,
        vaultId: vaultEl.value || undefined,
        background: true,
        files: extractedData.images,
        noteKey: extractedData.url ? canonicalizeUrl(extractedData.url) : undefined,
        userNotes: userNotesEl.value || undefined,
      });
    }

    saveBtn.textContent = 'Saved!';
    saveBtn.classList.add('saved');
    setTimeout(() => window.close(), 800);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    showStatus('Save failed: ' + msg, 'error');
    saveBtn.textContent = 'Save';
    saveBtn.disabled = false;
  }
});

// --- Settings ---

settingsLink.addEventListener('click', (e) => {
  e.preventDefault();
  browser.runtime.openOptionsPage();
});

// --- Helpers ---

async function loadSettings(): Promise<{ serverUrl: string; apiKey: string }> {
  const [urlResult, keyResult] = await Promise.all([
    browser.storage.local.get({ memexServerUrl: 'http://localhost:8000' }),
    loadApiKey(),
  ]);
  return {
    serverUrl: ((urlResult as Record<string, string>).memexServerUrl).replace(/\/$/, ''),
    apiKey: keyResult.apiKey,
  };
}

function showStatus(message: string, type: 'success' | 'error' | 'loading'): void {
  statusEl.textContent = message;
  statusEl.className = type;
}
