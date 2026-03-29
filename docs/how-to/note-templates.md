# How to Use Note Templates

This guide shows you how to list, use, create, and manage note templates so your notes follow a consistent structure.

## Prerequisites

* Memex CLI installed
* Familiarity with the `memex note` command

## Understanding Template Scope

Templates are discovered in three layers, with later layers overriding earlier ones on slug collision:

1. **Built-in** — shipped with Memex (`general_note`, `quick_note`, `technical_brief`, `architectural_decision_record`, `request_for_comments`)
2. **Global** — stored in `{filestore_root}/templates/`, available across all projects
3. **Project-local** — stored in `.memex/templates/` within a project directory, scoped to that project

## Instructions

### 1. List Available Templates

```bash
memex note template list
```

This shows each template's slug, name, description, and source scope (built-in, global, or local).

### 2. Preview a Template

```bash
memex note template get quick_note
```

Prints the Markdown content of the template so you can see its structure before using it.

### 3. Add a Note Using a Template

```bash
memex note add --template technical_brief --title "Redis Caching Layer"
```

The template provides the Markdown scaffold; you fill in the sections.

### 4. Create a Custom Template

Templates are `.toml` files. Create one with a `name`, `description`, and a `[template]` section containing the Markdown scaffold:

```toml
name = "Sprint Retrospective"
description = "Template for sprint retrospective notes"

[template]
content = """
# {title}

## What went well

## What could be improved

## Action items
"""
```

Use `{title}` as a placeholder — it is replaced with the note title at creation time.

### 5. Register a Custom Template

**Globally** (available to all projects):

```bash
memex note template register ./retro.toml
```

**Project-local** (available only in the current project):

```bash
memex note template register ./retro.toml --local
```

Local templates are stored in `.memex/templates/` and override global templates with the same slug.

### 6. Register a Template from a GitHub Repository

```bash
memex note template register https://github.com/user/repo
```

This fetches template files from the repository and registers them.

### 7. Find the Templates Directory

```bash
# Global templates directory
memex note template dir

# Project-local templates directory
memex note template dir --local
```

### 8. Delete a Custom Template

```bash
memex note template delete sprint_retrospective
```

Add `--yes` to skip the confirmation prompt. Use `--local` to target a project-local template specifically.

> **Note:** Built-in templates cannot be deleted.

## Using Templates via MCP

AI clients connected through MCP can work with templates using these tools:

| Tool | Purpose |
| :--- | :--- |
| `memex_list_templates` | List all available templates with metadata |
| `memex_get_template` | Get the Markdown content of a template by slug |
| `memex_register_template` | Register a template from inline content |

An AI assistant can call `memex_list_templates()` to discover available templates, then `memex_get_template("technical_brief")` to retrieve the scaffold before drafting a note.

## See Also

* [Using the MCP Server](using-mcp.md) — connecting AI clients to Memex
* [Configuring Memex](configure-memex.md) — filestore and project configuration
