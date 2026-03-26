/**
 * Background script for Save to Memex.
 * Handles extension install and privileged image downloading (bypasses CORS via host_permissions).
 */

browser.runtime.onInstalled.addListener(() => {
  console.log('Save to Memex extension installed.');
});

interface DownloadImageMessage {
  action: 'downloadImage';
  url: string;
}

interface DownloadImageResponse {
  ok: boolean;
  base64?: string;
  contentType?: string;
}

/** Relay a fetch request from an extension page (popup/options) through the
 *  background script, which has unconditional cross-origin access via
 *  host_permissions. This avoids CORS preflight issues with custom headers
 *  like X-API-Key that Firefox blocks from extension page contexts. */
interface ProxyFetchMessage {
  action: 'proxyFetch';
  url: string;
  init?: { method?: string; headers?: Record<string, string>; body?: string };
}

interface ProxyFetchResponse {
  ok: boolean;
  status: number;
  statusText: string;
  body: string;
}

browser.runtime.onMessage.addListener(
  (
    message: unknown,
  ): Promise<DownloadImageResponse> | Promise<ProxyFetchResponse> | undefined => {
    const msg = message as ProxyFetchMessage | DownloadImageMessage;

    if (msg.action === 'proxyFetch') {
      const pfMsg = msg as ProxyFetchMessage;
      return (async (): Promise<ProxyFetchResponse> => {
        try {
          console.log('[memex:bg] proxyFetch →', pfMsg.url);
          const controller = new AbortController();
          const timeoutId = setTimeout(() => controller.abort(), 15_000);
          const resp = await fetch(pfMsg.url, {
            ...pfMsg.init,
            signal: controller.signal,
          });
          clearTimeout(timeoutId);
          const body = await resp.text();
          console.log('[memex:bg] proxyFetch ←', resp.status, resp.statusText);
          return { ok: resp.ok, status: resp.status, statusText: resp.statusText, body };
        } catch (err) {
          console.error('[memex:bg] proxyFetch error:', err);
          return { ok: false, status: 0, statusText: 'Network error', body: '' };
        }
      })();
    }

    if (msg.action !== 'downloadImage') return undefined;

    return (async (): Promise<DownloadImageResponse> => {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 15_000);
        const resp = await fetch(msg.url, { signal: controller.signal });
        clearTimeout(timeoutId);

        if (!resp.ok) return { ok: false };
        const contentType = resp.headers.get('content-type') ?? '';
        if (!contentType.startsWith('image/')) return { ok: false };

        const buf = await resp.arrayBuffer();
        // Skip tracking pixels (< 200 B) and excessively large images (> 10 MB)
        if (buf.byteLength < 200 || buf.byteLength > 10 * 1024 * 1024) return { ok: false };

        const bytes = new Uint8Array(buf);
        let binary = '';
        // Process in chunks to avoid max call-stack with String.fromCharCode spread
        for (let i = 0; i < bytes.length; i += 8192) {
          const chunk = bytes.subarray(i, i + 8192);
          for (const byte of chunk) {
            binary += String.fromCharCode(byte);
          }
        }
        return { ok: true, base64: btoa(binary), contentType };
      } catch {
        return { ok: false };
      }
    })();
  },
);
