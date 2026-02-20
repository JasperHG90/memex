# Organizing with Vaults

Vaults provide logical isolation within Memex. They are "folders" for your memories, allowing you to separate personal knowledge from work projects or shared data.

## Understanding Vault Types

- **Global Vault**: Every Memex instance has a `global` vault (ID: `ac9b6a45-d388-5ddb-9fa9-50d4e5bca511`). If no vault is specified, data goes here.
- **Active Vault (Write)**: The vault currently used for new ingestions.
- **Attached Vaults (Read)**: Additional vaults that are searched when you perform a query.

## Managing Vaults via CLI

### 1. Create a Vault
```bash
memex vault create "Project Hindsight" --description "Research on human-AI memory consolidation"
```

### 2. List Vaults
```bash
memex vault list
```
This shows all vaults, their IDs, and identifies which one is currently **Active** in your configuration.

### 3. Deleting a Vault
```bash
memex vault delete "Project Hindsight"
```

## How to "Switch" Vaults

There is no `vault switch` command. Instead, you specify the vault context in one of two ways:

### A. Override via CLI Flag (Temporary)
Use the `--vault` or `-v` flag on any ingestion or search command.

```bash
# Ingest into a specific vault
memex memory add --url "https://example.com" --vault project-x

# Search across specific vaults
memex memory search "Status" --vault project-x --vault global
```

### B. Update Configuration (Persistent)

Place a `.memex.yaml` in your local directory that overrides the vault-related settings:

```yaml
server:
  active_vault: "project-x"
  attached_vaults: ["global", "reference-material"]
```

The **active vault** will be used for write operations. **Attached vaults** are read-only vaults.

## Why use Vaults?

1.  **Security/Privacy**: Keep client data isolated in dedicated vaults.
2.  **Search Relevance**: Limit search scope to relevant projects to reduce noise.
3.  **Reflection Scope**: The Reflection loop (Hindsight) operates within a vault context, synthesizing mental models specific to that project.
