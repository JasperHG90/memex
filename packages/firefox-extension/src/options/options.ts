/**
 * Options page: configure Memex server URL and API key.
 */

import type { Settings } from '../types';

const serverUrlEl = document.getElementById('server-url') as HTMLInputElement;
const apiKeyEl = document.getElementById('api-key') as HTMLInputElement;
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
browser.storage.local
  .get({ memexServerUrl: 'http://localhost:8000', memexApiKey: '' })
  .then((result) => {
    const settings = result as unknown as Settings;
    serverUrlEl.value = settings.memexServerUrl;
    apiKeyEl.value = settings.memexApiKey;
  });

// Save settings
saveBtn.addEventListener('click', async () => {
  const serverUrl = serverUrlEl.value.trim() || 'http://localhost:8000';
  const apiKey = apiKeyEl.value.trim();

  await browser.storage.local.set({
    memexServerUrl: serverUrl,
    memexApiKey: apiKey,
  });

  statusEl.textContent = 'Settings saved.';
  statusEl.className = 'success';
  setTimeout(() => {
    statusEl.textContent = '';
    statusEl.className = '';
  }, 2000);
});

// Test connection
testBtn.addEventListener('click', async () => {
  const serverUrl = (serverUrlEl.value.trim() || 'http://localhost:8000').replace(/\/$/, '');
  const apiKey = apiKeyEl.value.trim();

  connectionStatus.className = 'indicator testing';
  connectionStatus.textContent = '';
  statusEl.textContent = 'Testing...';
  statusEl.className = '';

  try {
    const resp = (await browser.runtime.sendMessage({
      action: 'proxyFetch',
      url: `${serverUrl}/api/v1/vaults`,
      init: { headers: apiKey ? { 'X-API-Key': apiKey } : {} },
    })) as { ok: boolean; status: number };

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
  } catch {
    connectionStatus.className = 'indicator failed';
    statusEl.textContent = 'Could not connect to server.';
    statusEl.className = 'error';
  }
});
