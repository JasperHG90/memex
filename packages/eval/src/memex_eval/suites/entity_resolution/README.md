# Entity Resolution Suite

Verifies that name-variants ("Dr. Elena Vasquez" vs "Elena Vasquez")
resolve to the same entity, and that cooccurrences across documents
populate the entity graph correctly.

## Components under test

- `memory/entity_resolver.py` — fuzzy name matching + canonicalization
- `memory/entity_graph` — cooccurrence tracking + hybrid ranking

## Primary metrics

- `suite.pass_rate` — deterministic checks across resolution + graph

## Knobs

- `server.memory.entity.resolution_threshold` — fuzzy-match cutoff
