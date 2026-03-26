import { test, expect, type Page } from '@playwright/test';

/**
 * Build a minimal article HTML page that Readability can parse.
 * Readability needs certain signals to detect an article.
 */
function buildArticleHTML(opts: {
  title?: string;
  content?: string;
  byline?: string;
  jsonLd?: string;
} = {}) {
  const title = opts.title ?? 'Why Neural Networks Are Changing Everything';
  const content = opts.content ?? '<p>A deep dive into modern AI architectures and their impact.</p>'.repeat(5);
  const byline = opts.byline ?? 'Jane Doe';
  const jsonLd = opts.jsonLd ?? '';
  return `<!DOCTYPE html><html><head><title>${title}</title>${jsonLd}</head><body>
    <article>
      <h1>${title}</h1>
      <p class="author">${byline}</p>
      ${content}
    </article>
  </body></html>`;
}

/** Inject browser API mocks into popup HTML via route interception. */
async function setupPopupMocks(
  page: Page,
  overrides: {
    articleHTML?: string | null;
    tabTitle?: string;
    tabUrl?: string;
    vaultsNDJSON?: string;
    saveOk?: boolean;
    saveResponse?: object;
    storageSettings?: object;
  } = {},
) {
  const tabTitle = overrides.tabTitle ?? 'Why Neural Networks Are Changing Everything';
  const tabUrl = overrides.tabUrl ?? 'https://medium.com/example-article';
  const articleHTML = overrides.articleHTML ?? buildArticleHTML();

  const vaultsNDJSON =
    overrides.vaultsNDJSON ??
    [
      '{"id":"aaa-111","name":"memex","description":"Default vault","is_active":true,"note_count":22,"last_note_added_at":null}',
      '{"id":"bbb-222","name":"AI","description":"AI articles","is_active":false,"note_count":3,"last_note_added_at":null}',
      '{"id":"ccc-333","name":"rituals","description":"Client notes","is_active":false,"note_count":178,"last_note_added_at":null}',
    ].join('\n');

  const saveOk = overrides.saveOk ?? true;
  const saveResponse = overrides.saveResponse ?? { note_id: 'test-note-123' };
  const storageSettings = overrides.storageSettings ?? {
    memexServerUrl: 'http://localhost:8000',
    memexApiKey: '',
  };

  const mockScript = `
    <script>
      window.browser = {
        tabs: {
          query: async function() {
            return [{ id: 1, title: ${JSON.stringify(tabTitle)}, url: ${JSON.stringify(tabUrl)} }];
          },
        },
        scripting: {
          executeScript: async function(opts) {
            if (opts.func) {
              // The popup calls func: () => document.documentElement.outerHTML
              // We return our mock article HTML instead
              return [{ result: ${JSON.stringify(articleHTML)} }];
            }
            return [{}];
          },
        },
        storage: {
          local: {
            _data: Object.assign({}, ${JSON.stringify(storageSettings)}),
            get: async function(defaults) {
              return Object.assign({}, defaults, this._data);
            },
            set: async function(items) { Object.assign(this._data, items); },
            remove: async function(keys) {
              for (var k of keys) delete this._data[k];
            },
          },
          session: {
            _data: {},
            get: async function(defaults) {
              return Object.assign({}, defaults, this._data);
            },
            set: async function(items) { Object.assign(this._data, items); },
            remove: async function(keys) {
              for (var k of keys) delete this._data[k];
            },
          },
        },
        runtime: {
          openOptionsPage: function() {},
          sendMessage: async function(msg) {
            if (msg && msg.action === 'downloadImage') return { ok: false };
            if (msg && msg.action === 'proxyFetch') {
              var url = msg.url || '';
              if (url.indexOf('/api/v1/vaults') !== -1) {
                return { ok: true, status: 200, statusText: 'OK', body: ${JSON.stringify(vaultsNDJSON)} };
              }
              if (url.indexOf('/api/v1/ingestions') !== -1) {
                if (msg.init && msg.init.body) window.__lastSaveBody = JSON.parse(msg.init.body);
                ${
                  saveOk
                    ? `return { ok: true, status: 200, statusText: 'OK', body: JSON.stringify(${JSON.stringify(saveResponse)}) };`
                    : `return { ok: false, status: 500, statusText: 'Internal Server Error', body: 'Internal Server Error' };`
                }
              }
              return { ok: false, status: 404, statusText: 'Not Found', body: '' };
            }
            return {};
          },
        },
      };

      window.__lastSaveBody = null;

      window.close = function() {};
    </script>
  `;

  await page.route('**/popup/popup.html', async (route) => {
    const response = await route.fetch();
    const html = await response.text();
    const modified = html.replace('</head>', mockScript + '</head>');
    await route.fulfill({ body: modified, contentType: 'text/html' });
  });
}

/** Inject browser mocks into options HTML. */
async function setupOptionsMocks(
  page: Page,
  opts: { settings?: object } = {},
) {
  const settings = opts.settings ?? {
    memexServerUrl: 'http://myserver:9000',
    memexApiKey: 'test-key-123',
  };

  const mockScript = `
    <script>
      window.__savedSettings = null;
      window.browser = {
        storage: {
          local: {
            _data: Object.assign({}, ${JSON.stringify(settings)}),
            get: async function(defaults) {
              return Object.assign({}, defaults, this._data);
            },
            set: async function(data) {
              Object.assign(this._data, data);
              window.__savedSettings = data;
            },
            remove: async function(keys) {
              for (var k of keys) delete this._data[k];
            },
          },
          session: {
            _data: {},
            get: async function(defaults) {
              return Object.assign({}, defaults, this._data);
            },
            set: async function(items) { Object.assign(this._data, items); },
            remove: async function(keys) {
              for (var k of keys) delete this._data[k];
            },
          },
        },
        runtime: {
          sendMessage: async function(msg) {
            if (msg && msg.action === 'proxyFetch') {
              return { ok: true, status: 200, statusText: 'OK', body: '{}' };
            }
            return {};
          },
        },
      };
    </script>
  `;

  await page.route('**/options/options.html', async (route) => {
    const response = await route.fetch();
    const html = await response.text();
    const modified = html.replace('</head>', mockScript + '</head>');
    await route.fulfill({ body: modified, contentType: 'text/html' });
  });
}

// --- Popup rendering tests ---

test.describe('popup rendering', () => {
  test('renders all form elements', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('heading', { name: 'Save to Memex' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Title' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: /Additional Notes/ })).toBeVisible();
    await expect(page.getByRole('combobox', { name: 'Vault' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: /Tags/ })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Settings' })).toBeVisible();
  });

  test('displays brain logo', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    const logo = page.locator('img.logo');
    await expect(logo).toBeVisible();
    await expect(logo).toHaveAttribute('src', /icon-48\.png/);
  });
});

// --- Article extraction tests ---

test.describe('article extraction', () => {
  test('populates title from extracted article', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('textbox', { name: 'Title' })).toHaveValue(
      'Why Neural Networks Are Changing Everything',
    );
  });

  test('leaves additional notes empty by default', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('textbox', { name: /Additional Notes/ })).toHaveValue('');
  });

  test('displays source URL', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    await expect(page.locator('#url-preview')).toHaveText('https://medium.com/example-article');
  });

  test('enables save button after successful extraction', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
  });

  test('populates author and publish date from metadata', async ({ page }) => {
    const htmlWithMeta = buildArticleHTML({
      jsonLd: '<meta property="article:author" content="Jane Doe"><meta property="article:published_time" content="2025-12-09T00:00:00Z">',
    });
    await setupPopupMocks(page, { articleHTML: htmlWithMeta });
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
    await expect(page.locator('#author')).toHaveValue('Jane Doe');
    await expect(page.locator('#publish-date')).toHaveValue('2025-12-09');
  });

  test('author and publish date fields are editable', async ({ page }) => {
    const htmlWithMeta = buildArticleHTML({
      jsonLd: '<meta property="article:author" content="Jane Doe"><meta property="article:published_time" content="2025-12-09T00:00:00Z">',
    });
    await setupPopupMocks(page, { articleHTML: htmlWithMeta });
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
    await page.locator('#author').fill('Custom Author');
    await expect(page.locator('#author')).toHaveValue('Custom Author');
  });

  test('falls back to tab title when Readability fails', async ({ page }) => {
    await setupPopupMocks(page, {
      articleHTML: '<html><head></head><body><p>x</p></body></html>',
      tabTitle: 'Simple Page - Firefox',
    });
    await page.goto('/popup/popup.html');

    // Should fall back to tab title and still enable save
    await expect(page.getByRole('textbox', { name: 'Title' })).toHaveValue('Simple Page - Firefox');
    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
  });
});

// --- Vault loading tests ---

test.describe('vault loading', () => {
  test('loads vaults into dropdown', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    const vault = page.getByRole('combobox', { name: 'Vault' });
    const options = vault.locator('option');
    await expect(options).toHaveCount(3);
    await expect(options.nth(0)).toHaveText('memex (active)');
    await expect(options.nth(1)).toHaveText('AI');
    await expect(options.nth(2)).toHaveText('rituals');
  });

  test('pre-selects the active vault', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('combobox', { name: 'Vault' })).toHaveValue('aaa-111');
  });

  test('shows error when vault fetch fails', async ({ page }) => {
    await page.route('**/popup/popup.html', async (route) => {
      const response = await route.fetch();
      const html = await response.text();
      const mock = `<script>
        window.browser = {
          tabs: {
            query: async function() { return [{ id: 1, title: 'T', url: 'https://example.com' }]; },
          },
          scripting: {
            executeScript: async function() {
              return [{ result: ${JSON.stringify(buildArticleHTML({ title: 'T' }))} }];
            },
          },
          storage: {
            local: {
              _data: {},
              get: async function(d) { return Object.assign({}, d, this._data); },
              set: async function(items) { Object.assign(this._data, items); },
              remove: async function(keys) { for (var k of keys) delete this._data[k]; },
            },
            session: {
              _data: {},
              get: async function(d) { return Object.assign({}, d, this._data); },
              set: async function(items) { Object.assign(this._data, items); },
              remove: async function(keys) { for (var k of keys) delete this._data[k]; },
            },
          },
          runtime: {
            openOptionsPage: function() {},
            sendMessage: async function(msg) {
              if (msg && msg.action === 'downloadImage') return { ok: false };
              if (msg && msg.action === 'proxyFetch') {
                return { ok: false, status: 0, statusText: 'Network error', body: '' };
              }
              return {};
            },
          },
        };
        window.close = function() {};
      </script>`;
      await route.fulfill({ body: html.replace('</head>', mock + '</head>'), contentType: 'text/html' });
    });
    await page.goto('/popup/popup.html');

    await expect(page.locator('#vault option')).toHaveText('Failed to load vaults');
  });
});

// --- Save flow tests ---

test.describe('save flow', () => {
  test('shows success message after saving', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
    await page.getByRole('button', { name: 'Save' }).click();

    await expect(page.getByRole('button', { name: 'Saved!' })).toBeVisible();
  });

  test('sends correct data to API', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
    await page.getByRole('button', { name: 'Save' }).click();
    await expect(page.getByRole('button', { name: 'Saved!' })).toBeVisible();

    const body = await page.evaluate(() => (window as any).__lastSaveBody);
    expect(body.name).toBe('Why Neural Networks Are Changing Everything');
    expect(body.vault_id).toBe('aaa-111');
    // Content should be base64-encoded and include frontmatter
    const decoded = atob(body.content);
    expect(decoded).toContain('source_url: https://medium.com/example-article');
  });

  test('allows editing title before saving', async ({ page }) => {
    await setupPopupMocks(page);
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
    await page.getByRole('textbox', { name: 'Title' }).fill('My Custom Title');
    await page.getByRole('button', { name: 'Save' }).click();
    await expect(page.getByRole('button', { name: 'Saved!' })).toBeVisible();

    const body = await page.evaluate(() => (window as any).__lastSaveBody);
    expect(body.name).toBe('My Custom Title');
  });

  test('uses edited author and date in saved frontmatter', async ({ page }) => {
    const htmlWithMeta = buildArticleHTML({
      jsonLd: '<meta property="article:author" content="Original Author"><meta property="article:published_time" content="2025-12-09T00:00:00Z">',
    });
    await setupPopupMocks(page, { articleHTML: htmlWithMeta });
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
    await page.locator('#author').fill('Edited Author');
    await page.locator('#publish-date').fill('2024-01-15');
    await page.getByRole('button', { name: 'Save' }).click();
    await expect(page.getByRole('button', { name: 'Saved!' })).toBeVisible();

    const body = await page.evaluate(() => (window as any).__lastSaveBody);
    const decoded = atob(body.content);
    expect(decoded).toContain('author: Edited Author');
    expect(decoded).toContain('publish_date: 2024-01-15');
  });

  test('shows error on save failure and re-enables button', async ({ page }) => {
    await setupPopupMocks(page, { saveOk: false });
    await page.goto('/popup/popup.html');

    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
    await page.getByRole('button', { name: 'Save' }).click();

    await expect(page.locator('#status')).toContainText('Save failed');
    await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
  });
});

// --- Options page tests ---

test.describe('options page', () => {
  test('renders settings form with saved values', async ({ page }) => {
    await setupOptionsMocks(page);
    await page.goto('/options/options.html');

    await expect(page.getByRole('heading', { name: /Settings/ })).toBeVisible();
    await expect(page.getByRole('textbox', { name: /Server URL/ })).toHaveValue('http://myserver:9000');
    // Legacy key is migrated to session storage by loadApiKey, still shown in the field
    await expect(page.locator('#api-key')).toHaveValue('test-key-123');
  });

  test('saves settings to storage', async ({ page }) => {
    await setupOptionsMocks(page, { settings: { memexServerUrl: 'http://localhost:8000', memexApiKey: '' } });
    await page.goto('/options/options.html');

    await page.getByRole('textbox', { name: /Server URL/ }).fill('http://newserver:3000');
    await page.locator('#api-key').fill('my-new-key');
    await page.getByRole('button', { name: 'Save Settings' }).click();

    await expect(page.locator('#status')).toHaveText('Settings saved.');

    // Server URL is saved to storage.local
    const localData = await page.evaluate(
      () => (window as any).browser.storage.local._data,
    );
    expect(localData.memexServerUrl).toBe('http://newserver:3000');

    // API key is stored in session storage (remember unchecked by default)
    const sessionData = await page.evaluate(
      () => (window as any).browser.storage.session._data,
    );
    expect(sessionData.memexApiKey).toBe('my-new-key');
  });

  test('has test connection button', async ({ page }) => {
    await setupOptionsMocks(page);
    await page.goto('/options/options.html');

    await expect(page.getByRole('button', { name: 'Test Connection' })).toBeVisible();
    await expect(page.locator('.indicator')).toBeVisible();
  });
});
