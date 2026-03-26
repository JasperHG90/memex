/**
 * Options page: configure Memex server URL and API key.
 */

import { saveApiKey, loadApiKey } from '../lib/key-store';

const serverUrlEl = document.getElementById('server-url') as HTMLInputElement;
const apiKeyEl = document.getElementById('api-key') as HTMLInputElement;
const rememberKeyEl = document.getElementById('remember-key') as HTMLInputElement;
const saveBtn = document.getElementById('save-btn') as HTMLButtonElement;
const testBtn = document.getElementById('test-btn') as HTMLButtonElement;
const toggleKeyBtn = document.getElementById('toggle-key') as HTMLButtonElement;
const connectionStatus = document.getElementById('connection-status')!;
const statusEl = document.getElementById('status')!;

// Show/hide API key toggle
toggleKeyBtn.addEventListener('click', () => {
  const isPassword = apiKeyEl.type === 'password';
  apiKeyEl.type = isPassword ? 'text' : 'password';
  toggleKeyBtn.textContent = isPassword ? 'Hide' : 'Show';
});

// Load saved settings
Promise.all([
  browser.storage.local.get({ memexServerUrl: 'http://localhost:8000' }),
  loadApiKey(),
]).then(([urlResult, keyResult]) => {
  serverUrlEl.value = (urlResult as Record<string, string>).memexServerUrl;
  apiKeyEl.value = keyResult.apiKey;
  rememberKeyEl.checked = keyResult.remember;
});

// Save settings
saveBtn.addEventListener('click', async () => {
  const serverUrl = serverUrlEl.value.trim() || 'http://localhost:8000';
  const apiKey = apiKeyEl.value.trim();
  const remember = rememberKeyEl.checked;

  await browser.storage.local.set({ memexServerUrl: serverUrl });
  await saveApiKey(apiKey, remember);

  statusEl.textContent = 'Settings saved.';
  statusEl.className = 'success';
  setTimeout(() => {
    statusEl.textContent = '';
    statusEl.className = '';
  }, 2000);
});

/**
 * Test connection: try direct fetch first (works when server CORS allows
 * moz-extension:// origins), fall back to background script proxy.
 */
async function testConnection(
  url: string,
  headers: Record<string, string>,
): Promise<{ ok: boolean; status: number; via: string }> {
  // Strategy 1: direct fetch (preferred — simpler, works if CORS is configured)
  try {
    console.log('[memex] Trying direct fetch to', url);
    const resp = await fetch(url, { headers });
    console.log('[memex] Direct fetch succeeded:', resp.status);
    return { ok: resp.ok, status: resp.status, via: 'direct' };
  } catch (err) {
    console.warn('[memex] Direct fetch failed:', err);
  }

  // Strategy 2: background script proxy (for environments where CORS blocks direct)
  try {
    console.log('[memex] Trying background script proxy...');
    const resp = (await browser.runtime.sendMessage({
      action: 'proxyFetch',
      url,
      init: { headers },
    })) as { ok: boolean; status: number; statusText: string } | undefined;

    if (resp && typeof resp.ok === 'boolean') {
      console.log('[memex] Proxy fetch succeeded:', resp.status);
      return { ok: resp.ok, status: resp.status, via: 'proxy' };
    }
    console.warn('[memex] Proxy returned unexpected response:', resp);
  } catch (err) {
    console.error('[memex] Proxy fetch failed:', err);
  }

  return { ok: false, status: 0, via: 'none' };
}

// Test connection
testBtn.addEventListener('click', async () => {
  const serverUrl = (serverUrlEl.value.trim() || 'http://localhost:8000').replace(/\/$/, '');
  const apiKey = apiKeyEl.value.trim();

  connectionStatus.className = 'indicator testing';
  connectionStatus.textContent = '';
  statusEl.textContent = 'Testing...';
  statusEl.className = '';

  const resp = await testConnection(
    `${serverUrl}/api/v1/vaults`,
    apiKey ? { 'X-API-Key': apiKey } : {},
  );

  if (resp.ok) {
    connectionStatus.className = 'indicator connected';
    statusEl.textContent = 'Connected!';
    statusEl.className = 'success';
  } else if (resp.status > 0) {
    connectionStatus.className = 'indicator failed';
    statusEl.textContent = `Server responded with ${resp.status}`;
    statusEl.className = 'error';
  } else {
    connectionStatus.className = 'indicator failed';
    statusEl.textContent = 'Could not connect to server.';
    statusEl.className = 'error';
  }
});
