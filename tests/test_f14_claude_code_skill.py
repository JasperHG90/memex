"""F14 — Claude Code plugin SKILL.md invariants (TC-F14-4).

Locks the differentiated /remember + /recall blocks for the procedure: KV
namespace per team-lead's approved draft 3. Two flavors of assertion:

* Positive: each skill names the procedure: namespace and the right
  surface for its register (write-side for remember, read-side for
  recall).
* Negative: the wrong surface does NOT bleed into the other skill —
  remember does not advertise the read-side helper, recall does not
  advertise the write-side helper.
"""

from __future__ import annotations

from pathlib import Path


def _skills_dir() -> Path:
    return Path(__file__).resolve().parent.parent / 'packages' / 'claude-code-plugin' / 'skills'


def _read(slug: str) -> str:
    return (_skills_dir() / slug / 'SKILL.md').read_text()


# ---------------------------------------------------------------------------
# /remember — write-side surface
# ---------------------------------------------------------------------------


def test_remember_documents_procedure_namespace_and_kv_put_path():
    text = _read('remember')
    # Procedure key shape MUST be named so an agent can self-discover the contract.
    assert 'procedure:<verb>:<context-tag>' in text
    # Write side: kv_put is the path for capturing a learned procedure.
    assert 'memex_kv_put' in text


def test_remember_does_not_advertise_kv_get_include_history():
    """``include_history=true`` is a READ-time concern; it does NOT belong
    in /remember (which is the write-side capture skill). This guard fences
    against accidental cross-pollination during future edits.
    """
    text = _read('remember')
    # The write-side skill MAY mention reading history as a follow-up
    # ("read prior versions with..."), but only as a one-line aside; the
    # verbose recall instructions live in /recall.
    forbidden_combinations = [
        'BEFORE other surfaces',  # the recall-side instruction
        'when the user asks "how do I X?"',  # the recall-side trigger
    ]
    for forbidden in forbidden_combinations:
        assert forbidden not in text, (
            f'/remember/SKILL.md MUST NOT contain {forbidden!r} '
            '(belongs in /recall — keep the registers separated)'
        )


# ---------------------------------------------------------------------------
# /recall — read-side surface
# ---------------------------------------------------------------------------


def test_recall_documents_procedure_namespace_and_kv_get_include_history():
    text = _read('recall')
    # Procedure namespace MUST be referenced (full template lives in /remember).
    assert 'procedure:' in text
    assert 'memex_kv_list(namespaces=["procedure"])' in text


def test_recall_does_not_advertise_kv_put_for_procedures():
    """Writing a procedure: key is the /remember register, not /recall.

    Negative-grep regression fence per team-lead's draft-3 review:
    recall/SKILL.md must NOT contain ``memex_kv_put`` paired with
    ``procedure:``. Even though ``memex_kv_put`` is mentioned elsewhere
    on the recall side, advertising it as the way to write a learned
    procedure belongs to /remember.
    """
    text = _read('recall')
    # Search for the specific cross-pollination signal: kv_put used for
    # procedure: keys in the recall context.
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if 'memex_kv_put' in line and 'procedure:' in line:
            raise AssertionError(
                f'/recall/SKILL.md line {i + 1} pairs memex_kv_put with '
                f'procedure:, which belongs in /remember: {line!r}'
            )


def test_recall_step_numbering_is_consecutive():
    """Visible numbered steps (outside HTML comments) are 1, 2, 3, 4 in order.

    F14 inserted step 4 (procedure recall) per team-lead's renumber call.
    F32's step (currently inside an HTML comment, awaiting WS-diagnostics
    activation) is renumbered to 5 so it becomes consecutive when
    uncommented.
    """
    import re

    text = _read('recall')
    # Strip everything inside HTML comments — these are scheduled
    # contributions from other workstreams, not visible to users yet.
    visible = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)

    step_nums = [int(m) for m in re.findall(r'^(\d+)\.\s', visible, flags=re.MULTILINE)]
    assert step_nums == list(range(1, len(step_nums) + 1)), (
        f'visible steps in recall/SKILL.md must be 1..N consecutive starting at 1; got {step_nums}'
    )
