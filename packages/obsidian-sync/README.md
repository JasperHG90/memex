<p align="center">
  <img src="assets/logo.jpg" width="160" alt="Memex Folder Sync" />
</p>

<h1 align="center">Memex Folder Sync</h1>

<p align="center">
  Sync local Markdown notes and their assets to Memex.<br/>
  Works with Obsidian vaults, plain directories, or any folder of <code>.md</code> files.<br/>
  <strong>Ingest anything. Remember everything. Retrieve what matters.</strong>
</p>

<p align="center">
  <a href="#usage">Usage</a> &middot;
  <a href="#configuration">Configuration</a> &middot;
  <a href="#handling-deleted-files">Deletes</a> &middot;
  <a href="#running-watch-mode-as-a-background-service">Watch Mode</a>
</p>

---

Scans a folder for Markdown files, detects changes since the last sync, resolves referenced assets (images, PDFs), and ingests everything into Memex via the REST API. Supports both Obsidian-style wiki-link embeds (`![[image.png]]`) and standard Markdown images (`![alt](image.png)`).

## Usage

```bash
# Initialize config in your notes folder
obsidian-memex-sync init ./my-notes

# One-shot sync
obsidian-memex-sync sync ./my-notes

# Dry run (show what would sync)
obsidian-memex-sync sync ./my-notes --dry-run

# Full re-sync (ignore last sync state)
obsidian-memex-sync sync ./my-notes --full

# Show sync status
obsidian-memex-sync status ./my-notes

# Watch for changes (continuous sync)
obsidian-memex-sync watch ./my-notes
obsidian-memex-sync watch ./my-notes --mode poll
```

## Configuration

Run `obsidian-memex-sync init <path>` to create a default `obsidian-sync.toml`, or create one manually in your notes folder root (or `~/.config/memex/obsidian-sync.toml` for global defaults).

All settings can be overridden via environment variables with the prefix `OBSIDIAN_SYNC_` and double-underscore nesting (e.g., `OBSIDIAN_SYNC_SERVER__API_KEY`).

```toml
[server]
url = "http://localhost:8321"
# api_key = ""  # or set OBSIDIAN_SYNC_SERVER__API_KEY env var
# vault_id = ""  # target Memex vault ID or name

[sync]
state_file = ".memex-sync.db"
batch_size = 32

[sync.exclude]
# Directories always excluded (Obsidian internals + common non-content dirs)
base = [".obsidian", ".trash", ".git", "node_modules"]
# Additional glob patterns to exclude
extends_exclude = []
# Folder names whose notes are never synced (exact match at any depth)
ignore_folders = []

# Notes with this frontmatter are skipped:
#   ---
#   agents: skip
#   ---
frontmatter_skip_key = "agents"
frontmatter_skip_value = "skip"

[sync.assets]
# Upload referenced assets (images, PDFs, etc.) alongside notes
enabled = true
# Skip assets larger than this (MB)
max_size_mb = 50
# Extra asset extensions beyond defaults (.png, .jpg, .jpeg, .gif, .svg, .pdf, .webp)
extends_include = []

[watch]
# "events" (watchdog, reactive), "poll" (periodic), or "off"
mode = "events"
# Seconds to wait after last change before syncing (event mode)
debounce_seconds = 5
# Seconds between sync cycles (poll mode)
poll_interval_seconds = 300
```

## Skipping individual notes

Add a frontmatter marker to any note you don't want synced:

```markdown
---
agents: skip
---

# This note will not be synced
```

The key and value are configurable via `frontmatter_skip_key` and `frontmatter_skip_value`.

## Ignoring folders

Use `ignore_folders` to exclude entire folders by name (matched at any depth):

```toml
[sync.exclude]
ignore_folders = ["private", "scratch", "drafts"]
```

This differs from `extends_exclude` (which uses glob patterns) — `ignore_folders` matches exact folder names.

## Handling deleted files

When a file is deleted from the local folder, the sync tool handles it automatically:

**Default behavior (archive):** The corresponding Memex note is marked as `archived`. All its memory units become `stale` and are excluded from retrieval. The data is preserved and can be restored by setting the note status back to `active`.

```bash
# Default: archive deleted notes (soft delete)
obsidian-memex-sync sync ./my-notes
```

**Hard delete:** Permanently remove the note and all associated data (memory units, entities, assets) from Memex. Irreversible.

```bash
obsidian-memex-sync sync ./my-notes --hard-delete
```

**Skip delete handling:** Just report deleted files without taking any action in Memex.

```bash
obsidian-memex-sync sync ./my-notes --no-handle-deletes
```

## Background sync and job tracking

Submit a sync as a background job and check on it later:

```bash
# Submit and return immediately
obsidian-memex-sync sync ./my-notes --background
# Output: Batch job submitted: <job-id>

# Check job status
obsidian-memex-sync job-status <job-id>
```

## How it works

1. **Scan** the folder recursively for `.md` files
2. **Exclude** files matching configured patterns, ignored folders, and frontmatter skip markers
3. **Resolve** referenced assets (images, PDFs) from both `![[wiki-links]]` and `![markdown](images)`
4. **Diff** against the last sync state (stored in `.memex-sync.db`, a SQLite database) to find new/changed notes
5. **Ingest** changed notes (with assets) to Memex via the batch REST API
6. **Save** updated sync state

Each note gets a stable `note_key` (`obsidian:<folder-name>:<relative-path>`) so re-syncing unchanged content is a no-op.

## Running watch mode as a background service

The `watch` command runs in the foreground. To keep it running persistently, use your OS service manager.

### Linux (systemd)

Create `~/.config/systemd/user/memex-sync.service`:

```ini
[Unit]
Description=Memex note sync
After=network.target

[Service]
ExecStart=%h/.local/bin/obsidian-memex-sync watch %h/Documents/notes
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now memex-sync
systemctl --user status memex-sync      # check status
journalctl --user -u memex-sync -f      # follow logs
```

To sync multiple folders, create one service per folder (e.g., `memex-sync-work.service`, `memex-sync-personal.service`).

### macOS (launchd)

Create `~/Library/LaunchAgents/com.memex.sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.memex.sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/obsidian-memex-sync</string>
    <string>watch</string>
    <string>/Users/you/Documents/notes</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/memex-sync.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/memex-sync.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.memex.sync.plist
launchctl list | grep memex                                # check status
launchctl unload ~/Library/LaunchAgents/com.memex.sync.plist  # stop
```

### Windows (Task Scheduler)

Open Task Scheduler and create a new task:

1. **General**: "Memex Note Sync", check "Run whether user is logged on or not"
2. **Triggers**: "At log on" (or "At startup")
3. **Actions**: Start a program
   - Program: `C:\Users\you\.local\bin\obsidian-memex-sync.exe`
   - Arguments: `watch C:\Users\you\Documents\notes`
4. **Settings**: Check "If the task fails, restart every 1 minute"

Or via PowerShell:

```powershell
$action = New-ScheduledTaskAction `
  -Execute "obsidian-memex-sync" `
  -Argument "watch C:\Users\you\Documents\notes"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "MemexSync" -Action $action -Trigger $trigger -Settings $settings
```

### Quick and dirty (any OS)

For a simple background process without a service manager:

```bash
# Start in background
nohup obsidian-memex-sync watch ./my-notes > /tmp/memex-sync.log 2>&1 &

# Check if running
ps aux | grep obsidian-memex-sync

# Stop
kill $(pgrep -f "obsidian-memex-sync watch")
```
