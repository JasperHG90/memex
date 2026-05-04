"""KV namespace parity test — ensures the Claude Code rule file's KV
namespace list matches the canonical sources.

The `procedure:` namespace was missing from `memex.md` in a prior revision,
and no test caught the drift. This test pins the five canonical namespace
prefixes that MUST appear in every agent surface that documents the KV store.
"""

from pathlib import Path

from memex_common.agent_surface import LAYER_ROUTING_PRIMER_TABLE

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CC_RULE_PATH = _REPO_ROOT / 'claude-code-plugin' / 'rules' / 'memex.md'

# The procedure: prefix appears in the layer-routing table; the others
# appear in the KV store section of agent surfaces. All five MUST be
# present across surfaces.
_CANONICAL_KV_PREFIXES = ('global:', 'user:', 'project:', 'app:', 'procedure:')


def test_procedure_prefix_in_layer_routing_table() -> None:
    """The `procedure:` namespace must appear in the canonical
    `LAYER_ROUTING_PRIMER_TABLE` (it's in the Procedural-observations row)."""
    assert 'procedure:' in LAYER_ROUTING_PRIMER_TABLE, (
        '`procedure:` prefix missing from LAYER_ROUTING_PRIMER_TABLE — '
        'the Procedural-observations row must include this namespace.'
    )


def test_kv_prefixes_in_claude_code_rule() -> None:
    """The `memex.md` rule file MUST list all five canonical KV namespace
    prefixes in its KV store section."""
    rule_text = _CC_RULE_PATH.read_text()

    # Find the "## Memex KV store" section
    kv_section_start = rule_text.find('## Memex KV store')
    assert kv_section_start != -1, '`memex.md` is missing a "## Memex KV store" section entirely'

    # Extract text until the next heading or end of file
    next_heading = rule_text.find('\n## ', kv_section_start + 1)
    kv_section = (
        rule_text[kv_section_start:next_heading]
        if next_heading != -1
        else rule_text[kv_section_start:]
    )

    missing: list[str] = []
    for prefix in _CANONICAL_KV_PREFIXES:
        if prefix not in kv_section:
            missing.append(prefix)

    assert not missing, (
        f'`memex.md` KV section is missing canonical namespace prefixes: {missing}. '
        'Every KV namespace prefix must appear here. '
        'If a prefix was intentionally removed, update _CANONICAL_KV_PREFIXES.'
    )
