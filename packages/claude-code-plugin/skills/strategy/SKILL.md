---
name: strategy
description: "Recall the cross-procedure strategy for a verb — Memex's top procedural tier, the general approach that spans the individual procedures for a scope + verb."
argument-hint: "[verb / general approach, e.g. 'how we approach releases']"
---

# /strategy — Recall a strategy

A **strategy** is the top tier of the procedural plane: the general approach that spans the individual procedures for a given scope and verb (e.g. the overall release philosophy above the specific `release` procedures). Strategies are **derived** from clustered procedures — like procedures, you never author one directly.

## 1. Query source

`$ARGUMENTS` names the verb / approach you want. Empty → ask the user.

## 2. Search the strategy tier

- **Fuzzy / natural-language** → `memex_procedural_search(query="...", kind="strategy")`.
- **Exact anchor** → `memex_procedural_get_by_identity(kind="strategy", scope="<scope>", verb="<verb>")`. A strategy anchors on `(scope, verb)` **only** — do **NOT** pass a `context` argument; supplying one makes the tool error out (that's the procedure shape, not the strategy shape). Returns the entry or `null`.
- Scope grammar is the same as procedures: `global`, `project:<id>`, `app:<id>`.

## 3. Present

Surface the strategy and **cite its entry id**. The strategy tier is thin by design and populates only as procedures accumulate and cluster — if there's no strategy for this verb yet, say so plainly. Offer the underlying procedures instead via `/procedure`, rather than passing a single procedure off as a strategy.
