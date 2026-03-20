# Save to Memex — Firefox Extension

Browser extension that captures articles and PDFs directly from Firefox and saves them to your Memex knowledge base. Bypasses the limitations of server-side scraping (JS-heavy pages, bot detection, paywalls) by extracting content from the already-rendered page.

## How it works

1. Navigate to any article or PDF and click the extension icon
2. **Articles**: content is extracted via [Readability.js](https://github.com/mozilla/readability) and converted to markdown via [Turndown](https://github.com/mixmark-io/turndown)
3. **PDFs**: the raw PDF is uploaded directly to the server for server-side parsing
4. Edit the title, pick a vault, optionally add tags/description
5. Click **Save** — the note is sent to your Memex server in the background

## Install from GitHub Release (recommended)

1. Go to [Releases](../../releases) and download the latest `.xpi` file
2. In Firefox, drag the `.xpi` onto any Firefox window
3. Click **Add** when prompted

> **Note:** If only an unsigned `.zip` is available, you need Firefox Developer Edition or Nightly with `xpinstall.signatures.required` set to `false` in `about:config`.

## Install for development

```bash
cd packages/firefox-extension
npm install
npm run build
```

Then load the extension in Firefox:

1. Open `about:debugging#/runtime/this-firefox`
2. Click **Load Temporary Add-on...**
3. Select `packages/firefox-extension/manifest.json` (at the project root, not in any subfolder)

## Configure

Click the extension icon → **Settings**, or go to `about:addons` → Save to Memex → Preferences:

| Setting | Default | Description |
|---------|---------|-------------|
| Server URL | `http://localhost:8000` | Your Memex server address |
| API Key | *(empty)* | Required if your server has auth enabled |

Use the **Test Connection** button to verify connectivity.

## Scripts

| Command | Description |
|---------|-------------|
| `npm run build` | Compile TypeScript + copy static assets |
| `npm run dev` | Build and open Firefox with the extension loaded |
| `npm test` | Run unit tests (vitest) |
| `npm run test:e2e` | Run Playwright end-to-end tests |
| `npm run test:all` | Run all tests (unit + e2e) |
| `npm run typecheck` | TypeScript type checking |
| `npm run lint` | Validate extension with `web-ext lint` |
| `npm run package` | Build and create distributable `.zip` in `dist/` |

## CI/CD

CI runs automatically on PRs and pushes to `main` when files in `packages/firefox-extension/` change. It runs typecheck, build, lint, unit tests, and Playwright e2e tests.

### Releasing a new version

1. Bump `version` in `manifest.json` and `package.json`
2. Tag and push:
   ```bash
   git tag firefox-extension-v0.1.0
   git push origin firefox-extension-v0.1.0
   ```
3. GitHub Actions builds, tests, signs (if AMO credentials are configured), and creates a GitHub Release with the `.xpi`

### Setting up AMO signing

To produce signed `.xpi` files that install on regular Firefox (not just Developer Edition):

1. Get API credentials from [AMO Developer Hub](https://addons.mozilla.org/developers/addon/api/key/)
2. Add these as GitHub repository secrets:
   - `AMO_JWT_ISSUER` — your API key
   - `AMO_JWT_SECRET` — your API secret
3. The release workflow will automatically sign the extension via `web-ext sign --channel unlisted`

Without AMO credentials, the workflow produces an unsigned `.zip` instead.

## Project structure

```
src/
├── types.ts              — Shared types and global declarations
├── global.d.ts           — browser API type shim
├── background.ts         — Extension lifecycle (install handler)
├── lib/
│   ├── memex-api.ts      — Memex REST API client (notes + file uploads)
│   └── frontmatter.ts    — YAML frontmatter builder
├── popup/
│   ├── popup.ts/html/css — Main save UI (article extraction + PDF upload)
└── options/
    └── options.ts/html/css — Settings page with connection test
tests/
├── memex-api.test.ts     — API client tests (vitest)
├── frontmatter.test.ts   — Frontmatter builder tests (vitest)
└── popup.spec.ts         — UI + save flow tests (Playwright)
```
