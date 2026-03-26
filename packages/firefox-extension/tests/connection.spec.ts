/**
 * Real network tests — makes actual HTTP requests to the running Memex server
 * from a Playwright browser context to diagnose CORS and connectivity issues.
 *
 * These tests require the server to be running at http://memex.localstack.
 */
import { test, expect } from '@playwright/test';

// Use both hostname and IP to diagnose DNS resolution issues
const SERVER = 'http://memex.localstack';
const SERVER_IP = 'http://192.168.2.46:8000';
const API_KEY = 'oK74SWmybcUgQaK1dUpB4xnHQyFGjUrjYGJth_W2deI';

test.describe('real server connectivity', () => {
  test('health endpoint via hostname', async ({ page }) => {
    await page.goto('http://localhost:8889/options/options.html');
    const result = await page.evaluate(async (server) => {
      try {
        const r = await fetch(`${server}/api/v1/health`);
        return { ok: r.ok, status: r.status, body: await r.text() };
      } catch (e) {
        return { error: String(e) };
      }
    }, SERVER);
    console.log('Test 1a (health via hostname):', JSON.stringify(result));
  });

  test('health endpoint via IP', async ({ page }) => {
    await page.goto('http://localhost:8889/options/options.html');
    const result = await page.evaluate(async (server) => {
      try {
        const r = await fetch(`${server}/api/v1/health`);
        return { ok: r.ok, status: r.status, body: await r.text() };
      } catch (e) {
        return { error: String(e) };
      }
    }, SERVER_IP);
    console.log('Test 1b (health via IP):', JSON.stringify(result));
  });

  test('vaults endpoint WITHOUT API key — simple GET, should get 401', async ({ page }) => {
    await page.goto('about:blank');
    const result = await page.evaluate(async (server) => {
      try {
        const r = await fetch(`${server}/api/v1/vaults`);
        return { ok: r.ok, status: r.status, body: await r.text() };
      } catch (e) {
        return { error: String(e) };
      }
    }, SERVER);
    console.log('Test 2 (vaults, no auth):', JSON.stringify(result));
    // Should reach the server (401 = auth required), NOT a CORS error
    expect(result).toHaveProperty('status', 401);
  });

  test('vaults endpoint WITH X-API-Key header — CORS preflight test', async ({ page }) => {
    await page.goto('about:blank');
    const result = await page.evaluate(async ({ server, apiKey }) => {
      try {
        const r = await fetch(`${server}/api/v1/vaults`, {
          headers: { 'X-API-Key': apiKey },
        });
        return { ok: r.ok, status: r.status, body: await r.text() };
      } catch (e) {
        return { error: String(e) };
      }
    }, { server: SERVER, apiKey: API_KEY });
    console.log('Test 3 (vaults, with API key):', JSON.stringify(result));
    // If this has 'error' property, CORS is blocking the request
    // If status is 200, CORS is fine
    if ('error' in result) {
      console.log('>>> CORS IS BLOCKING the request with X-API-Key header');
    } else {
      console.log('>>> CORS is fine, status:', result.status);
    }
  });

  test('OPTIONS preflight from browser page', async ({ page }) => {
    await page.goto('about:blank');
    const result = await page.evaluate(async (server) => {
      try {
        const r = await fetch(`${server}/api/v1/vaults`, {
          method: 'OPTIONS',
          headers: {
            'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'X-API-Key',
          },
        });
        const headers: Record<string, string> = {};
        r.headers.forEach((v, k) => { headers[k] = v; });
        return { ok: r.ok, status: r.status, headers };
      } catch (e) {
        return { error: String(e) };
      }
    }, SERVER);
    console.log('Test 4 (manual OPTIONS):', JSON.stringify(result));
  });

  test('fetch from extension options page context', async ({ page }) => {
    // Load the actual built options page and see what happens
    // when we make a real fetch from it (bypassing the sendMessage proxy)
    await page.route('**/options/options.html', async (route) => {
      const response = await route.fetch();
      const html = await response.text();
      // Inject a test script that does a direct fetch (no browser.runtime.sendMessage)
      const testScript = `
        <script>
          window.browser = {
            storage: {
              local: {
                _data: { memexServerUrl: '${SERVER}' },
                get: async function(d) { return Object.assign({}, d, this._data); },
                set: async function(items) { Object.assign(this._data, items); },
                remove: async function(keys) { for (var k of keys) delete this._data[k]; },
              },
              session: {
                _data: { memexApiKey: '${API_KEY}' },
                get: async function(d) { return Object.assign({}, d, this._data); },
                set: async function(items) { Object.assign(this._data, items); },
                remove: async function(keys) { for (var k of keys) delete this._data[k]; },
              },
            },
            runtime: {
              sendMessage: async function(msg) {
                console.log('[test] sendMessage called with:', JSON.stringify(msg));
                if (msg && msg.action === 'proxyFetch') {
                  // Actually do the fetch right here (simulating what background.js does)
                  try {
                    var r = await fetch(msg.url, msg.init || {});
                    var body = await r.text();
                    console.log('[test] fetch succeeded:', r.status);
                    return { ok: r.ok, status: r.status, statusText: r.statusText, body: body };
                  } catch (e) {
                    console.error('[test] fetch failed:', e);
                    return { ok: false, status: 0, statusText: 'Network error', body: '' };
                  }
                }
                return {};
              },
            },
          };
        </script>
      `;
      const modified = html.replace('</head>', testScript + '</head>');
      await route.fulfill({ body: modified, contentType: 'text/html' });
    });

    await page.goto('/options/options.html');

    // Wait for settings to load
    await expect(page.locator('#api-key')).toHaveValue(API_KEY, { timeout: 5000 });

    // Click Test Connection
    await page.getByRole('button', { name: 'Test Connection' }).click();

    // Wait for result
    await page.waitForTimeout(3000);

    const statusText = await page.locator('#status').textContent();
    console.log('Test 5 (options page with inline fetch):', statusText);

    // Capture console messages
    const messages: string[] = [];
    page.on('console', (msg) => messages.push(msg.text()));
  });
});
