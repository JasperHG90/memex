"""Suite runner — orchestrates one suite invocation end-to-end."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import inspect
import json
import logging
import random
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import httpx

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import CreateVaultRequest, NoteCreateDTO

from memex_eval.helpers import wait_for_extraction
from memex_eval.judge import Judge
from memex_eval.suite.agents import AgentAnswer, get_backend
from memex_eval.suite.base import (
    LLMJudge,
    RunResult,
    Scenario,
    ScenarioOutcome,
    SetupAction,
    Suite,
    UsefulAtK,
)
from memex_eval.suite.metrics import aggregate_metric_keys, percentile
from memex_eval.suite.setup_actions import get_setup_action
from memex_eval.suite.sources import canonicalize_name

if TYPE_CHECKING:
    from memex_eval.recorders.mlflow_recorder import MLflowRecorder, NullRecorder

logger = logging.getLogger('memex_eval.suite.runner')


def _git_capture(args: list[str]) -> str:
    try:
        return (
            subprocess.check_output(
                ['git', *args], stderr=subprocess.DEVNULL, timeout=5, cwd=str(Path.cwd())
            )
            .decode()
            .strip()
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.CalledProcessError):
        return ''


def _memex_version() -> str:
    try:
        from memex_core.__about__ import __version__

        return str(__version__)
    except Exception:
        return ''


_NOTES_TAG_MAX = 240
_NOTES_OVERFLOW_SUFFIX = '… (see run_notes.md artifact)'

# Param keys consumed by the runner — stripped before the SetupAction's params
# dict is handed to the registered handler so handlers can use those names
# freely in their own ``extra='allow'`` fields. If you add another runner-
# interpreted reserved field on ``SetupAction``, add it here too.
_RUNNER_RESERVED_PARAM_KEYS: frozenset[str] = frozenset({'kind', 'required'})


def _build_notes_tag(notes: str | None) -> str:
    """Build the MLflow ``notes`` tag value from a free-form notes body.

    Returns an empty string when ``notes`` is None or whitespace-only —
    callers should treat that as "skip the tag entirely". Appends the
    overflow suffix only when the tag actually loses content (multi-line
    body OR first line longer than the cap), so trailing whitespace alone
    does not falsely advertise a longer artifact.
    """
    if not notes:
        return ''
    stripped = notes.strip()
    if not stripped:
        return ''
    first_line = stripped.splitlines()[0]
    truncated = first_line[:_NOTES_TAG_MAX]
    content_lost = len(stripped.splitlines()) > 1 or len(first_line) > _NOTES_TAG_MAX
    return truncated + (_NOTES_OVERFLOW_SUFFIX if content_lost else '')


def _extract_judge_revision(lm: Any) -> str | None:
    try:
        entry = lm.history[-1]
        return entry.get('response', {}).get('model') or entry.get('model')
    except (IndexError, AttributeError, KeyError, TypeError):
        return None


async def _setup_vault(api: RemoteMemexAPI, name: str, description: str) -> UUID:
    """Create a vault if it doesn't exist; otherwise truncate it for a fresh slate."""
    vaults = await api.list_vaults()
    for vault in vaults:
        if vault.name == name:
            with contextlib.suppress(Exception):
                await api.truncate_vault(vault.id)
            return vault.id
    vault = await api.create_vault(CreateVaultRequest(name=name, description=description))
    return vault.id


async def _run_setup_actions(
    api: RemoteMemexAPI,
    vault_id: UUID,
    actions: list[SetupAction],
    note_key_to_unit_ids: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Dispatch each action through the setup-action registry.

    Each handler's optional dict return is auto-prefixed with the handler
    name (e.g. ``snapshot.baseline``) and merged into the per-scenario
    context. The context dict carries:

    - ``<action_name>.<key>`` entries from each handler's return.
    - ``_setup_failures`` — list of ``{kind, error}`` for any action that
      raised. Outcomes that depend on baseline data should check this.
    - ``_required_setup_failed`` — True if any handler with
      ``required=True`` raised; the runner short-circuits the scenario to
      status='error' in this case.

    ``note_key_to_unit_ids`` is the runner's post-extraction map of
    source ``note_key`` → list of unit IDs. It's threaded into each
    handler's params under the private key ``_note_key_to_unit_ids``
    so ``_resolve_unit_ids`` can do deterministic note-key-scoped
    resolution (preferred over ``search_query``).
    """
    # ``_per_action_results``: positional list of each action's run() return
    # (or None if the action didn't return a dict / raised). The teardown loop
    # passes ``_per_action_results[i]`` to ``actions[i]``'s handler.teardown(),
    # so two ``record_outcome`` calls in one scenario each get their OWN
    # context — vs. the merged ``record_outcome.unit_ids`` key (which the
    # second action would overwrite, leaking the first's stamp into the
    # next scenario).
    #
    # ``_executed_action_indices``: parallel list of position-indices whose
    # run() actually completed (no raise, no missing-handler). Replaces the
    # earlier ``_executed_action_kinds`` membership check, which was wrong
    # for duplicate-kind actions: with two ``record_outcome`` actions where
    # one succeeded and one raised, list-membership of the kind would still
    # invoke the failed action's teardown — and it would then read the
    # successful action's data via the merged-context fallback (review
    # round-1 CRITICAL #1). Indices are exact.
    #
    # ``_executed_action_kinds`` is preserved as an aggregate-of-kinds
    # because some external tests/tools read it. It now reflects only the
    # set of kinds that succeeded, deduped.
    context: dict[str, Any] = {
        '_setup_failures': [],
        '_executed_action_kinds': [],
        '_executed_action_indices': [],
        '_per_action_results': [],
    }
    for i, action in enumerate(actions):
        try:
            handler = get_setup_action(action.kind)
        except KeyError as e:
            context['_setup_failures'].append({'kind': action.kind, 'error': str(e)})
            context['_per_action_results'].append(None)
            logger.warning('  Setup: %s', e)
            # If the missing handler was declared required at the action
            # level, treat as a required-failure so the scenario short-circuits.
            if getattr(action, 'required', False):
                context['_required_setup_failed'] = True
                break
            continue
        try:
            params = {
                k: v for k, v in action.model_dump().items() if k not in _RUNNER_RESERVED_PARAM_KEYS
            }
            if note_key_to_unit_ids is not None:
                # Underscore-prefixed runner-injected key. Handlers MAY read
                # it via _resolve_unit_ids; custom handlers ignore it freely.
                params['_note_key_to_unit_ids'] = note_key_to_unit_ids
            result = await handler.run(api, vault_id, params)
            # Persist the un-prefixed dict for the per-action teardown path.
            context['_per_action_results'].append(result if isinstance(result, dict) else None)
            if isinstance(result, dict):
                # Merged context for outcome.score() reads (kept for back-compat).
                # Note that if two actions of the same kind run, the LAST one
                # wins for these keys — that's why teardowns now read from
                # ``_per_action_results`` instead.
                prefix = action.kind + '.'
                for k, v in result.items():
                    key = k if k.startswith(prefix) else f'{prefix}{k}'
                    context[key] = v
            # Successful run — track BOTH the index (authoritative) and the
            # kind (de-duped, kept for back-compat readers).
            context['_executed_action_indices'].append(i)
            if action.kind not in context['_executed_action_kinds']:
                context['_executed_action_kinds'].append(action.kind)
        except Exception as e:
            context['_setup_failures'].append({'kind': action.kind, 'error': str(e)})
            context['_per_action_results'].append(None)
            if getattr(handler, 'required', False) or getattr(action, 'required', False):
                context['_required_setup_failed'] = True
                # Stop running further actions so we don't mutate vault state
                # after a required snapshot/precondition has failed.
                logger.warning(
                    '  Required setup %s failed; aborting remaining setup actions.',
                    action.kind,
                )
                break
            logger.warning('  Setup action %s failed: %s', action.kind, e)
    return context


async def _run_setup_teardowns(
    api: RemoteMemexAPI,
    vault_id: UUID,
    actions: list[SetupAction],
    setup_context: dict[str, Any],
) -> None:
    """Invoke ``handler.teardown()`` for each setup action whose ``run()``
    actually executed in this scenario (per ``_executed_action_kinds``).

    Per-handler isolation: an exception inside one teardown is caught with a
    logger warning and does NOT abort subsequent teardowns. A failed teardown
    leaves vault state dirty for the next scenario; that's the trade-off for
    keeping the loop going so other handlers still clean up their own state.

    Skipped silently when an action's ``run()`` never executed (earlier
    required-failure broke the loop) — there's nothing to undo and emitting
    a warning would be misleading.
    """
    # ``_per_action_results`` is positionally aligned with ``actions``: index i
    # holds the run() return for action i. ``_executed_action_indices`` lists
    # the indices whose run() actually completed.
    #
    # Each teardown gets ITS OWN per-action run() return as ``setup_context``
    # — never the merged dict. Pre-fix: when index i ran but returned None,
    # the merged-context fallback would silently substitute a SIBLING action's
    # data (review round-1 CRITICAL #2), causing double-revert / wrong-target
    # SQL. Post-fix: if per_action_results[i] is None, the teardown still
    # runs (some handlers carry no run-state — e.g. consolidation_tick) but
    # is fed ``{}`` rather than the merged dict.
    #
    # Back-compat: external callers may pass setup_context without these
    # keys. Falls through to legacy behavior — pass the whole context AND
    # honor the legacy ``_executed_action_kinds`` membership check.
    per_action_results: list[dict[str, Any] | None] = setup_context.get('_per_action_results') or []
    executed_indices_raw = setup_context.get('_executed_action_indices')
    legacy_executed_kinds: list[str] = setup_context.get('_executed_action_kinds', [])
    if executed_indices_raw is not None:
        executed_indices: set[int] = set(executed_indices_raw)
        use_positional = True
    else:
        executed_indices = set()
        use_positional = False
    for i, action in enumerate(actions):
        if use_positional:
            if i not in executed_indices:
                logger.debug(
                    'Skipping teardown of %r at index %d (setup did not execute).',
                    action.kind,
                    i,
                )
                continue
        else:
            # Legacy path: external callers without per-action tracking.
            if action.kind not in legacy_executed_kinds:
                logger.debug('Skipping teardown of %r (setup did not execute).', action.kind)
                continue
        try:
            handler = get_setup_action(action.kind)
            params = {
                k: v for k, v in action.model_dump().items() if k not in _RUNNER_RESERVED_PARAM_KEYS
            }
            # Per-action context. Use the action's OWN run() return; never
            # the merged context (would substitute sibling action's data).
            # ``{}`` for actions that returned None / had nothing to publish.
            per_ctx: dict[str, Any]
            if use_positional:
                slot = per_action_results[i] if i < len(per_action_results) else None
                per_ctx = slot if slot is not None else {}
            else:
                # Legacy callers: use merged context (pre-fix behavior).
                per_ctx = setup_context
            await handler.teardown(api, vault_id, params, per_ctx)
        except Exception as exc:
            logger.warning(
                'Teardown of %r raised %s — continuing with remaining teardowns. '
                'Vault state may be dirty for the next scenario.',
                action.kind,
                exc,
            )


async def _run_inline_note_teardowns(
    api: RemoteMemexAPI,
    setup_context: dict[str, Any],
) -> None:
    """DELETE every inline note this scenario ingested.

    Cascade deletes the note's memory units, unit_entities edges, and
    audit-log breadcrumbs at the SQL level (FK ON DELETE CASCADE in the
    schema), so no DB-direct cleanup is needed beyond the public DELETE.

    Idempotent: ``RemoteMemexAPI.delete_note`` returns False on 404, so
    re-running a teardown (e.g. when a deferred consumer also tries to
    delete the same note) is harmless. Per-note try/except keeps one
    failure from blocking the rest of the teardowns.
    """
    inline_ids: dict[str, str] = setup_context.get('_inline_note_ids') or {}
    if not inline_ids:
        return
    for note_key, note_id in inline_ids.items():
        try:
            await api.delete_note(UUID(str(note_id)))
        except Exception as exc:
            logger.warning(
                'inline-note teardown: delete_note(%s, key=%r) failed: %s. '
                'Vault may carry the inline note into the next scenario.',
                note_id,
                note_key,
                exc,
            )


async def _ingest_sources(
    api: RemoteMemexAPI,
    vault_id_default: UUID,
    vault_map: dict[str | None, UUID],
    suite: Suite,
) -> dict[str, str]:
    """Ingest every source note. Returns {note_key: note_id}.

    Validates note_key uniqueness across vaults — silent collapse on
    duplicate keys would break TemporalOrdering / NoteAttribution outcomes
    (which rely on note_key → note_id mapping); per-vault routing means
    distinct vaults can contain genuinely-different notes that share a key,
    but the suite framework requires keys be globally unique within a suite.
    """
    # P3 + round-2 M1: detect duplicate note_keys across vaults at load time.
    seen_vault_by_key: dict[str, str] = {}
    for note in suite.sources.notes:
        vault_label = note.vault_name or '__default__'
        prior = seen_vault_by_key.get(note.note_key)
        if prior is not None and prior != vault_label:
            raise ValueError(
                f'Duplicate note_key {note.note_key!r} across vaults '
                f'({prior!r} and {vault_label!r}). note_key must be globally '
                f'unique within a suite (TemporalOrdering / NoteAttribution '
                f'rely on a flat note_key → note_id map).'
            )
        seen_vault_by_key[note.note_key] = vault_label

    note_id_by_key: dict[str, str] = {}
    for note in suite.sources.notes:
        target_vault_id = vault_map.get(note.vault_name, vault_id_default)
        files_b64 = note.asset_bytes_b64()
        import base64

        dto = NoteCreateDTO(
            name=note.title or note.note_key,
            description=note.description or f'Eval suite source: {note.note_key}',
            # ``wire_content()`` re-emits frontmatter so the server's
            # parser sees ``publish_date`` etc. (the loader strips the
            # YAML block from ``post.content`` for SourceNote internals).
            content=base64.b64encode(note.wire_content().encode('utf-8')),
            files=files_b64,
            tags=note.tags,
            vault_id=str(target_vault_id),
            note_key=f'eval-{suite.name}-{note.note_key}',
        )
        resp = await api.ingest(dto)
        if hasattr(resp, 'note_id') and resp.note_id:
            # ``IngestResponse.note_id`` is currently a 32-char MD5 hex
            # idempotency key (`note.py:241` calls it "MD5 hex digest, not
            # a UUID"); ``MemoryUnitDTO.note_id`` is parsed as ``UUID`` and
            # stringifies dashed. Round-trip through ``UUID()`` so
            # ``TemporalOrdering`` / ``NoteAttribution`` see one canonical
            # form on both sides. If the wire format ever drifts to
            # something that isn't a valid UUID literal, fall back to the
            # raw string + warn — better stale-comparison than crash.
            note_id_by_key[note.note_key] = _canonical_uuid(resp.note_id, note.note_key)
        elif hasattr(resp, 'status') and resp.status == 'skipped':
            # Idempotent skip — find the existing note in THIS vault by name.
            # Suite-prefixed note_key would be more precise but the client lacks
            # a by-key lookup; vault-scoped title search is sufficient because
            # the per-suite vault contains only this suite's notes.
            lookup_name = note.title or note.note_key
            try:
                existing = await api.find_notes_by_title(lookup_name, vault_ids=[target_vault_id])
            except Exception as e:
                logger.warning(
                    'idempotent-skip lookup failed for note_key=%r: %s', note.note_key, e
                )
                continue
            # ``FindNoteResult.title`` + ``.note_id`` are the wire fields
            # (schemas.py:1394-1395) — NOT ``.name`` / ``.id``. Reading
            # the wrong field returns the empty fallback for every row,
            # silently dropping every match and rendering the
            # duplicate-title RuntimeError unreachable in production
            # (round-4 CRITICAL). Casefold + whitespace-collapse via the
            # shared helper so ingest, reuse-vault, and trigger_reflections
            # all canonicalise the same way (round-3 MEDIUM 1).
            target_canon = canonicalize_name(lookup_name)
            matches = [
                n
                for n in existing
                if canonicalize_name(getattr(n, 'title', '') or '') == target_canon
            ]
            # Refuse to silently pick a winner when titles collide — caught
            # OUTSIDE the lookup try/except so the RuntimeError propagates
            # (a swallowed raise would degrade to the same silent
            # mis-attribute the round-1 fix was meant to remove).
            if len(matches) > 1:
                raise RuntimeError(
                    f'idempotent-skip lookup for note_key={note.note_key!r} '
                    f'found {len(matches)} notes named {lookup_name!r} in vault '
                    f'{target_vault_id}; refuse to silently pick one. Make titles '
                    f'unique inside a suite.'
                )
            if matches:
                note_id_by_key[note.note_key] = str(matches[0].note_id)
    return note_id_by_key


_canonical_uuid_warned: set[str] = set()


def _compute_own_skip_reason(
    sc: Scenario,
    *,
    suite: Suite,
    reuse_vault: str | None,
    config_snapshot_available: bool,
    nli_available: bool | None,
) -> str | None:
    """Skip reason a scenario would receive based on its OWN config (not
    inherited from deps). Module-level so tests can exercise the
    decision in isolation; the runner's per-scenario loop wraps this in
    a closure for ergonomics.

    Currently produces: ``'setup_action_not_reusable'`` (under
    ``--reuse-vault`` when any setup_action declares
    ``reusable_under_reuse_vault = False``) or ``'nli_disabled'`` (when
    NLI is required but the live config has it off). Returns ``None``
    when the scenario is runnable.
    """
    if reuse_vault is not None:
        for action in sc.setup_actions:
            try:
                hcls = type(get_setup_action(action.kind))
            except KeyError:
                continue
            if not getattr(hcls, 'reusable_under_reuse_vault', True):
                return 'setup_action_not_reusable'
    sc_needs_nli = suite.metadata.requires_nli_classifier or sc.requires_nli_classifier
    if sc_needs_nli and config_snapshot_available and nli_available is False:
        return 'nli_disabled'
    return None


def _canonical_uuid(value: str, note_key: str) -> str:
    """Return ``str(UUID(value))`` — canonical dashed form — falling back
    to the raw value with a warning if ``value`` is not a UUID literal.

    The eval framework compares this against ``MemoryUnitDTO.note_id``
    which is always canonical; a non-UUID value will fail the comparison
    one way or another, but we'd rather log + degrade than crash.

    Caller MUST pass a non-empty string. ``None`` / empty are upstream
    bugs and would be silently coerced to ``'None'`` / ``''`` which
    cannot match any UUID — that mode caused the original silent
    mis-attribute. Raise so the caller's existing ``if hasattr(resp,
    'note_id') and resp.note_id`` guard is the single point of truth.
    """
    if not value:
        raise ValueError(
            f'_canonical_uuid called with empty value for note_key={note_key!r}; '
            f"caller's truthy-guard should have rejected this."
        )
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        # Dedupe identical values within a process — if the wire format
        # ever drifts to a single opaque token type re-used across notes,
        # the suite would otherwise log N× the same warning. Distinct
        # opaque tokens (e.g. UUIDv7 per note) STILL log per-token; this
        # is memoisation, not a true rate limit. Acceptable: the actionable
        # signal ("the wire format changed") is recoverable from the
        # first warning of any value.
        if value not in _canonical_uuid_warned:
            _canonical_uuid_warned.add(value)
            logger.warning(
                'note_id %r is not a UUID literal (note_key=%r): %s. '
                'Storing raw — TemporalOrdering / NoteAttribution may mis-attribute. '
                '(Identical-value warnings suppressed; distinct values still emit.)',
                value,
                note_key,
                exc,
            )
        return str(value)


async def _wait_extraction_per_note(
    api: RemoteMemexAPI,
    note_id_by_key: dict[str, str],
    note_key_to_vault_id: dict[str, UUID],
    per_note_timeout_s: float = 60.0,
    poll_interval_s: float = 2.0,
) -> dict[str, list[str]]:
    """For each ingested note, poll the new /notes/{id}/memory_units endpoint
    until ≥1 unit appears or per-note timeout. Returns {note_key: [unit_ids]}.

    ``note_key_to_vault_id`` maps each note_key to its target vault — notes
    routed to a non-default vault must be polled under that vault, otherwise
    the API returns 0 units regardless of extraction state and the helper
    times out spuriously.
    """
    out: dict[str, list[str]] = {}
    for note_key, note_id in note_id_by_key.items():
        target_vault_id = note_key_to_vault_id[note_key]
        deadline = time.monotonic() + per_note_timeout_s
        units: list[str] = []
        while time.monotonic() < deadline:
            try:
                rows = await api.list_memory_units_by_note(note_id, target_vault_id)
                if rows:
                    units = [str(u.id) for u in rows]
                    break
            except Exception as e:
                logger.debug('  list_memory_units_by_note failed for %s: %s', note_key, e)
            await asyncio.sleep(poll_interval_s)
        if not units:
            logger.warning(
                '  Extraction timed out for note_key=%r (note_id=%s, vault=%s); '
                'scenarios referencing this key will status=error',
                note_key,
                note_id,
                target_vault_id,
            )
        out[note_key] = units
    return out


async def _ingest_inline_notes(
    api: RemoteMemexAPI,
    vault_id: UUID,
    suite: Suite,
    scenario: Scenario,
    note_key_to_unit_ids: dict[str, list[str]],
) -> dict[str, str]:
    """Ingest a scenario's inline notes into the suite vault and resolve
    their note_key → unit_ids in-place on ``note_key_to_unit_ids``.

    Each inline note's wire-level note_key is prefixed with
    ``inline-<scenario_id>-`` to avoid collisions with sibling scenarios.
    The map is populated under both the prefixed AND short keys so
    GoldUnitIds outcomes can reference either form.

    Returns a ``{short_note_key: note_id}`` map of every inline note this
    call ingested (or recovered via idempotency lookup), so the caller can
    register them for teardown. On a no-op replicate where every inline
    note was already resolved, returns an empty dict — teardown should
    only fire on the scenario instance that actually owns the ingest.

    No-op if the scenario has no inline notes OR if all inline notes have
    already been resolved (e.g. on a 2nd replicate of the same scenario).
    """
    import base64

    if not scenario.inline_notes:
        return {}
    # Idempotence guard for replicates 2..N: gate on the per-scenario prefixed
    # key, never the short form. Two scenarios may legitimately declare inline
    # notes under the same short note_key; the prefixed form is the unique id.
    prefixed_keys = {f'inline-{scenario.id}-{n.note_key}' for n in scenario.inline_notes}
    if all(k in note_key_to_unit_ids for k in prefixed_keys):
        # Re-publish the short forms in case another scenario shadowed them.
        for n in scenario.inline_notes:
            note_key_to_unit_ids[n.note_key] = note_key_to_unit_ids[
                f'inline-{scenario.id}-{n.note_key}'
            ]
        return {}

    inline_id_by_key: dict[str, str] = {}
    for inline in scenario.inline_notes:
        prefixed = f'inline-{scenario.id}-{inline.note_key}'
        wire_note_key = f'eval-{suite.name}-{prefixed}'
        lookup_name = inline.title or inline.note_key
        dto = NoteCreateDTO(
            name=lookup_name,
            description=inline.description or f'Eval suite inline note: {inline.note_key}',
            content=base64.b64encode(inline.content.encode('utf-8')),
            tags=inline.tags,
            vault_id=str(vault_id),
            note_key=wire_note_key,
        )
        # Per-note try/except: if one inline note fails to ingest, keep going
        # so the rest of the scenario's inline notes (and the scenario itself)
        # can still produce a meaningful error breadcrumb.
        try:
            resp = await api.ingest(dto)
        except Exception as e:
            logger.warning('Inline-note ingest failed for %r: %s', inline.note_key, e)
            continue
        if hasattr(resp, 'note_id') and resp.note_id:
            inline_id_by_key[inline.note_key] = str(resp.note_id)
        elif hasattr(resp, 'status') and resp.status == 'skipped':
            # Idempotency-skip: same wire note_key already in this vault. The
            # only path that hits this is intra-scenario retry (because each
            # scenario's wire note_key is uniquely prefixed with the
            # scenario_id). Look up by title within THIS vault; if zero or
            # multiple matches, log loudly so the eval status='error' has
            # a breadcrumb the user can act on.
            try:
                existing = await api.find_notes_by_title(lookup_name, vault_ids=[vault_id])
                # ``FindNoteResult.title`` + ``.note_id`` (schemas.py:1394).
                # See round-4 CRITICAL on the symmetric fresh-ingest path.
                target_canon = canonicalize_name(lookup_name)
                matches = [
                    n
                    for n in existing
                    if canonicalize_name(getattr(n, 'title', '') or '') == target_canon
                ]
                if len(matches) == 1:
                    inline_id_by_key[inline.note_key] = str(matches[0].note_id)
                elif not matches:
                    logger.warning(
                        'Inline-note idempotent-skip: no existing note with '
                        'name=%r found in vault for note_key=%r; downstream '
                        'GoldUnitIds will status=error',
                        lookup_name,
                        inline.note_key,
                    )
                else:
                    logger.warning(
                        'Inline-note idempotent-skip: %d notes share name=%r '
                        'in this vault; refusing to guess which one belongs '
                        'to inline note_key=%r',
                        len(matches),
                        lookup_name,
                        inline.note_key,
                    )
            except Exception as e:
                logger.warning(
                    'Inline-note idempotent-skip lookup failed for %r: %s',
                    inline.note_key,
                    e,
                )
        else:
            logger.warning(
                'Inline-note ingest returned no note_id for %r (resp=%r); '
                'GoldUnitIds referencing this key will status=error',
                inline.note_key,
                resp,
            )

    if not inline_id_by_key:
        return {}
    # Inline notes always live in the scenario's per-call vault.
    inline_vault_map = {k: vault_id for k in inline_id_by_key}
    resolved = await _wait_extraction_per_note(api, inline_id_by_key, inline_vault_map)
    for inline_key, unit_ids in resolved.items():
        note_key_to_unit_ids[inline_key] = unit_ids
        prefixed = f'inline-{scenario.id}-{inline_key}'
        note_key_to_unit_ids[prefixed] = unit_ids
    return inline_id_by_key


def _compute_filter_inclusion(
    scenarios: list[Scenario],
    scenario_ids: list[str] | None,
    groups: list[str] | None,
) -> set[str] | None:
    """Compute the included-scenario-id set under ``--scenario`` / ``--group``
    filters. Returns ``None`` when neither filter is set (run everything).

    Both filters expand by walking ``depends_on_prior_scenarios`` so a
    consumer's prerequisites still execute. When BOTH are set, the result
    is the intersection of (scenario-id closure) and (group closure).
    Unknown ids or groups raise ValueError.
    """
    if not scenario_ids and not groups:
        return None
    all_ids = {s.id for s in scenarios}

    def _close_under_deps(seed: set[str]) -> set[str]:
        out = set(seed)
        while True:
            new = {d for s in scenarios if s.id in out for d in s.depends_on_prior_scenarios}
            if new <= out:
                return out
            out |= new

    id_closure: set[str] | None = None
    if scenario_ids:
        requested_ids = set(scenario_ids)
        unknown = requested_ids - all_ids
        if unknown:
            raise ValueError(
                f'Unknown scenario id(s) {sorted(unknown)}. Available: {sorted(all_ids)}'
            )
        id_closure = _close_under_deps(requested_ids)

    group_closure: set[str] | None = None
    if groups:
        requested_groups = set(groups)
        available_groups = {s.group for s in scenarios if s.group is not None}
        if not available_groups:
            raise ValueError(
                'No scenarios in this suite declare a ``group`` field; '
                f'cannot apply --group {sorted(requested_groups)}. Drop '
                f'--group, or annotate scenarios with ``group="<name>"``.'
            )
        unknown_groups = requested_groups - available_groups
        if unknown_groups:
            raise ValueError(
                f'Unknown group(s) {sorted(unknown_groups)}. Available: {sorted(available_groups)}'
            )
        group_seed = {s.id for s in scenarios if s.group in requested_groups}
        group_closure = _close_under_deps(group_seed)

    if id_closure is not None and group_closure is not None:
        return id_closure & group_closure
    return id_closure if id_closure is not None else group_closure


# Numeric metric coercion lives in ``decorator`` (single source of
# truth); we re-export under the runner-side name to keep call-site
# semantics readable in this module.
from memex_eval.suite.decorator import _coerce_numeric as _coerce_numeric_metrics  # noqa: E402


def _custom_eval_has_async_fn(expected: Any) -> bool:
    """True iff ``expected`` is a ``CustomEvaluate`` whose ``fn`` is an
    async callable. The runner uses this to bypass the sync ``score()``
    path (which would raise inside a running event loop) and dispatch
    the function directly via ``await``.

    Detects:
    - ``async def`` functions (``inspect.iscoroutinefunction``).
    - Callable instances whose ``__call__`` is ``async def`` (e.g. an
      evaluator class that holds judge config in ``__init__``).
    - ``functools.partial`` wrapping any of the above (handled
      automatically by ``iscoroutinefunction`` since 3.8).
    """
    fn = getattr(expected, 'fn', None)
    if fn is None:
        return False
    if inspect.iscoroutinefunction(fn):
        return True
    # Callable-instance case: check __call__.
    call_attr = getattr(fn, '__call__', None)
    return call_attr is not None and inspect.iscoroutinefunction(call_attr)


async def _invoke_custom_evaluate_async(
    expected: Any,
    answer: Any,
    scenario: Scenario,
    *,
    note_key_to_unit_ids: dict[str, list[str]],
    judge: Any | None,
    context: dict[str, Any] | None,
) -> dict[str, float]:
    """Async dispatch path for ``CustomEvaluate`` wrapping an ``async def``
    evaluator. Mirrors the sync ``CustomEvaluate.score`` body — same
    AssertionError-as-fail mapping, same fail-closed default for empty
    metrics — but awaits the user function natively from the runner's
    own event loop. ``context=None`` is tolerated (defensive parity with
    the sync path)."""
    from memex_eval.suite.decorator import ScenarioContext as _SCtx

    ctx_dict = context or {}
    ctx = _SCtx(
        query=scenario.query,
        scenario=scenario,
        api=ctx_dict.get('_api'),
        vault_id=ctx_dict.get('_vault_id'),
        server_url=ctx_dict.get('_server_url', ''),
        judge=judge,
        answer=answer,
        note_key_to_unit_ids=dict(note_key_to_unit_ids or {}),
        note_id_by_key=dict(ctx_dict.get('_note_id_by_key') or {}),
        setup_context=dict(ctx_dict),
    )
    try:
        await expected.fn(ctx)
    except AssertionError as exc:
        # Preserve any metrics populated BEFORE the assertion fired
        # (e.g. ``ctx.metrics['recall'] = 0.6; assert recall >= 0.8``).
        # Stash on the exception via an attribute so the outer
        # ``_execute_scenario`` AssertionError handler can recover them
        # when building the ScenarioOutcome. Non-numeric values get
        # dropped with a warning rather than crashing the Pydantic
        # validator on outcome construction.
        partial = _coerce_numeric_metrics(ctx.metrics)
        partial['pass'] = 0.0
        exc.partial_metrics = partial  # type: ignore[attr-defined]
        logger.info('CustomEvaluate async fn raised AssertionError: %s', exc)
        raise
    if not ctx.metrics:
        logger.warning(
            'CustomEvaluate async fn for scenario %r returned without '
            'populating ctx.metrics; recording pass=0.0 (fail-closed).',
            scenario.id,
        )
        ctx.metrics['pass'] = 0.0
    return _coerce_numeric_metrics(ctx.metrics)


async def _execute_scenario(
    api: RemoteMemexAPI,
    server_url: str,
    vault_id: UUID,
    scenario: Scenario,
    suite: Suite,
    judge: Judge | None,
    note_key_to_unit_ids: dict[str, list[str]],
    note_id_by_key: dict[str, str],
    replicate_index: int,
    backend_cache: dict[str, Any] | None = None,
    defer_teardown: bool = False,
    # Each entry: (scenario, vault_id, scenario_context, base_scenario_or_none,
    # base_ctx_or_none). The two trailing slots carry the BaseScenario
    # instance and its ScenarioContext when the scenario was authored via
    # ``@suite.register_class`` AND its setup ran — drained by the suite
    # loop after the last consumer scenario completes.
    deferred_teardown_sink: 'list[Any] | None' = None,
) -> ScenarioOutcome:
    started = time.monotonic()
    answer_mode = suite.answer_mode_for(scenario)

    # Track inline note IDs ingested by THIS scenario invocation so the
    # teardown path (per-scenario or deferred) can DELETE them. Idempotent
    # replicates return an empty dict — they don't own the ingest.
    inline_note_ids: dict[str, str] = {}

    # Inline notes — ingest into the suite vault before validating gold
    # note_keys, so the validation can see the inline-note unit IDs.
    if scenario.inline_notes:
        try:
            inline_note_ids = await _ingest_inline_notes(
                api, vault_id, suite, scenario, note_key_to_unit_ids
            )
        except Exception as e:
            return ScenarioOutcome(
                scenario_id=scenario.id,
                status='error',
                metrics={},
                actual_summary={'inline_note_ingest_error': str(e)},
                duration_ms=(time.monotonic() - started) * 1000,
                error=f'Failed to ingest inline notes: {e}',
                replicate_index=replicate_index,
                answer_mode=answer_mode,
            )

    # Validate gold note_keys resolved before running.
    referenced = scenario.expected.referenced_note_keys()
    unresolved = [k for k in referenced if not note_key_to_unit_ids.get(k)]
    if unresolved:
        return ScenarioOutcome(
            scenario_id=scenario.id,
            status='error',
            metrics={},
            actual_summary={'unresolved_note_keys': unresolved},
            duration_ms=(time.monotonic() - started) * 1000,
            error=f'Ingest produced no units for note_keys={unresolved}',
            replicate_index=replicate_index,
            answer_mode=answer_mode,
        )

    # Resolve the answer backend (registered name → instance). Cached
    # per-run so backends with non-trivial setup (e.g. HermesBackend, which
    # symlinks a temp HERMES_HOME and instantiates AIAgent) don't redo
    # their work on every scenario × replicate.
    try:
        if backend_cache is not None and answer_mode in backend_cache:
            backend = backend_cache[answer_mode]
        else:
            backend = get_backend(answer_mode)
            if backend_cache is not None:
                backend_cache[answer_mode] = backend
    except KeyError as e:
        return ScenarioOutcome(
            scenario_id=scenario.id,
            status='error',
            metrics={},
            actual_summary={},
            duration_ms=(time.monotonic() - started) * 1000,
            error=str(e),
            replicate_index=replicate_index,
            answer_mode=answer_mode,
        )

    # Initialise scenario_context BEFORE the try so the finally can always
    # reach it (round-3 H-NEW-2: an exception during _run_setup_actions
    # would leave the var unbound, masking the original error).
    # Seed reserved keys the score path expects (round-1 C2 + round-2 C2):
    #   _note_id_by_key  → TemporalOrdering / NoteAttribution lookups
    #   _note_key_to_unit_ids → setup_action note_key resolution + GoldUnitIds
    scenario_context: dict[str, Any] = {
        '_note_id_by_key': note_id_by_key,
        '_note_key_to_unit_ids': note_key_to_unit_ids,
        '_executed_action_kinds': [],
        # Inline notes this scenario ingested. Populated by the inline-note
        # ingest path above; consumed by the inline-note delete teardown
        # in the finally block (or its deferred variant).
        '_inline_note_ids': dict(inline_note_ids),
        # Plumbing for CustomEvaluate (decorator-API outcomes) to reach
        # api / vault_id / server_url from a sync ``score()`` call.
        '_api': api,
        '_vault_id': vault_id,
        '_server_url': server_url,
    }
    # BaseScenario instance dispatch — set when the scenario was authored
    # via @suite.register_class. The runner calls instance.setup/act/
    # evaluate/teardown at the right points; super() inside those methods
    # reaches the existing machinery (setup_actions, backend, expected.score,
    # per-action teardowns + inline-note delete are already handled by the
    # runner around the dispatch points, so the lifecycle methods are
    # additive — see decorator.py:BaseScenario for semantics table).
    #
    # Two recovery paths:
    #  1. The instance stashed by ``to_scenario_model`` via
    #     ``object.__setattr__(sc, '_base_scenario_instance', self)``.
    #  2. A sidecar map on the legacy Suite, populated by
    #     ``decorator.Suite.build()``. This survives Pydantic
    #     re-validation / model_copy paths that drop the stashed attr.
    base_scenario = getattr(scenario, '_base_scenario_instance', None)
    if base_scenario is None:
        sidecar = getattr(suite, '_base_scenarios_by_id', None)
        if sidecar is not None:
            base_scenario = sidecar.get(scenario.id)
    base_ctx: Any = None
    base_setup_ran = False
    if base_scenario is not None:
        from memex_eval.suite.decorator import ScenarioContext as _SCtx

        base_ctx = _SCtx(
            query=scenario.query,
            scenario=scenario,
            api=api,
            vault_id=vault_id,
            server_url=server_url,
            judge=judge,
            note_key_to_unit_ids=note_key_to_unit_ids,
            note_id_by_key=note_id_by_key,
            setup_context=scenario_context,
        )
    try:
        if scenario.setup_actions:
            setup_result = await _run_setup_actions(
                api,
                vault_id,
                scenario.setup_actions,
                note_key_to_unit_ids=note_key_to_unit_ids,
            )
            scenario_context.update(setup_result)
            if scenario_context.get('_required_setup_failed'):
                return ScenarioOutcome(
                    scenario_id=scenario.id,
                    status='error',
                    metrics={},
                    actual_summary={'setup_failures': scenario_context.get('_setup_failures', [])},
                    duration_ms=(time.monotonic() - started) * 1000,
                    error='A required setup_action failed; refusing to score',
                    replicate_index=replicate_index,
                    answer_mode=answer_mode,
                )

        # BaseScenario.setup hook — runs AFTER setup_actions / inline notes
        # so subclasses can layer extra side-effects on the validated baseline.
        # Exceptions propagate to the outer try/except which converts to
        # status='error' (preserves the existing fail-loud invariant). The
        # ``base_setup_ran`` flag gates ``teardown`` so a setup that never
        # ran cannot leave teardown observing un-initialized state.
        if base_scenario is not None and base_ctx is not None:
            await base_scenario.setup(base_ctx)
            base_setup_ran = True

        # Pre-fetch the asset list for any note_key the outcome explicitly
        # requires (NoteAssetsContain — and CompositeOutcome that wraps
        # one). Direct ``GET /notes/{id}`` returns ``NoteDTO.assets``,
        # which is the server's source-of-truth list of attached files.
        # We only do this when the outcome asks for it, so vanilla
        # scenarios pay nothing extra.
        asset_note_keys = scenario.expected.note_keys_requiring_assets()
        if asset_note_keys:
            assets_by_key: dict[str, list[str]] = {}
            for nk in asset_note_keys:
                nid = note_id_by_key.get(nk)
                if not nid:
                    # Already filtered by the unresolved-note_keys check
                    # above for outcomes that use referenced_note_keys.
                    # Asset-only references (e.g. NoteAssetsContain whose
                    # note_key is NOT in referenced_note_keys for some
                    # custom outcome) get a clear empty-list breadcrumb.
                    logger.warning(
                        '  asset prefetch: note_key=%r has no resolved note_id; '
                        'NoteAssetsContain will see assets_found=0',
                        nk,
                    )
                    assets_by_key[nk] = []
                    continue
                try:
                    note_dto = await api.get_note(UUID(str(nid)))
                    assets_by_key[nk] = list(note_dto.assets or [])
                except Exception as exc:
                    logger.warning(
                        '  asset prefetch: get_note(%s, key=%r) failed: %s; '
                        'NoteAssetsContain will see assets_found=0',
                        nid,
                        nk,
                        exc,
                    )
                    assets_by_key[nk] = []
            scenario_context['_note_assets_by_key'] = assets_by_key

        # Backend produces an AgentAnswer; outcomes score against it uniformly.
        answer: AgentAnswer = await backend.answer(
            scenario, api=api, vault_id=vault_id, server_url=server_url, judge=judge
        )
        # If the backend reported an error, surface it as scenario error
        # rather than risking a false-pass on an empty answer.
        if answer.error:
            return ScenarioOutcome(
                scenario_id=scenario.id,
                status='error',
                metrics={},
                actual_summary={'backend_error': answer.error},
                duration_ms=(time.monotonic() - started) * 1000,
                error=f'backend({answer.backend_name}): {answer.error}',
                replicate_index=replicate_index,
                answer_mode=answer_mode,
            )

        # BaseScenario.act hook — receives the backend's AgentAnswer.
        # Subclasses can mutate or replace ``ctx.answer`` (e.g. multi-step
        # retrieval) before evaluation. Default is no-op (keeps backend's
        # answer unchanged). Named ``act`` to avoid colliding with the
        # ``query: str`` data field on Scenario / BaseScenario.
        if base_scenario is not None and base_ctx is not None:
            base_ctx.answer = answer
            await base_scenario.act(base_ctx)
            # Honor any answer mutation/replacement the user did.
            if base_ctx.answer is not None:
                answer = base_ctx.answer

        # Score: BaseScenario.evaluate REPLACES the direct expected.score
        # call (the default impl runs expected.score itself, so subclasses
        # that don't override or that call super() get identical behavior).
        #
        # AssertionError contract: an assertion raised inside the user
        # evaluator (BaseScenario.evaluate, @suite.scenario function body,
        # or expected.score) is the natural signal for status='fail'. The
        # narrow ``except AssertionError`` below catches ONLY user-evaluator
        # asserts; an assertion raised in setup_actions, BaseScenario.setup
        # /.act, or backend.answer falls through to the broad outer handler
        # and records status='error' — those are infrastructure phases, not
        # eval verdicts.
        no_metrics_breadcrumb: str | None = None
        try:
            if base_scenario is not None and base_ctx is not None:
                base_ctx.metrics.clear()
                await base_scenario.evaluate(base_ctx)
                # Coerce non-numeric breadcrumbs the user may have stuck
                # into ctx.metrics so they don't crash the ScenarioOutcome
                # Pydantic validator on success.
                metrics = _coerce_numeric_metrics(base_ctx.metrics)
                if not metrics:
                    # User forgot to override evaluate, OR set expected, OR
                    # populate ctx.metrics. Fail-closed with a clear breadcrumb.
                    metrics = {'pass': 0.0}
                    no_metrics_breadcrumb = (
                        f'BaseScenario.evaluate for {scenario.id!r} produced no '
                        f'metrics — did you forget to set ``self.expected`` or '
                        f'override ``evaluate`` to populate ``ctx.metrics``?'
                    )
            elif _custom_eval_has_async_fn(scenario.expected):
                # CustomEvaluate wrapping an ``async def`` evaluator —
                # bypass ``score()`` and await the function directly.
                # score() would otherwise raise RuntimeError (running event
                # loop). The decorator-API @suite.scenario async-fn path
                # lands here.
                metrics = await _invoke_custom_evaluate_async(
                    scenario.expected,
                    answer,
                    scenario,
                    note_key_to_unit_ids=note_key_to_unit_ids,
                    judge=judge,
                    context=scenario_context,
                )
            else:
                # Legacy / built-in outcome path. Coerce to numeric so a
                # third-party ``register_outcome`` subclass that returns
                # non-floats can't crash ``ScenarioOutcome`` Pydantic
                # validation downstream and erase the verdict — matches
                # the BaseScenario / CustomEvaluate paths above.
                metrics = _coerce_numeric_metrics(
                    scenario.expected.score(
                        answer,
                        scenario,
                        note_key_to_unit_ids=note_key_to_unit_ids,
                        judge=judge,
                        context=scenario_context,
                    )
                )
        except AssertionError as exc:
            # User-evaluator AssertionError → status='fail' (real eval
            # verdict, not a runner crash). Setup/act/backend asserts
            # bypass this narrow catch and reach the outer ``except
            # Exception`` below as status='error'.
            #
            # Preserve any metrics the evaluator populated BEFORE the
            # assertion fired (e.g. ``ctx.metrics['recall'] = 0.6;
            # assert recall >= 0.8``). Dropping them would erase the
            # gradient signal that makes evaluation runs useful for
            # tracking progress over time. ``pass=0.0`` is overlaid on
            # top so the status logic agrees with the verdict.
            #
            # Three sources, tried in order:
            #  1. ``exc.partial_metrics`` — set by
            #     ``_invoke_custom_evaluate_async`` for async @suite.scenario
            #     paths (the user ScenarioContext is local to that helper
            #     and not visible here).
            #  2. ``base_ctx.metrics`` — for class-based BaseScenario
            #     evaluators where the runner owns the ScenarioContext.
            #  3. Empty fallback — sync ``CustomEvaluate.score`` already
            #     handles its own preservation internally.
            preserved: dict[str, float] = {}
            partial_from_exc = getattr(exc, 'partial_metrics', None)
            if isinstance(partial_from_exc, dict):
                # ``_invoke_custom_evaluate_async`` already coerced; this
                # is a defense-in-depth pass.
                preserved.update(_coerce_numeric_metrics(partial_from_exc))
            elif base_ctx is not None:
                # Class-based evaluator path — base_ctx.metrics is the
                # raw dict the user mutated; coerce non-numeric breadcrumbs
                # before they crash the ScenarioOutcome validator.
                preserved.update(_coerce_numeric_metrics(base_ctx.metrics))
            preserved['pass'] = 0.0
            return ScenarioOutcome(
                scenario_id=scenario.id,
                status='fail',
                metrics=preserved,
                actual_summary={'assertion_failed': str(exc)},
                duration_ms=(time.monotonic() - started) * 1000,
                error=f'AssertionError: {exc}',
                replicate_index=replicate_index,
                answer_mode=answer_mode,
            )
        duration_ms = (time.monotonic() - started) * 1000

        # Determine pass/fail status from the metrics dict
        if 'pass' in metrics:
            passed = metrics['pass'] >= 1.0
        else:
            # No explicit pass — pass if any metric > 0 (e.g. recall > 0)
            passed = any(v > 0 for v in metrics.values())

        # pytest-style xfail: if the scenario declares the active answer_mode
        # as expected-failure, recolor pass→xpass and fail→xfail. Time-budget
        # violation always fails (xfail expectation does not extend to SLA).
        is_xfail_mode = answer_mode in scenario.expected_failure_modes

        # Time-budget assertion
        if scenario.max_duration_ms is not None and duration_ms > scenario.max_duration_ms:
            return ScenarioOutcome(
                scenario_id=scenario.id,
                status='fail',
                metrics={**metrics, 'pass': 0.0},
                actual_summary={'exceeded_max_duration_ms': duration_ms},
                duration_ms=duration_ms,
                error=(
                    f'Exceeded max_duration_ms: '
                    f'{duration_ms:.0f}ms > {scenario.max_duration_ms:.0f}ms'
                ),
                replicate_index=replicate_index,
                answer_mode=answer_mode,
                tokens_in=answer.tokens_in,
                tokens_out=answer.tokens_out,
                cost_usd=answer.cost_usd,
                answer_text=answer.answer_text,
                tool_calls=answer.tool_calls,
            )
        if is_xfail_mode:
            status: Literal['pass', 'fail', 'skip', 'error', 'xfail', 'xpass'] = (
                'xpass' if passed else 'xfail'
            )
            error_note = (
                f'unexpected pass: scenario marked expected_failure_modes={scenario.expected_failure_modes!r} '
                f'but passed in mode {answer_mode!r} — the constraint is wrong or the bug is fixed'
                if passed
                else None
            )
        else:
            status = 'pass' if passed else 'fail'
            error_note = no_metrics_breadcrumb

        return ScenarioOutcome(
            scenario_id=scenario.id,
            status=status,
            metrics=metrics,
            actual_summary={
                'unit_count': len(answer.units),
                'entity_count': len(answer.entities),
                'lint_findings_count': len(answer.lint_findings),
                'lint_enrichment_failures': answer.lint_enrichment_failures,
                'lint_enrichment_attempted': answer.lint_enrichment_attempted,
                'tool_call_count': len(answer.tool_calls),
                'retrieved_unit_id_count': len(answer.retrieved_unit_ids),
                'backend_error': answer.error,
            },
            duration_ms=duration_ms,
            error=error_note,
            replicate_index=replicate_index,
            answer_mode=answer_mode,
            tokens_in=answer.tokens_in,
            tokens_out=answer.tokens_out,
            cost_usd=answer.cost_usd,
            answer_text=answer.answer_text,
            tool_calls=answer.tool_calls,
        )
    except Exception as e:
        # Outer catch: setup/act/backend phase crashes (including
        # AssertionError raised in those infrastructure phases) become
        # status='error'. The narrow inner ``except AssertionError`` above
        # covers user-evaluator asserts and short-circuits before reaching
        # here.
        return ScenarioOutcome(
            scenario_id=scenario.id,
            status='error',
            metrics={},
            actual_summary={},
            duration_ms=(time.monotonic() - started) * 1000,
            error=f'{type(e).__name__}: {e}',
            replicate_index=replicate_index,
            answer_mode=answer_mode,
        )
    finally:
        # BaseScenario.teardown — runs only if the matching ``setup`` ran
        # (asymmetric setup/teardown is a classic Python lifecycle bug).
        # ALSO: when this scenario is a producer for another's
        # ``depends_on_prior_scenarios``, defer the teardown to the same
        # sink as the runner-level teardowns. Otherwise a class-based
        # dep producer (e.g. flips a feature flag in setup, restores in
        # teardown) would revert its state before the consumer scenario
        # observed it. Per-handler isolation: a raise here is logged but
        # does NOT block the runner's own teardowns from firing.
        should_defer = defer_teardown and deferred_teardown_sink is not None
        run_base_teardown_now = (
            base_scenario is not None
            and base_ctx is not None
            and base_setup_ran
            and not should_defer
        )
        if run_base_teardown_now:
            try:
                await base_scenario.teardown(base_ctx)
            except Exception as exc:
                logger.warning(
                    'BaseScenario.teardown for %r raised %s — continuing with '
                    'runner-level teardowns.',
                    scenario.id,
                    exc,
                )

        # Run teardowns immediately UNLESS this scenario is named in
        # another's ``depends_on_prior_scenarios`` — in that case the
        # caller passes ``defer_teardown=True`` and we hand the
        # context off to ``deferred_teardown_sink`` so the runner can
        # invoke teardown after the last consumer scenario has run.
        # Otherwise side-effects from this scenario would be reverted
        # before the dependent scored against them. The same defer
        # rule applies to inline-note deletes — a dependent scenario
        # may reference its parent's inline-note unit IDs.
        has_inline_notes = bool(scenario_context.get('_inline_note_ids'))
        # The deferred-teardown sink also carries the BaseScenario instance
        # (or None) so the deferred drain can call its teardown after
        # the last consumer ran — preserving setup→teardown symmetry
        # across the dependency boundary.
        deferred_payload = (
            scenario,
            vault_id,
            scenario_context,
            base_scenario if (base_setup_ran and base_scenario is not None) else None,
            base_ctx if (base_setup_ran and base_ctx is not None) else None,
        )
        if scenario.setup_actions or has_inline_notes or (should_defer and base_setup_ran):
            if should_defer:
                # Sink list is variant-typed: entries may carry the
                # optional BaseScenario teardown pair appended above.
                if deferred_teardown_sink is not None:
                    deferred_teardown_sink.append(deferred_payload)
            else:
                if scenario.setup_actions:
                    await _run_setup_teardowns(
                        api, vault_id, scenario.setup_actions, scenario_context
                    )
                if has_inline_notes:
                    await _run_inline_note_teardowns(api, scenario_context)


def _aggregate_results(
    outcomes: list[ScenarioOutcome],
) -> dict[str, float]:
    """Build the suite_metrics dict logged to MLflow."""
    runnable_outcomes = [o for o in outcomes if o.status != 'skip']
    pass_count = sum(1 for o in outcomes if o.status == 'pass')
    fail_count = sum(1 for o in outcomes if o.status == 'fail')
    error_count = sum(1 for o in outcomes if o.status == 'error')
    skip_count = sum(1 for o in outcomes if o.status == 'skip')
    xfail_count = sum(1 for o in outcomes if o.status == 'xfail')
    xpass_count = sum(1 for o in outcomes if o.status == 'xpass')

    metrics_only = [o.metrics for o in runnable_outcomes if o.metrics]
    aggregated = aggregate_metric_keys(metrics_only)

    # Latency only over outcomes that actually exercised the system.
    # Errored runs (setup-action crash, ingest fail, backend exception)
    # carry meaningless durations — including them would skew p50/p95.
    duration_outcomes = [o for o in runnable_outcomes if o.status != 'error']
    durations = [o.duration_ms for o in duration_outcomes]
    aggregated['latency_ms.p50'] = percentile(durations, 50)
    aggregated['latency_ms.p95'] = percentile(durations, 95)
    aggregated['latency_ms.mean'] = (sum(durations) / len(durations)) if durations else 0.0

    # pass_rate semantics (pytest-style):
    #   numerator   = pass + xfail   (expected outcome achieved)
    #   denominator = pass + fail + xfail + xpass  (every scenario that produced a verdict)
    # error and skip are excluded from both. xpass counts as failure because
    # the embedded constraint (expected_failure_modes) is wrong or stale.
    verdict_total = pass_count + fail_count + xfail_count + xpass_count
    aggregated['suite.pass_rate'] = (
        (pass_count + xfail_count) / verdict_total if verdict_total else 0.0
    )
    aggregated['count.scenarios'] = float(len(outcomes))
    aggregated['count.passed'] = float(pass_count)
    aggregated['count.failed'] = float(fail_count)
    aggregated['count.errored'] = float(error_count)
    aggregated['count.skipped'] = float(skip_count)
    aggregated['count.xfailed'] = float(xfail_count)
    aggregated['count.xpassed'] = float(xpass_count)
    return aggregated


# Logical directory name for the primary vault in sharded cache layout.
# Underscore prefix avoids clashing with user-declared vault_name values.
_DEFAULT_VAULT_LOGICAL = '_default'


class MultiVaultImportNotSupported(RuntimeError):
    """Raised when an explicit ``--from-snapshot <path>`` points at a
    flat single-vault V3 dump but the suite declares per-note or
    per-scenario ``vault_name`` (i.e. is multi-vault).

    Explicit paths may also point at a sharded cache slot (one with a
    ``vaults/`` subdir) — in that case the refusal does NOT fire, since
    a sharded layout can satisfy a multi-vault suite. Auto-cache always
    produces sharded layouts, so ``--from-snapshot auto`` never triggers
    this exception.
    """


def _refuse_if_multi_vault_for_snapshot(suite: Suite) -> None:
    """Raise ``MultiVaultImportNotSupported`` if the suite declares any
    secondary vault on a source note or scenario.

    Only used on the explicit-path branch — auto-cache populates and
    imports a sharded layout with one subdir per vault.
    """
    multi_vault_sources = {n.vault_name for n in suite.sources.notes if n.vault_name}
    multi_vault_scenarios = {s.vault_name for s in suite.scenarios if s.vault_name}
    if multi_vault_sources or multi_vault_scenarios:
        raise MultiVaultImportNotSupported(
            f'Suite {suite.name!r} declares per-note/per-scenario '
            f'vault_name; explicit --from-snapshot <path> handles a single '
            f'V3 dump only. Use --from-snapshot auto (which populates a '
            f'per-vault cache layout) or split the suite.'
        )


async def run_suite(
    suite: Suite,
    server_url: str,
    *,
    config_overrides: dict[str, str] | None = None,
    judge_model: str | None = None,
    replicates: int = 1,
    seed: int | None = None,
    recorder: 'MLflowRecorder | NullRecorder | None' = None,
    extra_tags: dict[str, str] | None = None,
    extra_params: dict[str, str] | None = None,
    notes: str | None = None,
    keep_vault: str | None = None,
    reuse_vault: str | None = None,
    manifest_dir: Path | None = None,
    scenario_ids: list[str] | None = None,
    groups: list[str] | None = None,
    from_snapshot: str | Path | None = None,
    reingest: bool = False,
    snapshot_cache_dir: str | Path | None = None,
) -> RunResult:
    """Run one suite end-to-end.

    Args:
        notes: Free-form description of the change being evaluated. Persisted
            on ``RunResult.notes`` and uploaded to MLflow as the
            ``run_notes.md`` artifact + a truncated ``notes`` tag for
            UI-side filtering. The full text lives in the artifact.
        keep_vault: When set, skip the vault-cleanup step at end of run and
            persist a JSON manifest at ``<manifest_dir>/<keep_vault>.json``
            so a follow-up ``--reuse-vault`` run can bind to the same
            vaults. The value is the LABEL — already validated upstream
            (CLI rejects path-traversal characters).
        reuse_vault: When set, bind to vaults named in the manifest at
            ``<manifest_dir>/<reuse_vault>.json`` and skip the
            ingest+extraction phase entirely. Setup actions still run on
            the reused vault — teardowns ensure the next scenario starts
            clean (P4). Scenarios whose setup includes any non-reusable
            handler (declared via ``reusable_under_reuse_vault = False``
            on the SetupActionHandler subclass) cannot be safely re-run
            on a reused vault and are skipped with
            skip_reason='setup_action_not_reusable'.
        manifest_dir: Directory for keep/reuse manifest files. Defaults
            to ``~/.memex/eval/keep-vault-manifests/``.
        from_snapshot: ``'auto'`` for content-hash cache lookup, OR an
            explicit server-local snapshot directory path. When a path is
            given the runner imports it into a fresh ephemeral vault and
            skips ingest + extraction-wait. ``'auto'`` looks the cache up
            via ``platformdirs`` (override with ``snapshot_cache_dir``);
            on cache miss the runner falls through to the normal ingest
            path AND populates the cache afterwards so the next run hits.
            Multi-vault suites (any source note or scenario with a
            ``vault_name``) are supported in auto mode — the cache slot
            is populated with one subdir per vault under ``vaults/`` and
            re-imported into matching vault map entries. Explicit paths
            pointing at a flat single-vault V3 dump still raise
            ``MultiVaultImportNotSupported`` for multi-vault suites; an
            explicit path pointing at a sharded cache slot is accepted.
            Mutually exclusive with ``keep_vault`` / ``reuse_vault``.
        reingest: When True with ``from_snapshot='auto'``, force the
            ingest+extract path even on a cache hit and overwrite the
            cache entry on success. Has no effect when ``from_snapshot``
            is None or an explicit path.
        snapshot_cache_dir: Override for the cache root used by
            ``from_snapshot='auto'``. Falls back to
            ``MEMEX_EVAL_SNAPSHOT_ROOT`` env, then platformdirs default.

    Logs to ``recorder`` if provided. Returns the full ``RunResult``.
    """
    from memex_eval.suite import snapshot_cache as _snapshot_cache

    # Validation order matters for clear error reporting: surface
    # programming-error mutexes (keep + reuse) BEFORE filter typos, since
    # the user can't recover from the mutex without changing the call,
    # whereas a typoed --scenario is a near-miss they can correct.
    if keep_vault is not None and reuse_vault is not None:
        raise ValueError(
            'keep_vault and reuse_vault are mutually exclusive. '
            'Reuse already implicitly preserves the vault for the next run.'
        )
    # Validate filter inputs BEFORE any network IO / vault setup so a
    # typoed --scenario / --group fails fast instead of after a vault
    # has been created and torn down. The helper itself is pure and
    # side-effect-free; the result is recomputed below to keep the
    # ordering with ingest validation but the validate-fail path here
    # short-circuits the expensive setup.
    _compute_filter_inclusion(suite.scenarios, scenario_ids, groups)
    if manifest_dir is None:
        manifest_dir = Path.home() / '.memex' / 'eval' / 'keep-vault-manifests'
    config_overrides = dict(config_overrides or {})
    extra_tags = dict(extra_tags or {})
    extra_params = dict(extra_params or {})
    actual_seed = seed if seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
    random.seed(actual_seed)
    try:
        import numpy

        numpy.random.seed(actual_seed % (2**32 - 1))
    except Exception:
        pass

    started_at = dt.datetime.now(dt.timezone.utc)
    run_id = uuid.uuid4().hex
    run_id_short = run_id[:8]
    vault_name = f'eval-suite-{suite.name}-{run_id_short}'

    # Judge setup + probe — always loaded when the suite has any
    # judge-graded scenario. The CLI flag to skip the judge was removed
    # because every important quality signal in the framework runs through
    # it; running judge-graded scenarios with the judge stubbed out
    # produces silent skips that pretend the suite passed.
    judge: Judge | None = None
    judge_model_probe: dict[str, Any] | None = None
    judge_model_value: str | None = None
    if any(isinstance(s.expected, (LLMJudge, UsefulAtK)) for s in suite.scenarios):
        try:
            judge = Judge(model=judge_model)
            judge_model_value = judge.lm.model
            with contextlib.suppress(Exception):
                judge.judge_correctness('probe', 'probe', 'probe')
            # Drain the probe call so its tokens/cost don't bill to the
            # first scenario's _absorb_judge_usage drain.
            with contextlib.suppress(Exception):
                judge.consume_usage()
            judge_kwargs = getattr(judge.lm, 'kwargs', None)
            judge_temp = (
                judge_kwargs.get('temperature', 0.0) if isinstance(judge_kwargs, dict) else 0.0
            )
            judge_model_probe = {
                'model': judge_model_value,
                'revision': _extract_judge_revision(judge.lm),
                'temperature': judge_temp,
            }
        except Exception as e:
            logger.warning('Judge unavailable: %s', e)
            judge = None

    if recorder is None:
        from memex_eval.recorders.mlflow_recorder import NullRecorder

        recorder = NullRecorder()

    # Fold scenario vault routing into the cache key so a suite that
    # adds a per-scenario vault_name without touching source content
    # doesn't silently hit a stale single-vault cache slot.
    sources_hash = suite.sources.content_hash()
    _scenario_routing = hashlib.sha256()
    for sc in sorted(suite.scenarios, key=lambda s: s.id):
        _scenario_routing.update(f'{sc.id}:{sc.vault_name or ""}\n'.encode())
    sources_hash = hashlib.sha256(
        f'{sources_hash}:{_scenario_routing.hexdigest()}'.encode()
    ).hexdigest()
    git_sha = _git_capture(['rev-parse', 'HEAD'])
    git_branch = _git_capture(['rev-parse', '--abbrev-ref', 'HEAD'])
    memex_v = _memex_version()

    outcomes: list[ScenarioOutcome] = []
    note_key_to_unit_ids: dict[str, list[str]] = {}
    config_snapshot: dict[str, Any] = {}
    embedding_model = ''
    reranker_model = ''
    # round-6 C1: only write the keep-vault manifest after the scenario loop
    # exits cleanly. A KeyboardInterrupt mid-extraction would otherwise
    # persist a manifest pointing at an under-populated vault, causing the
    # next --reuse-vault run to silently false-fail when the (incomplete)
    # extraction's missing-units check passes by coincidence.
    run_completed = False

    try:
        async with httpx.AsyncClient(base_url=server_url, timeout=180.0) as client:
            api = RemoteMemexAPI(client)

            # Capture config snapshot (best-effort — admin auth may block)
            config_snapshot_available = False
            nli_available: bool | None = None  # None = config unavailable
            try:
                config_snapshot = await api.get_system_config()
                config_snapshot_available = True
                # Resolve embedding/reranker model identity from snapshot.
                # Real config paths: server.embedding_model and
                # server.memory.retrieval.reranker.
                emb = config_snapshot.get('server', {}).get('embedding_model') or {}
                rer = (
                    config_snapshot.get('server', {})
                    .get('memory', {})
                    .get('retrieval', {})
                    .get('reranker', {})
                )
                embedding_model = str(emb.get('model') or emb.get('type') or '')
                reranker_model = str(rer.get('model') or rer.get('type') or '')
                # P7: determine NLI availability for requires_nli_classifier gating.
                pol = (
                    config_snapshot.get('server', {})
                    .get('memory', {})
                    .get('lint_llm', {})
                    .get('polarity', {})
                )
                lint_llm = config_snapshot.get('server', {}).get('memory', {}).get('lint_llm', {})
                pol_enabled = bool(pol.get('enabled', True))
                lint_llm_enabled = bool(lint_llm.get('enabled', True))
                backend_type = (pol.get('backend') or {}).get('type', 'onnx')
                nli_available = lint_llm_enabled and pol_enabled and backend_type != 'disabled'
            except Exception as e:
                logger.warning('Could not fetch /system/config: %s', e)

            # Vault setup. Four paths:
            #   (a) --reuse-vault: bind to vaults from a prior --keep-vault
            #       manifest, skipping ingest + extraction.
            #   (b) --from-snapshot=<path>: import a V3 snapshot into a
            #       fresh vault. Multi-vault suites unsupported.
            #   (c) --from-snapshot=auto: cache lookup on
            #       (suite_name, sources_hash). Hit (and not --reingest)
            #       → import; miss/reingest → ingest path + populate the
            #       cache from the post-extraction baseline (BEFORE
            #       scenarios run, so scenario-side mutations don't
            #       contaminate the cached vault).
            #   (d) default: create vault(s) and ingest sources as today.
            vault_map: dict[str | None, UUID] = {}
            note_id_by_key: dict[str, str] = {}

            # Resolve auto cache lookup before deciding the path.
            cache_lookup: '_snapshot_cache.CacheLookup | None' = None
            if from_snapshot == 'auto' and reuse_vault is None:
                cache_root = _snapshot_cache.resolve_cache_root(snapshot_cache_dir)
                cache_lookup = _snapshot_cache.lookup(cache_root, suite.name, sources_hash)
                logger.info(
                    'Snapshot cache lookup: root=%s key=%s hit=%s reingest=%s',
                    cache_lookup.cache_root,
                    cache_lookup.cache_path.name,
                    cache_lookup.hit,
                    reingest,
                )

            use_import = (
                from_snapshot is not None and from_snapshot != 'auto' and reuse_vault is None
            ) or (cache_lookup is not None and cache_lookup.hit and not reingest)

            # Auto cache hit on a legacy-flat slot for a suite that has
            # since become multi-vault → clear the stale slot and fall
            # through to the ingest path so the cache repopulates in the
            # new sharded layout. Avoids a permanent failure loop where
            # every subsequent run refuses on the same stale cache entry.
            if (
                cache_lookup is not None
                and cache_lookup.hit
                and not reingest
                and not (cache_lookup.cache_path / 'vaults').is_dir()
            ):
                _is_multi = any(n.vault_name for n in suite.sources.notes) or any(
                    s.vault_name for s in suite.scenarios
                )
                if _is_multi:
                    logger.warning(
                        'Snapshot cache slot %s is legacy-flat single-vault but suite '
                        '%r is now multi-vault; clearing cache and falling through to '
                        'ingest+extract (cache will repopulate in sharded layout).',
                        cache_lookup.cache_path,
                        suite.name,
                    )
                    _snapshot_cache.clear_cache_entry(cache_lookup.cache_path)
                    cache_lookup = _snapshot_cache.CacheLookup(
                        cache_root=cache_lookup.cache_root,
                        cache_path=cache_lookup.cache_path,
                        hit=False,
                    )
                    use_import = False

            if reuse_vault is not None:
                manifest_path = manifest_dir / f'{reuse_vault}.json'
                if not manifest_path.exists():
                    raise FileNotFoundError(
                        f'Reuse manifest not found: {manifest_path}. '
                        f'Did you pass --keep-vault on a prior run?'
                    )
                manifest = json.loads(manifest_path.read_text())
                primary_name = manifest['primary_vault']['name']
                primary_id = UUID(manifest['primary_vault']['id'])
                # Verify the vault still exists on the server.
                existing = await api.list_vaults()
                existing_ids = {str(v.id) for v in existing}
                if str(primary_id) not in existing_ids:
                    raise FileNotFoundError(
                        f'Manifest {manifest_path} names vault {primary_id!r} '
                        f'which no longer exists on the server. Drop the manifest '
                        f'and re-run without --reuse-vault.'
                    )
                default_vault_id = primary_id
                vault_name = primary_name
                vault_map[None] = default_vault_id
                for sec_name, sec_data in (manifest.get('secondary_vaults') or {}).items():
                    sec_id = UUID(sec_data['id'])
                    if str(sec_id) not in existing_ids:
                        raise FileNotFoundError(
                            f'Manifest {manifest_path} names secondary vault '
                            f'{sec_id!r} ({sec_name!r}) which no longer exists.'
                        )
                    vault_map[sec_name] = sec_id
                logger.info(
                    'Reusing vault %r (%s) from manifest %s',
                    vault_name,
                    default_vault_id,
                    manifest_path,
                )
            elif use_import:
                snapshot_path: str = (
                    str(cache_lookup.cache_path)
                    if cache_lookup is not None and cache_lookup.hit
                    else str(from_snapshot)
                )
                # Multi-vault aware: cache slots ship per-vault subdirs under
                # ``vaults/``. Explicit V3 paths are flat single-vault dumps —
                # in that mode the suite must be single-vault. (Legacy-flat
                # cache hits for multi-vault suites are cleared upstream
                # before reaching this branch; see H1 in adversarial review.)
                vaults_root = Path(snapshot_path) / 'vaults'
                is_sharded = vaults_root.is_dir()
                if not is_sharded:
                    _refuse_if_multi_vault_for_snapshot(suite)
                logger.info(
                    'Importing snapshot %s into target vault %s (skipping ingest + extraction)',
                    snapshot_path,
                    vault_name,
                )
                from memex_eval.snapshot import SnapshotImporter
                from memex_eval.snapshot.runtime import (
                    check_runtime_matches_server,
                    snapshot_runtime,
                )

                # Refuse early if eval-side env doesn't match the
                # running server (non-localhost target, or embedding-
                # model divergence).
                check_runtime_matches_server(server_url, config_snapshot)

                _import_ids: list[str] = []
                if is_sharded:
                    # vaults/_default/ + vaults/<name>/ ... import each in
                    # its own snapshot_runtime() so each gets its own DB
                    # session + advisory-lock pair.
                    default_dir = vaults_root / _DEFAULT_VAULT_LOGICAL
                    if not default_dir.is_dir():
                        raise FileNotFoundError(
                            f'Sharded snapshot at {snapshot_path} is missing the '
                            f'required {_DEFAULT_VAULT_LOGICAL!r} vault subdir.'
                        )
                    async with snapshot_runtime() as rt:
                        importer = SnapshotImporter(
                            session=rt.session,
                            filestore=rt.filestore,
                            embedding_backend=rt.config.server.embedding_model,
                            snapshot_dir=default_dir,
                            target_vault_name=vault_name,
                        )
                        default_vault_id = await importer.import_snapshot()
                        _import_ids.append(str(importer.import_id))
                    vault_map[None] = default_vault_id
                    for sub in sorted(vaults_root.iterdir()):
                        if not sub.is_dir() or sub.name == _DEFAULT_VAULT_LOGICAL:
                            continue
                        logical = sub.name
                        async with snapshot_runtime() as rt:
                            importer = SnapshotImporter(
                                session=rt.session,
                                filestore=rt.filestore,
                                embedding_backend=rt.config.server.embedding_model,
                                snapshot_dir=sub,
                                target_vault_name=f'{vault_name}-{logical}',
                            )
                            vault_map[logical] = await importer.import_snapshot()
                            _import_ids.append(str(importer.import_id))
                else:
                    async with snapshot_runtime() as rt:
                        importer = SnapshotImporter(
                            session=rt.session,
                            filestore=rt.filestore,
                            embedding_backend=rt.config.server.embedding_model,
                            snapshot_dir=Path(snapshot_path),
                            target_vault_name=vault_name,
                        )
                        default_vault_id = await importer.import_snapshot()
                        _import_ids.append(str(importer.import_id))
                    vault_map[None] = default_vault_id
                extra_params['snapshot.path'] = snapshot_path
                # MLflow caps param values at 250 chars; one UUID per
                # vault (~37 bytes incl. comma) → safe up to ~6 vaults.
                # Beyond that, record the first id + the count to stay
                # under the limit while keeping the value parseable.
                if len(_import_ids) <= 6:
                    extra_params['snapshot.import_id'] = ','.join(_import_ids)
                else:
                    extra_params['snapshot.import_id'] = (
                        f'{_import_ids[0]},+{len(_import_ids) - 1}-more'
                    )
                extra_params['snapshot.import_count'] = str(len(_import_ids))
                extra_params['snapshot.cache_hit'] = 'true'
            else:
                default_vault_id = await _setup_vault(
                    api, vault_name, f'Eval suite vault: {suite.name}'
                )
                vault_map[None] = default_vault_id
                extra_vault_names = {n.vault_name for n in suite.sources.notes if n.vault_name}
                extra_vault_names |= {s.vault_name for s in suite.scenarios if s.vault_name}
                for name in extra_vault_names:
                    if name is None:
                        continue
                    vault_map[name] = await _setup_vault(
                        api, f'{vault_name}-{name}', f'Eval extra vault: {name}'
                    )
                # Note that we'll be on the ingest path; record cache state
                # for MLflow regardless of populate success.
                if cache_lookup is not None:
                    extra_params['snapshot.cache_hit'] = 'false'

            # Per-run backend cache. Backends with non-trivial setup
            # (HermesBackend) are reused across scenarios; teardown happens
            # in the finally block below.
            backend_cache: dict[str, Any] = {}

            try:
                if reuse_vault is not None:
                    # P8: skip ingest+extraction. Resolve the existing notes
                    # back to source note_keys by matching on the per-note
                    # display ``name`` — that's the field _ingest_sources
                    # writes from ``note.title or note.note_key`` and the
                    # only stable handle ``NoteListItemDTO`` exposes
                    # (it omits the wire ``note_key``).
                    note_id_by_key = {}
                    # Walk every vault the suite uses. ``vault_map`` already
                    # contains ``default_vault_id`` under key ``None`` (set
                    # during vault setup); ``set(...)`` dedupes if a
                    # secondary vault happens to alias the default.
                    # round-6 H2: collect ids per name as a list so duplicate
                    # display names raise loudly instead of silently mapping
                    # both source notes to whichever id wins the dict update.
                    notes_by_name_per_vault: dict[UUID, dict[str, list[str]]] = {}
                    for vid in set(vault_map.values()):
                        try:
                            # Server caps limit at 500 (server/notes.py: le=500).
                            # Suites with >500 source notes will need pagination;
                            # current suites are well under this.
                            rows = await api.list_notes(vault_id=vid, limit=500)
                        except Exception as e:
                            logger.warning('list_notes failed for vault %s: %s', vid, e)
                            rows = []
                        per_vault: dict[str, list[str]] = {}
                        for n in rows:
                            # Canonicalise so reuse-mode lookup matches the
                            # fresh-ingest path's case/whitespace handling
                            # (round-3 MEDIUM 1).
                            nm = canonicalize_name(getattr(n, 'name', None) or '')
                            per_vault.setdefault(nm, []).append(str(n.id))
                        notes_by_name_per_vault[vid] = per_vault
                    missing: list[str] = []
                    ambiguous: list[str] = []
                    for src in suite.sources.notes:
                        target_vault_id = vault_map.get(src.vault_name, default_vault_id)
                        lookup_name = canonicalize_name(src.title or src.note_key)
                        candidates = notes_by_name_per_vault.get(target_vault_id, {}).get(
                            lookup_name, []
                        )
                        if not candidates:
                            missing.append(src.note_key)
                            continue
                        if len(candidates) > 1:
                            ambiguous.append(src.note_key)
                            continue
                        note_id_by_key[src.note_key] = candidates[0]
                    if missing:
                        raise ValueError(
                            f'Reused vault is missing expected notes: {missing}. '
                            f'Re-run without --reuse-vault to ingest fresh.'
                        )
                    if ambiguous:
                        raise ValueError(
                            f'Reused vault has multiple notes sharing a display '
                            f'name for source note_keys {ambiguous}. The reuse '
                            f'lookup matches by ``note.title or note.note_key`` '
                            f'(NoteListItemDTO does not expose the wire note_key); '
                            f'duplicate names cannot be resolved unambiguously. '
                            f'Give each source note a unique title, or drop '
                            f'--reuse-vault and re-ingest fresh.'
                        )
                elif use_import:
                    # Snapshot path: extraction is pre-baked.
                    note_id_by_key = {}
                else:
                    # Ingest source notes
                    note_id_by_key = await _ingest_sources(api, default_vault_id, vault_map, suite)

                    # Wait for extraction (vault-wide stable signal first). Skip
                    # entirely when no notes were ingested — otherwise we burn the
                    # full retry budget on a vault that has nothing to extract.
                    if note_id_by_key:
                        with contextlib.suppress(Exception):
                            await wait_for_extraction(
                                api,
                                default_vault_id,
                                poll_interval=2.0,
                                poll_timeout=120.0,
                                stable_ticks_required=2,
                                max_consecutive_errors=5,
                            )

                if use_import:
                    # Snapshot path: scenarios that depend on per-note unit IDs
                    # via the snapshot path are out of scope for v1 (the
                    # MultiVaultImportNotSupported check above already filters
                    # most of these).
                    note_key_to_unit_ids = {}
                else:
                    # Build per-note vault map: each note_key polls under its
                    # actual target vault, not blindly under default_vault_id —
                    # otherwise notes routed to a non-default vault timeout
                    # spuriously even though extraction succeeded.
                    note_key_to_vault_id: dict[str, UUID] = {
                        n.note_key: vault_map.get(n.vault_name, default_vault_id)
                        for n in suite.sources.notes
                        if n.note_key in note_id_by_key
                    }
                    # Per-note unit-id resolution (also serves as per-note extraction wait)
                    note_key_to_unit_ids = await _wait_extraction_per_note(
                        api, note_id_by_key, note_key_to_vault_id
                    )

                    # Cache-populate IMMEDIATELY after extraction completes,
                    # BEFORE scenarios run. Scenarios mutate vault state
                    # (KV writes, inline-note ingestion, contradiction
                    # links) — the cache must capture the clean post-
                    # extraction baseline, not post-scenario state.
                    # Atomic publish via tmp-dir + rename so a partial
                    # populate never replaces a known-good entry.
                    # Export runs in-process via SnapshotExporter against
                    # the same DB the server uses (no HTTP round-trip).
                    if cache_lookup is not None:
                        staged = _snapshot_cache.stage_path(
                            cache_lookup.cache_root,
                            cache_lookup.cache_path.name,
                        )
                        try:
                            from memex_core.services.snapshot import (
                                SnapshotExporter,
                            )
                            from memex_eval.snapshot.runtime import (
                                build_embedding_identity,
                                check_runtime_matches_server,
                                snapshot_runtime,
                            )

                            check_runtime_matches_server(server_url, config_snapshot)
                            # Sharded layout: one subdir per vault under
                            # vaults/. _default holds the primary vault;
                            # named vaults use their declared vault_name.
                            #
                            # Each export gets its OWN snapshot_runtime():
                            # SnapshotExporter issues SET TRANSACTION
                            # ISOLATION LEVEL REPEATABLE READ READ ONLY,
                            # which Postgres only accepts as the first
                            # statement of a transaction. Sharing one
                            # session across exports trips SQLSTATE 25001
                            # on the second vault — verified in adversarial
                            # review C1. Per-export runtime sidesteps it
                            # cleanly and isolates each subdir's writes.
                            assert None in vault_map, (
                                'populate invariant: primary (None-keyed) vault must be present in '
                                f'vault_map; got keys {list(vault_map.keys())!r}'
                            )
                            vaults_dir = staged / 'vaults'
                            vaults_dir.mkdir(parents=True, exist_ok=True)
                            for vault_logical, vid in sorted(
                                vault_map.items(), key=lambda kv: '' if kv[0] is None else kv[0]
                            ):
                                sub = vaults_dir / (
                                    _DEFAULT_VAULT_LOGICAL
                                    if vault_logical is None
                                    else vault_logical
                                )
                                sub.mkdir(parents=True, exist_ok=True)
                                async with snapshot_runtime() as rt:
                                    identity = build_embedding_identity(rt.config)
                                    exporter = SnapshotExporter(
                                        session=rt.session,
                                        filestore=rt.filestore,
                                        vault_id_or_name=vid,
                                        output_dir=sub,
                                        embedding_model=identity,
                                    )
                                    await exporter.export()
                            _snapshot_cache.mark_complete(staged)
                            _snapshot_cache.publish(staged, cache_lookup.cache_path)
                            extra_params['snapshot.cache_populated'] = 'true'
                            extra_params['snapshot.path'] = str(cache_lookup.cache_path)
                            logger.info(
                                'Snapshot cache populated at %s (%d vault(s))',
                                cache_lookup.cache_path,
                                len(vault_map),
                            )
                        except Exception as e:
                            logger.warning('Snapshot cache populate failed: %s', e)
                            extra_params['snapshot.cache_populated'] = 'false'
                            _snapshot_cache.discard_staged(staged)

                # Run scenarios in DEFINITION ORDER. The runner is contractually
                # required to iterate suite.scenarios in the same order they
                # appear in SCENARIOS = [...]; downstream scenarios may rely on
                # state mutations from earlier scenarios (recorded outcomes,
                # deprioritization, KV writes, inline-note contradictions).
                # Guarded by tests/suite/test_extensibility.py.
                scenarios_by_id = {s.id: s for s in suite.scenarios}

                def _own_skip_reason(sc: Scenario) -> str | None:
                    return _compute_own_skip_reason(
                        sc,
                        suite=suite,
                        reuse_vault=reuse_vault,
                        config_snapshot_available=config_snapshot_available,
                        nli_available=nli_available,
                    )

                # Scenarios named in another's ``depends_on_prior_scenarios``
                # have their teardown deferred until after the last consumer
                # runs — the consumer's assertion observes side effects the
                # dep stamped, so reverting them at the dep's own teardown
                # phase would defeat the dependency contract.
                deps_referenced: set[str] = {
                    dep for s in suite.scenarios for dep in s.depends_on_prior_scenarios
                }
                # Each tuple: (scenario, vault_id, scenario_context,
                # base_scenario_or_none, base_ctx_or_none). Variant-typed
                # because the BaseScenario slots are populated only for
                # decorator-API class-based scenarios whose setup ran.
                deferred_teardowns: list[Any] = []

                # ``scenario_ids`` / ``groups`` filtering. Both filters
                # walk ``depends_on_prior_scenarios`` so a consumer's
                # prerequisites still execute. When both are set the
                # result is the intersection. Validation (unknown ids /
                # unknown groups) happens here; the helper raises before
                # any further side effects.
                included_ids = _compute_filter_inclusion(suite.scenarios, scenario_ids, groups)
                if included_ids is not None:
                    logger.info(
                        'filter: scenario_ids=%s, groups=%s → running=%s',
                        sorted(scenario_ids or []),
                        sorted(groups or []),
                        sorted(included_ids),
                    )

                for scenario in suite.scenarios:
                    if included_ids is not None and scenario.id not in included_ids:
                        outcomes.append(
                            ScenarioOutcome(
                                scenario_id=scenario.id,
                                status='skip',
                                skip_reason='filtered_out',
                                replicate_index=0,
                                answer_mode=suite.answer_mode_for(scenario),
                            )
                        )
                        continue
                    sc_vault_id = vault_map.get(scenario.vault_name, default_vault_id)
                    # P7: NLI gate — applies suite-level OR per-scenario flag.
                    needs_nli = (
                        suite.metadata.requires_nli_classifier or scenario.requires_nli_classifier
                    )
                    # Own skip reasons. P8 + round-6 H4: a handler declares
                    # ``reusable_under_reuse_vault = False`` when it has
                    # unbounded write-side effects (e.g. ``record_outcome``
                    # appends a history entry per call, biasing retrieval
                    # scoring). Skip scenarios whose setup contains any such
                    # action; let the rest of the suite run.
                    own_skip = _own_skip_reason(scenario)
                    # Transitive: a scenario observing state stamped by a
                    # prior scenario inherits that prior's skip reason.
                    # Generalised across all skip kinds (reuse, NLI,
                    # future budget gates) — the round-2 review found that
                    # a reuse-only check left NLI-disabled deps unprotected.
                    transitive_skip_dep: str | None = None
                    transitive_skip_reason: str | None = None
                    if scenario.depends_on_prior_scenarios:
                        for dep_id in scenario.depends_on_prior_scenarios:
                            dep = scenarios_by_id.get(dep_id)
                            if dep is None:
                                continue
                            dep_skip = _own_skip_reason(dep)
                            if dep_skip is not None:
                                transitive_skip_dep = dep_id
                                transitive_skip_reason = dep_skip
                                break
                    for replicate in range(replicates):
                        if own_skip == 'setup_action_not_reusable':
                            outcomes.append(
                                ScenarioOutcome(
                                    scenario_id=scenario.id,
                                    status='skip',
                                    skip_reason='setup_action_not_reusable',
                                    replicate_index=replicate,
                                    answer_mode=suite.answer_mode_for(scenario),
                                )
                            )
                            continue
                        if transitive_skip_dep is not None:
                            outcomes.append(
                                ScenarioOutcome(
                                    scenario_id=scenario.id,
                                    status='skip',
                                    skip_reason=(
                                        f'depends_on_prior_scenario_skipped:'
                                        f'{transitive_skip_dep}:{transitive_skip_reason}'
                                    ),
                                    replicate_index=replicate,
                                    answer_mode=suite.answer_mode_for(scenario),
                                )
                            )
                            continue
                        if own_skip == 'nli_disabled':
                            outcomes.append(
                                ScenarioOutcome(
                                    scenario_id=scenario.id,
                                    status='skip',
                                    skip_reason='nli_disabled',
                                    replicate_index=replicate,
                                    answer_mode=suite.answer_mode_for(scenario),
                                )
                            )
                            continue
                        if needs_nli and not config_snapshot_available:
                            # Cannot determine NLI availability — admin auth missing.
                            # Run the scenario anyway: skip-on-unknown produced
                            # silent gaps in eval coverage, and a real NLI-disabled
                            # server will fail the scenario with a clearer signal
                            # (no findings emitted) than a skip.
                            logger.warning(
                                'Running %r without NLI gate: /system/config '
                                'unavailable (admin auth missing). If NLI is '
                                'actually disabled the scenario will fail; pass '
                                'an admin api-key for a proper skip.',
                                scenario.id,
                            )
                        outcome = await _execute_scenario(
                            api,
                            server_url,
                            sc_vault_id,
                            scenario,
                            suite,
                            judge,
                            note_key_to_unit_ids,
                            note_id_by_key,
                            replicate,
                            backend_cache=backend_cache,
                            defer_teardown=scenario.id in deps_referenced,
                            deferred_teardown_sink=deferred_teardowns,
                        )
                        outcomes.append(outcome)
                # All scenarios complete — fire any deferred teardowns
                # whose execution we postponed because their side effects
                # were observed by a later ``depends_on_prior_scenarios``
                # consumer. Running these in reverse declaration order so
                # earlier-stamped state is undone last. Each entry carries
                # an optional BaseScenario teardown to drain too — that
                # also had to be deferred so dep producers' class-level
                # cleanup (e.g. flag-flip restore) runs after consumers.
                for entry in reversed(deferred_teardowns):
                    dep_sc = entry[0]
                    dep_vault = entry[1]
                    dep_ctx = entry[2]
                    dep_base_scenario = entry[3] if len(entry) > 3 else None
                    dep_base_ctx = entry[4] if len(entry) > 4 else None
                    try:
                        # BaseScenario.teardown first — symmetric to its
                        # immediate-path counterpart which runs BEFORE the
                        # runner-level teardowns.
                        if dep_base_scenario is not None and dep_base_ctx is not None:
                            try:
                                await dep_base_scenario.teardown(dep_base_ctx)
                            except Exception as exc:
                                logger.warning(
                                    'Deferred BaseScenario.teardown for '
                                    'scenario %r raised %s — continuing.',
                                    dep_sc.id,
                                    exc,
                                )
                        if dep_sc.setup_actions:
                            await _run_setup_teardowns(
                                api, dep_vault, dep_sc.setup_actions, dep_ctx
                            )
                        if dep_ctx.get('_inline_note_ids'):
                            await _run_inline_note_teardowns(api, dep_ctx)
                    except Exception as exc:
                        logger.warning(
                            'Deferred teardown for scenario %r raised %s — '
                            'continuing. Vault may carry stale state.',
                            dep_sc.id,
                            exc,
                        )
                # round-6 C1: every scenario in the loop completed (no
                # KeyboardInterrupt, no abort). Safe to write the keep-vault
                # manifest.
                run_completed = True
            finally:
                # Tear down cached backends (frees temp HERMES_HOME etc.)
                # before deleting the vaults — the backends may try one
                # last cleanup call into the vault.
                for backend in backend_cache.values():
                    teardown = getattr(backend, 'close', None) or getattr(backend, 'shutdown', None)
                    if callable(teardown):
                        with contextlib.suppress(Exception):
                            teardown()
                # P8: keep the vault for follow-up reuse runs. Persist a
                # manifest so a follow-up ``--reuse-vault <label>`` run can
                # bind the same primary + secondary vaults by name+id.
                # Reuse mode also implicitly keeps the vault (we don't want
                # to delete the vault someone is actively reusing).
                preserve_vaults = keep_vault is not None or reuse_vault is not None
                # round-6 C1: only write the manifest if the scenario loop
                # completed cleanly. KeyboardInterrupt / unexpected raise
                # leaves run_completed=False; we still preserve the vault
                # (so the user can clean it up manually) but do NOT advertise
                # it via a manifest, because reuse against a partial vault
                # would mask incomplete extraction as silent false-fails.
                if keep_vault is not None and run_completed:
                    try:
                        manifest_dir.mkdir(parents=True, exist_ok=True)
                        secondary = {
                            name: {'id': str(vid), 'name': f'{vault_name}-{name}'}
                            for name, vid in vault_map.items()
                            if name is not None
                        }
                        manifest_payload = {
                            'label': keep_vault,
                            'suite_name': suite.name,
                            'suite_version': suite.metadata.suite_version,
                            'sources_hash': sources_hash,
                            'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                            'primary_vault': {
                                'id': str(default_vault_id),
                                'name': vault_name,
                            },
                            'secondary_vaults': secondary,
                        }
                        manifest_path = manifest_dir / f'{keep_vault}.json'
                        manifest_path.write_text(json.dumps(manifest_payload, indent=2))
                        logger.info(
                            'Kept vault %r — wrote manifest %s. '
                            'Run again with --reuse-vault %s to bind to it.',
                            vault_name,
                            manifest_path,
                            keep_vault,
                        )
                    except Exception as e:
                        # round-6 C1: manifest write failure must NOT delete
                        # the vault. The user explicitly asked to keep it,
                        # and forcing cleanup-on-disk-full would destroy
                        # work the user wanted preserved. Preserve the vault
                        # and surface the error; the user can re-run
                        # --keep-vault, or write the manifest by hand from
                        # the vault id we logged.
                        logger.error(
                            'Failed to write keep-vault manifest %s: %s. '
                            'Vault %s (%s) is preserved; manifest can be '
                            'reconstructed manually.',
                            keep_vault,
                            e,
                            vault_name,
                            default_vault_id,
                        )
                elif keep_vault is not None and not run_completed:
                    logger.warning(
                        'Run did not complete cleanly; --keep-vault manifest '
                        'NOT written to avoid pointing reuse runs at a '
                        'partially-populated vault. Vault %s (%s) is '
                        'preserved for manual inspection.',
                        vault_name,
                        default_vault_id,
                    )
                if not preserve_vaults:
                    # Best-effort cleanup: delete the temp vault(s) so we don't leak state.
                    with contextlib.suppress(Exception):
                        await api.delete_vault(default_vault_id)
                    for name, vid in vault_map.items():
                        if name is None:
                            continue
                        with contextlib.suppress(Exception):
                            await api.delete_vault(vid)
    except KeyboardInterrupt:
        logger.warning('Run interrupted by user')
        raise

    finished_at = dt.datetime.now(dt.timezone.utc)
    suite_metrics = _aggregate_results(outcomes)
    answer_modes_used = sorted({o.answer_mode for o in outcomes if o.answer_mode})
    # Aggregate cost/tokens for agent-mode runs (zero for direct API).
    suite_metrics['cost.total_usd'] = sum(o.cost_usd for o in outcomes)
    suite_metrics['tokens.total_in'] = float(sum(o.tokens_in for o in outcomes))
    suite_metrics['tokens.total_out'] = float(sum(o.tokens_out for o in outcomes))

    result = RunResult(
        suite_name=suite.name,
        suite_version=suite.metadata.suite_version,
        schema_version=suite.metadata.schema_version,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        config_snapshot=config_snapshot,
        config_overrides=config_overrides,
        sources_hash=sources_hash,
        git_sha=git_sha,
        git_branch=git_branch,
        memex_version=memex_v,
        judge_model=judge_model_value,
        judge_model_probe=judge_model_probe,
        seed=actual_seed,
        embedding_model=embedding_model,
        reranker_model=reranker_model,
        vault_name=vault_name,
        answer_modes=answer_modes_used,
        replicates=replicates,
        notes=notes,
        scenario_outcomes=outcomes,
        suite_metrics=suite_metrics,
        note_key_to_unit_ids=note_key_to_unit_ids,
    )

    # MLflow logging
    _log_to_recorder(result, suite, recorder, extra_tags, extra_params)
    return result


def _log_to_recorder(
    result: RunResult,
    suite: Suite,
    recorder: 'MLflowRecorder | NullRecorder',
    extra_tags: dict[str, str],
    extra_params: dict[str, str],
) -> None:
    import json
    import os
    import tempfile

    # Build params per the §6 schema
    base_params: dict[str, Any] = {
        'suite.name': result.suite_name,
        'suite.version': result.suite_version,
        'suite.schema_version': result.schema_version,
        'suite.sources_hash': result.sources_hash,
        'suite.answer_modes': ','.join(result.answer_modes) or 'api',
        'git.sha': result.git_sha,
        'git.branch': result.git_branch,
        'memex.version': result.memex_version,
        'release.version': os.environ.get('MEMEX_RELEASE_VERSION', ''),
        'judge.model': result.judge_model or '',
        'judge.model_revision': (result.judge_model_probe or {}).get('revision') or '',
        'judge.temperature': str((result.judge_model_probe or {}).get('temperature') or 0.0),
        'embedding.model_id': result.embedding_model,
        'reranker.model_id': result.reranker_model,
        'seed': str(result.seed),
        'replicates': str(result.replicates),
        'vault.name': result.vault_name,
    }
    base_params = {k: str(v) for k, v in base_params.items() if v not in (None, '')}

    # Knob params (allowlist from suite.metadata.knobs)
    knob_params: dict[str, str] = {}
    for knob in suite.metadata.knobs[:30]:
        # Resolve value from config_snapshot (dotted-path lookup)
        value = result.config_snapshot
        try:
            for part in knob.split('.'):
                value = value[part]
            knob_params[f'knob.{knob}'] = str(value)
        except (KeyError, TypeError):
            knob_params[f'knob.{knob}'] = '<unresolved>'

    override_params = {
        f'override.{k}': str(v) for k, v in list(result.config_overrides.items())[:20]
    }

    tags: dict[str, str] = {
        'suite.name': result.suite_name,
        'schema_version': result.schema_version,
        'suite.tags': ','.join(suite.metadata.tags),
        'components': ','.join(suite.metadata.components_under_test),
        **extra_tags,
    }

    # User-supplied change-description notes — short summary as a tag (for
    # UI filtering), full body uploaded as an artifact below. MLflow tag
    # values cap at 5000 chars; we keep it well under for readability.
    notes_tag = _build_notes_tag(result.notes)
    if notes_tag:
        tags['notes'] = notes_tag

    # mlflow.parentRunId belongs in tags, not params — when a caller passes
    # extra_params={'mlflow.parentRunId': <id>} (e.g. for grouping multiple
    # related runs in the MLflow UI), route it to tags so the SQL backend
    # registers the parent/child relationship correctly.
    parent_run_id = extra_params.pop('mlflow.parentRunId', None)
    is_nested = bool(parent_run_id)
    if parent_run_id:
        tags['mlflow.parentRunId'] = parent_run_id

    all_params = {**base_params, **knob_params, **override_params, **extra_params}

    metrics = {k: float(v) for k, v in result.suite_metrics.items() if isinstance(v, (int, float))}

    start_kwargs: dict[str, Any] = {'tags': tags}
    if is_nested:
        start_kwargs['nested'] = True
    recorder.start_run(**start_kwargs)
    try:
        recorder.log_params(all_params)
        for k, tag_v in tags.items():
            with contextlib.suppress(Exception):
                if hasattr(recorder, 'set_tag'):
                    recorder.set_tag(k, tag_v)
        recorder.log_metrics(metrics, step=0)

        # Artifacts
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            run_result_path = tmpdir / 'run_result.json'
            # Pydantic's mode='json' handles UUID/datetime/Path automatically;
            # ``default=`` is a json.dumps kwarg, not model_dump_json's.
            run_result_path.write_text(result.model_dump_json(indent=2))
            recorder.log_artifact(run_result_path)

            # Defense-in-depth: re-redact even though the server already did.
            # If a self-hosted memex is older / lacks redaction, we still avoid leaking secrets.
            from memex_common.redaction import redact

            cfg_path = tmpdir / 'config_snapshot.json'
            cfg_path.write_text(json.dumps(redact(result.config_snapshot), indent=2, default=str))
            recorder.log_artifact(cfg_path)

            if suite.readme_path and suite.readme_path.is_file():
                with contextlib.suppress(Exception):
                    recorder.log_artifact(suite.readme_path)

            # User-supplied change notes — full text as its own artifact.
            if result.notes and result.notes.strip():
                notes_path = tmpdir / 'run_notes.md'
                notes_path.write_text(result.notes)
                recorder.log_artifact(notes_path)

            # Snapshot the source notes (and any binary assets) so a 6-month-old
            # run is reproducible from the artifact alone — plan §6.
            with contextlib.suppress(Exception):
                snapshot_dir = tmpdir / 'sources'
                snapshot_dir.mkdir()
                for note in suite.sources.notes:
                    if note.path.is_file():
                        (snapshot_dir / note.path.name).write_bytes(note.path.read_bytes())
                    for asset_name, asset_path in note.assets.items():
                        if asset_path.is_file():
                            asset_target = snapshot_dir / 'assets' / asset_name
                            asset_target.parent.mkdir(parents=True, exist_ok=True)
                            asset_target.write_bytes(asset_path.read_bytes())
                recorder.log_artifact(snapshot_dir)
    finally:
        recorder.end_run()


__all__ = ['run_suite']
