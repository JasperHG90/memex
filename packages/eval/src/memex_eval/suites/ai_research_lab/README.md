# AI Research Lab Suite

Verifies entity resolution, graph cooccurrence, and edge-case name
handling across a corpus of AI/NLP and quantum-computing research
documents from a fictional AI Research Lab and its quantum collaborator
QuantumTech Labs.

The first four scenarios cover the canonical resolution + graph case
("Dr. Elena Vasquez" ↔ "Elena Vasquez"; Elena cooccurs with Raj Mehta;
cross-document NLP/transformer facts).

The remaining five scenarios stress entity-resolution edge cases on
quantum-research figures whose source documents use abbreviated or
title-prefixed forms:

- **Abbreviated names**: `J. Rodriguez` (symposium) and `Juan Rodriguez`
  (award) must collapse to the same canonical entity.
- **Title variations**: `Dr. Amara Osei` and `Amara Osei` resolve to one
  entity.
- **Cross-document cooccurrence**: Rodriguez ↔ Osei must be linked by the
  graph even though they never appear in the same paragraph.

Edge-case scenarios are phrased with **canonical names** (`Juan
Rodriguez`, `Amara Osei`) rather than short forms (`Rodriguez`, `Osei`)
because the `EntityResolves` outcome uses set equality on the resolver's
output — testing the resolver's canonicalization, not substring matching.

## Components under test

- `memory/entity_resolver.py` — fuzzy name matching + canonicalization
  across diacritics, titles, and abbreviated first names
- `memory/entity_graph` — cooccurrence tracking + hybrid ranking
- `memory/entity_cooccurrence` — cross-document link extraction

## Primary metrics

- `suite.pass_rate` — deterministic checks across resolution + graph

## Knobs

- `server.memory.entity.resolution_threshold` — fuzzy-match cutoff

## Notes on coverage

The cooccurrence-only scenario marks
`expected_failure_modes=['claude-code', 'hermes']` because graph
cooccurrence inspects server-side relations that agent backends cannot
reproduce from text-only tool output.
