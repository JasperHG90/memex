# F20 FSRS-4.5 Paper Cross-Check

> **Bottom line up front:** `py-fsrs==4.1.2` does **NOT** implement FSRS-4.5. It implements **FSRS-5**, a successor algorithm with 19 weights (vs 17), an exponential initial-difficulty formula, a different mean-reversion target, and a short-term-stability term that FSRS-4.5 does not have. The team's vendored port matches py-fsrs 4.1.2 (i.e., FSRS-5) bit-exact — and therefore is **not paper-faithful to FSRS-4.5 either**. RFC-014's stated "FSRS-4.5" target is wrong: what's actually being shipped is FSRS-5. The recommendation depends on which algorithm the F20 spec actually wants — see the bottom of this document.

## 1. Summary verdict

There is a version-naming collision that has propagated through the POC, the harness, and RFC-014:

| Claimed | Actual |
|---|---|
| `py-fsrs==4.1.2` implements FSRS-4.5 | `py-fsrs==4.1.2` implements **FSRS-5** (per its own release notes — see citations below) |
| The vendored port at `harness/schedule.py` matches the FSRS-4.5 paper | The port matches **FSRS-5**, not FSRS-4.5 |
| FSRS-4.5 has 19 weights `(0.4197, 1.1869, …)` (per the user prompt) | FSRS-4.5 has **17 weights** with canonical defaults `(0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031, 1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755)`. The 19-weight tuple is FSRS-5's default. |

The bit-exact parity test in `test_parity.py` is meaningful but proves a **different** invariant than the team thought: it proves the port is faithful to py-fsrs (= FSRS-5), not to the FSRS-4.5 paper.

py-fsrs's pip version number (4.1.2) is unrelated to the FSRS algorithm version. The FSRS-5 algorithm landed in py-fsrs **v3.0.0** (2024-08-22) per its release notes. py-fsrs **v2.5.1** is the last release that implements FSRS-4.5.

## 2. Source citations

**FSRS-4.5 specification (canonical):**
- Reference implementation: `open-spaced-repetition/py-fsrs` at tag `v2.5.1` — `src/fsrs/fsrs.py` (formula functions on lines ~109–150) and `src/fsrs/models.py` lines 266–298 (default 17-weight tuple). URL: https://github.com/open-spaced-repetition/py-fsrs/blob/v2.5.1/src/fsrs/fsrs.py
- Algorithm wiki ("The Algorithm"): https://github.com/open-spaced-repetition/awesome-fsrs/wiki/The-Algorithm — confirms "FSRS-4.5 uses 17 parameters (w₀ through w₁₆)"; `R(t,S) = (1 + FACTOR·t/S)^DECAY`, DECAY = -0.5, FACTOR = 19/81. (No standalone arxiv paper exists for FSRS-4.5; the algorithm wiki and reference implementation are the canonical specification — confirmed via the algorithm-history page and the awesome-fsrs README.)
- Confirmation that FSRS-5 = 19 params and adds short-term stability: py-fsrs **v3.0.0** release notes, https://github.com/open-spaced-repetition/py-fsrs/releases/tag/v3.0.0 — quote: "This release implements the new FSRS-5 scheduler algorithm. FSRS-5 now uses 19 parameters instead of 17."
- Confirmation that v6 = FSRS-6 (21 params): py-fsrs **v6.0.0** release notes, https://github.com/open-spaced-repetition/py-fsrs/releases/tag/v6.0.0 — explicitly labels the 19-tuple `(0.40255, 1.18385, …, 0.6621)` as "valid fsrs 5 parameters".

**py-fsrs 4.1.2 source:**
- Local install: `/home/vscode/workspace/.claude/worktrees/dev-ws-revisit/.venv-dev-ws-revisit/lib/python3.12/site-packages/fsrs/fsrs.py` (METADATA confirms `Version: 4.1.2`).
- Upstream tag: https://github.com/open-spaced-repetition/py-fsrs/blob/v4.1.2/src/fsrs/fsrs.py
- Relevant lines: `Scheduler.__init__` defaults at 295–334 (19-weight default); `_initial_stability` 652–658; `_initial_difficulty` 660–668; `_next_interval` 670–683; `_short_term_stability` 685–688; `_next_difficulty` 690–709; `_next_stability` 711–729; `_next_forget_stability` 731–748; `_next_recall_stability` 750–764; `DECAY = -0.5`, `FACTOR = 0.9 ** (1 / DECAY) - 1` at lines 22–23.

**Vendored port:**
- `/home/vscode/workspace/.dev-team-artifacts/dev-tier-a-cognitive-memory/pocs/003-f20-fsrs-parity/harness/schedule.py`
- Relevant lines: weights 42–62 (19-tuple); `DECAY/FACTOR` 29–30; `_initial_stability` 74–75; `_initial_difficulty` 78–80; `_next_difficulty` 83–89; `_retrievability` 92–93; `_next_recall_stability` 96–109; `_next_forget_stability` 112–122 (note `short_term = stability / exp(w[17]*w[18])` on line 121 — this is the FSRS-5 short-term-stability term); `_next_interval` 125–128; `schedule` 131–177.

## 3. Formula-by-formula comparison

Notation: `w = parameters tuple`; `G = Quality/Rating` (1=Again, 2=Hard, 3=Good, 4=Easy); `D = difficulty`; `S = stability`; `R = retrievability`; `r_d = desired_retention`.

| # | Formula | FSRS-4.5 paper / canonical (py-fsrs v2.5.1) | py-fsrs 4.1.2 | Vendored port (`harness/schedule.py`) | Verdict |
|---|---|---|---|---|---|
| a | Initial stability | `max(w[G-1], 0.1)` | `max(w[G-1], 0.1)` (line 656) | `max(w[G-1], 0.1)` (line 75) | AGREES (formula); weights differ — see (j) |
| b | Initial difficulty | `clamp(w[4] - w[5]*(G-3), 1, 10)` — **LINEAR** in `(G-3)` | `clamp(w[4] - exp(w[5]*(G-1)) + 1, 1, 10)` (line 662) — **EXPONENTIAL** | Same as py-fsrs (line 79) | INHERITED DIVERGENCE — port matches py-fsrs but **diverges from FSRS-4.5 paper**. This is the FSRS-5 form. |
| c | Stability after recall | `S * (1 + exp(w[8]) * (11-D) * S^(-w[9]) * (exp((1-R)*w[10]) - 1) * hard_penalty * easy_bonus)` with `hard_penalty=w[15]`, `easy_bonus=w[16]` | identical (lines 750–764) | identical (lines 96–109) | AGREES (formula); weight values diverge between FSRS-4.5 and FSRS-5 defaults |
| d | Stability after lapse / forget | `w[11] * D^(-w[12]) * ((S+1)^w[13] - 1) * exp((1-R)*w[14])` — **single expression, no `min`** | `min(long_term, S/exp(w[17]*w[18]))` (lines 731–748) — **takes minimum with short-term ceiling** | Same as py-fsrs (lines 112–122) | INHERITED DIVERGENCE — port adds the FSRS-5 short-term cap, which FSRS-4.5 does not have |
| e | Difficulty update | `next_d = D - w[6]*(G-3)` then `mean_reversion(init=w[4], current=next_d) = w[7]*w[4] + (1-w[7])*next_d`, clamped 1–10. **No linear damping**; mean-reversion target is `w[4]` (≈ `D_0(G=3)`). | `delta = -w[6]*(G-3)`; `arg_2 = D + (10-D)*delta/9` (linear damping); `next_d = w[7]*D_0(Easy) + (1-w[7])*arg_2`, clamped 1–10. Mean-reversion target is `D_0(G=4)`. (lines 690–709) | Same as py-fsrs (lines 83–89) | INHERITED DIVERGENCE — two FSRS-4.5 → FSRS-5 changes baked in: (1) addition of `(10-D)/9` linear damping, (2) shift of mean-reversion target from `w[4]` (=`D_0(3)`) to `D_0(4)` |
| f | Retrievability | `(1 + FACTOR * t / S) ^ DECAY` | identical (line 198) | identical (line 93) | AGREES |
| g | DECAY | `-0.5` | `-0.5` (line 22) | `-0.5` (line 29) | AGREES |
| h | FACTOR derivation | `0.9^(1/DECAY) - 1 = 0.9^(-2) - 1 = 1/0.81 - 1 = 19/81 ≈ 0.2346` (tied to default `r_d=0.9`) | `0.9 ** (1 / DECAY) - 1` (line 23) | `0.9 ** (1 / DECAY) - 1` (line 30) | AGREES — but note both py-fsrs and the port hard-code the `0.9` baseline into FACTOR even when `desired_retention != 0.9` is set on the scheduler. This is a known py-fsrs design choice that FSRS-4.5 also makes. |
| i | Next interval | `(S / FACTOR) * (r_d^(1/DECAY) - 1)`, then `clamp(round(.), 1, max_interval)` | identical (lines 670–683) | identical (lines 125–128) | AGREES |
| j | Default weights tuple | **17 weights**: `(0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031, 1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755)` (py-fsrs v2.5.1 `models.py` lines 281–297) | **19 weights**: `(0.40255, 1.18385, 3.173, 15.69105, 7.1949, 0.5345, 1.4604, 0.0046, 1.54575, 0.1192, 1.01925, 1.9395, 0.11, 0.29605, 2.2698, 0.2315, 2.9898, 0.51655, 0.6621)` (lines 297–317) | Same 19-tuple as py-fsrs (lines 42–62) | INHERITED DIVERGENCE — port carries py-fsrs's FSRS-5 defaults. The user-prompt's quoted tuple `(0.4197, 1.1869, …, 0.6468)` matches **neither**; it appears to be from a different (perhaps Anki-default or rs-fsrs-default) parameter set. |
| k | Maximum interval cap | 36500 days (default) | 36500 default (line 326) | 36500 default (line 64) | AGREES |
| l | Stability/difficulty bounds | Difficulty clamped to [1, 10] at init and after each update; stability has `>= 0.1` floor at init only | Same (lines 656, 666, 707) | Same (lines 75, 80, 89) | AGREES |
| m | `learning_steps` / `relearning_steps` short-circuit | **NOT in FSRS-4.5 paper.** FSRS-4.5 reference (py-fsrs v2.5.1 lines 51–66) does have a state-machine that distinguishes New vs Learning vs Review, but the configurable per-step time intervals (1 min, 10 min, etc.) are an Anki-flow accommodation, not a paper formula. py-fsrs v2.5.1 hard-codes these as `now + timedelta(minutes={1,5,10})`. | `learning_steps`/`relearning_steps` exposed as scheduler params with defaults `(1m, 10m)` and `(10m,)` (lines 319–325, 380–581). | **Omitted entirely** — port comment at lines 13–15 explicitly notes "Learning-steps short-circuit is omitted. Memex units skip the Learning/Relearning Anki-flow states; the first review writes stability/difficulty directly via the init formulas." | INTENTIONAL DIVERGENCE on the port — this is correct and explicitly documented. The short-circuit is an Anki UX accommodation in py-fsrs and is not part of the FSRS-4.5 paper. The port's omission is paper-faithful in spirit. |

### Summary by severity

- **AGREES** (formula-level): a, c (formula only), f, g, h, i, k, l
- **INHERITED DIVERGENCE** (port matches py-fsrs but neither matches the FSRS-4.5 paper): **b, d, e, j** — these are exactly the four spots where FSRS-5 differs from FSRS-4.5
- **INTENTIONAL/DOCUMENTED DIVERGENCE** (port, paper-faithful direction): m
- **PORT BUGS** (port diverges from both py-fsrs AND paper): none

## 4. Concrete discrepancy list

The four substantive discrepancies between what RFC-014 / the harness call "FSRS-4.5" and what the canonical FSRS-4.5 paper actually says:

1. **Initial difficulty formula** (`schedule.py:78–80`)
   - FSRS-4.5: `D_0(G) = clamp(w[4] - w[5]*(G-3), 1, 10)` (linear)
   - Port (= py-fsrs FSRS-5): `D_0(G) = clamp(w[4] - exp(w[5]*(G-1)) + 1, 1, 10)` (exponential)
   - Citation: py-fsrs v2.5.1 `fsrs.py:113`; py-fsrs v4.1.2 `fsrs.py:660–668`; algorithm wiki "The Algorithm" §FSRS-5: "Changes initial difficulty formula to D₀(G) = w_4 - e^(w_5·(G-1)) + 1"

2. **Difficulty update — addition of linear damping and shift of mean-reversion target** (`schedule.py:83–89`)
   - FSRS-4.5: `next_d = D - w[6]*(G-3); D' = w[7]*w[4] + (1-w[7])*next_d` (no damping; target is `w[4] ≈ D_0(3)`)
   - Port (= py-fsrs FSRS-5): `delta = -w[6]*(G-3); arg_2 = D + (10-D)*delta/9; D' = w[7]*D_0(4) + (1-w[7])*arg_2` (with `(10-D)/9` damping; target is `D_0(4)`)
   - Citation: py-fsrs v2.5.1 `fsrs.py:124–129`; py-fsrs v4.1.2 `fsrs.py:690–709`; algorithm wiki §FSRS-5: "Shifts mean reversion target from D₀(3) to D₀(4)"

3. **Forget stability — short-term cap addition** (`schedule.py:112–122`)
   - FSRS-4.5: single expression `w[11]*D^(-w[12]) * ((S+1)^w[13] - 1) * exp((1-R)*w[14])`, no min clamp
   - Port (= py-fsrs FSRS-5): `min(long_term, S/exp(w[17]*w[18]))` — adds a short-term-stability ceiling using `w[17]`/`w[18]`, which **don't exist** in FSRS-4.5
   - Citation: py-fsrs v2.5.1 `fsrs.py:144–150` (single expression); py-fsrs v4.1.2 `fsrs.py:731–748` (with `min`); py-fsrs v3.0.0 release notes: "FSRS-5 now uses 19 parameters instead of 17"

4. **Default weights tuple — 19 vs 17** (`schedule.py:42–62`)
   - FSRS-4.5: 17 weights, canonical default `(0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031, 1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755)`
   - Port (= py-fsrs FSRS-5): 19 weights, ending in `…, 2.9898, 0.51655, 0.6621`. The two extra weights (`w[17]≈0.51655`, `w[18]≈0.6621`) drive the FSRS-5-only short-term-stability term.
   - Note: the user prompt cited a third tuple `(0.4197, 1.1869, …, 0.6468)`. That tuple matches neither FSRS-4.5 nor py-fsrs 4.1.2's defaults. It appears to be an out-of-date or different-source tuple; verify before relying on it as either spec or implementation reference.

Additional observation (not a divergence, just worth flagging):

- **FACTOR is hard-coded to the `r_d=0.9` baseline** in both py-fsrs and the port (`0.9 ** (1/DECAY) - 1`), even though `desired_retention` is configurable. This is consistent with FSRS-4.5 (it's literally a constant in py-fsrs v2.5.1's `__init__`), but it means non-default `desired_retention` values produce a slightly inconsistent model (the retrievability curve is calibrated to 0.9 specifically). This is an existing FSRS quirk, not a port bug.

## 5. Recommendation

Three viable paths, depending on what F20 actually wants. I list them with the trade-offs spelled out — the F20 owner needs to pick.

**Path A (simplest, most likely correct): rebrand to FSRS-5 and ship as-is**

The vendored port and py-fsrs 4.1.2 are bit-exact equivalent (the parity test proves this) and both implement **FSRS-5**, the current production-grade open-source SRS algorithm. FSRS-5 is what's deployed in Anki, RemNote, and ts-fsrs in 2025. There is no published evidence that FSRS-4.5 is preferred over FSRS-5 for a memory-revisitation use case; quite the opposite — FSRS-5 was a strict improvement (better fit on benchmark; short-term reviews modeled).

**Action:**
1. Update RFC-014 §"FSRS implementation" to say **FSRS-5**, not FSRS-4.5.
2. Update the docstring in `harness/schedule.py:1` ("Vendored FSRS-4.5 scheduler") to "Vendored FSRS-5 scheduler".
3. Update comments at `harness/schedule.py:18–19` ("DECAY = -0.5 and FACTOR = … are FSRS-4.5 retrievability constants") — these constants are unchanged across FSRS-4.5/5/6, so just say "FSRS retrievability constants".
4. Then: drop the vendor; depend on `py-fsrs==4.1.2` directly. The port adds no value once the algorithm-version label is correct, and it's another surface to maintain.
5. (Optional but recommended) pin `py-fsrs >= 4.1.2, < 5.0.0` only if you want to preserve the v4 `Scheduler.review_card` API; py-fsrs 5.x kept the algorithm but changed `ReviewLog`. py-fsrs 6.x switches to FSRS-6 (21 params) — that's a deliberate algorithm upgrade, not a drop-in.

**Path B: keep the port, but fix the labels and lock the test invariant**

If you want the vendored port for control / decoupling reasons (e.g., not pulling a `random` import, deterministic-by-construction, or because Memex doesn't want a runtime dep on `py-fsrs`):

1. Same labelling fixes as Path A items 1–3 (RFC, docstring, comments).
2. Keep `test_parity.py` as-is. Rename the test description from "matches FSRS-4.5" to "matches py-fsrs 4.1.2 (FSRS-5)".
3. Add a one-line guard: pin `py-fsrs==4.1.2` in the dev/test extras so the parity test stays meaningful as upstream evolves.
4. No code changes to `schedule.py` are required. The four divergences from FSRS-4.5 listed above are correct **for FSRS-5** and the port faithfully implements FSRS-5.

**Path C (only if F20 truly requires FSRS-4.5 specifically): re-implement against FSRS-4.5 and switch py-fsrs version**

Only choose this if there's a documented reason RFC-014 needs FSRS-4.5 specifically (e.g., a published comparison study or a downstream constraint you can cite). I see no evidence of that in the F20 spec; if it's just "the RFC says 4.5 because that was the latest paper at the time of writing", that's a labelling error, not a requirement.

If you do go this route, the changes to `schedule.py` are:

```python
# (b) Initial difficulty: revert to linear form
def _initial_difficulty(quality: Quality, p: FSRSParams) -> float:
    d = p.w[4] - p.w[5] * (quality - 3)            # was: w[4] - exp(w[5]*(q-1)) + 1
    return min(max(d, 1.0), 10.0)

# (e) Difficulty update: drop linear damping, revert mean-reversion target to w[4]
def _next_difficulty(difficulty: float, quality: Quality, p: FSRSParams) -> float:
    next_d = difficulty - p.w[6] * (quality - 3)   # no (10-D)/9 damping
    nd = p.w[7] * p.w[4] + (1 - p.w[7]) * next_d   # target is w[4], not D_0(Easy)
    return min(max(nd, 1.0), 10.0)

# (d) Forget stability: drop short-term cap
def _next_forget_stability(
    difficulty: float, stability: float, retrievability: float, p: FSRSParams
) -> float:
    return (
        p.w[11]
        * math.pow(difficulty, -p.w[12])
        * (math.pow(stability + 1, p.w[13]) - 1)
        * math.exp((1 - retrievability) * p.w[14])
    )
    # no `min(long_term, short_term)`; w[17]/w[18] do not exist in FSRS-4.5

# (j) Truncate weights tuple to 17 with FSRS-4.5 canonical defaults
@dataclass(frozen=True)
class FSRSParams:
    w: tuple[float, ...] = (
        0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031,
        1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755,
    )  # 17 weights, canonical FSRS-4.5
    desired_retention: float = 0.9
    maximum_interval: int = 36500
```

You would then **also** need to switch the parity test reference: install `py-fsrs==2.5.1` (the last FSRS-4.5 release), not `4.1.2`, because no current py-fsrs version implements FSRS-4.5. This is the version-locking cost of choosing this path.

---

**My recommendation: Path A.** It eliminates the version-naming bug, drops a maintenance surface (the vendored port), and aligns Memex with what the wider FSRS ecosystem ships. There is no F20-specific requirement I can find that justifies the cost of Path C. But the F20 owner should make that call explicitly rather than absorb it implicitly.
