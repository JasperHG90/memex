# About system vaults vs content vaults

Every Memex vault now has a **kind** that classifies what the vault is *for* — and that classification controls which surfaces it shows up on. The vast majority of vaults are **content** vaults: project memos, personal journals, research digests. A small set of **system** vaults exist to glue Memex to its environment: today's example is `inbox`, but anything that captures data on behalf of another component (Hermes session state, the planned procedural case-vault, a third-party integration buffer) is the same shape.

The two kinds differ on exactly one axis: **visibility on synthesis-and-discovery surfaces**. They do **not** differ on the retention floor — every vault, system or content, gets the same extraction, full-text search, vector search, entity linking, and so on.

## The contract in one sentence

A vault's kind controls which surfaces the vault is **visible on by default**; it never controls whether the vault is **addressable** or whether data inside it is **extracted and searchable**.

Three invariants follow from that:

1. **Addressability is always on.** You can read `GET /vaults/{id}` for a system vault. You can run `memex memory search --vault inbox` and get its units. You can target a system vault by name or UUID from any tool. Visibility opt-out never takes the form "this vault doesn't exist."
2. **Retention is invariant.** Extraction always runs on a system vault, just as it does on a content vault. Memory units, full-text vectors, entity links, mental models — every observation-surface a content vault gets, a system vault gets too. A future reflection cycle sees system-vault evidence on the same footing as content-vault evidence.
3. **Synthesis is opt-out, not opt-in.** The default for a content vault is *visible everywhere*. The default for a system vault is *visible nowhere on the synthesis-and-discovery surfaces* — but that default is a per-vault `policy`, and a system vault that legitimately should be summarised (or reflected on) flips the relevant flag.

## What "synthesis-and-discovery" actually means

The set of surfaces that mute system vaults by default:

- `memex_list_vaults` and `GET /vaults` — system vaults don't appear unless you ask (`?include_system_vaults=true`, `--include-system`).
- The session briefing's *Available Vaults* section — same gate.
- `memex_get_vault_summary` periodic regeneration — skipped unless the vault's `policy.summarize` is true.
- `memex_survey` and `memex_memory_search` with a default scope (`*` / unset) — system vaults don't enter the in-set unless you opt in (`include_system_vaults=true`) or name them explicitly.
- Reflection enqueue — system vaults without `policy.reflect=true` never produce mental models; their queue stays empty.
- Entity tools (`memex_list_entities`, `memex_get_entity_cooccurrences`, `memex_get_entity_mentions`) — system-vault evidence is excluded from the membership and centrality queries by default, so a system vault cannot "leak" into entity rankings.

What stays the same regardless of kind:

- Every vault is addressable by name or UUID on every surface.
- Extraction, indexing, dedup, contradiction detection — all run on every vault.
- Maintenance operations (`memex note migrate`, lint, deprioritize) work the same.
- The entity graph is global, but cooccurrences are vault-scoped: a system vault contributes its own edges; those edges are excluded from default-scope ranking by the same gate that filters the system vault from search.

## Why "inbox" became a system vault

`inbox` was the canonical case that surfaced the gap. It is a vault like any other — notes land in it, extraction runs, entities link — but its purpose is "things that need a destination," not "things you read about." When an agent booted up and ran `memex memory_search` with the default scope, inbox units polluted the result set. The session briefing's *Available Vaults* list offered `inbox` alongside real workspaces. Reflection on inbox content produced mental models that, in turn, appeared in entity rankings — making inbox-routing artifacts part of an entity's perceived importance.

The fix isn't to add filtering on top — that drifts the rule across every surface. The fix is to make `inbox` *declare* what it is, and let the surfaces know how to read that declaration. That's what `kind='system'` is.

The `inbox` migration in `060_vault_kind_policy.py` does three things in one go: marks `inbox` as a system vault, archives any mental models that already pointed at it, and clears any `VaultSummary` row that was generated for it. Existing content is untouched; the synthesis products that would have been wrong are removed.

## The Union wildcard

`memex memory search` and friends accept a `vault_ids` list (or a comma-separated `--vault` flag) that accepts names, UUIDs, and the literal `*`. The semantics are deliberate:

- `*` (or no list) → all **content** vaults. System vaults are not part of the default universe.
- `[name, ...]` → resolve each name/UUID; system vaults are addressable by name. They are *added* to the content set, never replacing it.
- `[*, name]` → all content vaults *plus* the named system vault. Same as the previous case in practice; the `*` makes the intent explicit.
- `include_system_vaults=true` (per-tool flag) → **unconditional union**: every system vault joins the resolved scope, regardless of whether the caller used `*`, named specific vaults, or omitted the list. The flag is an *opt-in to the system-vault surface as a whole*, not a scope widener. To scope a call to one specific system vault without dragging in the rest, name it and leave the flag off; to read across the system fleet, set the flag.

The practical effect: a user who types `memex memory search` gets the universe they mean — their own content. A user who types `memex memory search --include-system-vaults` widens explicitly to the system fleet, even if they also passed `--vault research` (in which case the result is research + every system vault — pass `--vault inbox` and omit the flag to scope to just the named system vault). There is no way to type something that *only* returns system vaults (other than naming one by hand), because the synthesis tools exist to find your own work, not infrastructure state.

## Policy, kind, and immutability

`kind` is a property of the vault. It is **immutable** — there is no `set-kind` operation, and the CLI requires a `[y/N]` confirmation when you create a vault with `kind=system`. The reasoning: most of the harm comes from changing kind *after* a vault has accumulated state. A content vault with three months of mental models and a daily-rerun vault summary, if flipped to `system` in anger, would have its synthesis purged on the next reflection/summarise cycle, and there is no way to rebuild mental models for content that's no longer being tracked. The 30-second confirmation is the smallest friction that prevents that.

`policy` is mutable. It's a typed JSON document with two fields today (`reflect`, `summarize`), each `bool | None` — `None` means "use the kind's default" (true for content, false for system). The policy is the per-vault override; the kind is the tenancy root. A content vault that wants to be invisible on briefings (because it's a personal scratchpad) can set `policy.summarize=false`; a system vault that *should* be reflected on (because its contents are durable enough to want mental models) can set `policy.reflect=true`.

## The `inbox` case-vault precedent (and beyond)

The next consumers are already sketched. The procedural-experiential memory work calls for a hidden case-vault — notes that capture a single agent session's outcome for the procedural-mem pipeline. That vault is exactly the same shape: addressable, fully extracted, but silent on the agent-facing synthesis surfaces. The same goes for a future Hermes session vault that captures mid-session scratch state. They all instantiate the same `kind='system'` row; they all rely on the same `policy` override if their use case diverges from the default.

A new "system" vault is a one-line creation:

```bash
memex vault create my-integration-buffer --kind system
```

That single command buys you the right behavior on every surface that knows about kind. There is no second place to wire up the contract.

## What this is **not**

- Not multi-tenant isolation. Kind lives on the vault; the tenant model is the vault, as it always has been.
- Not access control. A `require_read` API key sees system vault *names* when it asks for them. There is no kind-based authorization layer; that is a separate concern.
- Not a replacement for `memex_kv_*`. Procedures and preferences still go to KV, not to a system vault. KV is the right tool when the data is small, namespaced, and queryable by key. A system vault is the right tool when the data is a stream of notes that another pipeline ingests.
- Not an extension of the *tenant root* concept. A system vault *is* a tenant. It just behaves differently on a small set of surfaces.

The mental model is: **a vault is a tenant; the kind is a contract on what that tenant is for.** Same plumbing, different policy.
