# How to Use the Save to Memex Firefox Extension

This guide shows you how to install, configure, and use the Firefox extension for one-click capture of articles, PDFs, and web pages into Memex.

## Prerequisites

* A running Memex server (`memex server start -d`)
* Firefox (or Firefox Developer Edition for unsigned builds)

## Why Use the Extension?

The extension captures content client-side from the already-rendered page. Compared to server-side scraping (`memex note add --url`), this:

- Bypasses JavaScript-heavy pages, bot detection, and paywalls
- Extracts from the page as your browser sees it
- Requires no server resources for parsing

## Installation

### From GitHub Release (recommended)

1. Download the `.xpi` file from the [Releases](https://github.com/JasperHG90/memex/releases) page
2. Drag the `.xpi` file onto any Firefox window
3. Click **Add** when prompted

### Unsigned Builds

Unsigned `.xpi` files require Firefox Developer Edition:

1. Open `about:config` in Firefox Developer Edition
2. Set `xpinstall.signatures.required` to `false`
3. Install the `.xpi` as described above

## Configuration

Open settings via the extension icon > **Settings**, or navigate to `about:addons` > **Save to Memex** > **Preferences**.

| Setting | Description | Default |
| :--- | :--- | :--- |
| Server URL | Address of your Memex server | `http://localhost:8000` |
| API Key | Required if the server has auth enabled (password field with show/hide toggle) | *(empty)* |
| Remember API key | When unchecked, the key is session-only and cleared on browser restart. When checked, the key is encrypted and persisted. | unchecked |

After entering your settings, click **Test Connection** to verify the extension can reach the server.

## Usage

### Saving an Article

1. Navigate to the article you want to capture
2. Click the **Save to Memex** extension icon
3. The extension extracts the article content using Readability.js and converts it to Markdown via Turndown
4. Edit the title if needed, select a vault, and optionally add tags or a description
5. Click **Save** — the note is sent to the Memex server in the background

### Saving a PDF

1. Open a PDF in Firefox
2. Click the **Save to Memex** extension icon
3. The raw PDF is uploaded directly to the server, which handles parsing server-side
4. Edit the title, select a vault, and optionally add tags or a description
5. Click **Save**

## API Key Security

The extension offers two storage modes for your API key:

- **Session-only (default):** The key is stored in `browser.storage.session` and cleared when Firefox restarts. This is the safer option.
- **Encrypted persistent:** The key is encrypted with AES-256-GCM using a non-exportable CryptoKey stored in IndexedDB. A fresh 12-byte IV is generated per encryption.

If you previously stored a plaintext API key (legacy behavior), it is automatically migrated to session-only storage on extension load.

## Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| "Connection refused" on Test Connection | Ensure the Memex server is running and the Server URL is correct |
| "Unauthorized" error | Verify your API key matches one configured in the server's `auth.keys` |
| Extension icon missing | Check `about:addons` to confirm the extension is installed and enabled |
| Content not extracted from article | Some pages block extraction; try reloading the page before clicking Save |
| PDF not parsed | PDF parsing happens server-side; check the server logs for errors |

## See Also

* [Configuring Memex](configure-memex.md) — server configuration including auth setup
* [Organizing Content with Vaults](organize-with-vaults.md) — vault creation and management
