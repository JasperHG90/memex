# How Memex is evaluated

Memex tunes more than a dozen knobs that decide which memory you see — `mw_alpha=0.3`, the four FSFM weights, the entity-resolver threshold at 0.65, the 14-day cooldown, and so on.

None of those numbers came from a held-out dataset.

They came from literature precedent and rules of thumb. So when you read a default in the design doc, the first question is fair: *how do you know it isn't wrong?* This page describes how Memex measures itself today and where the gaps are.

## Context: why eval matters here

You build a memory system by tuning.

The temporal weight has to push recent notes up, but not so hard that the right older note never surfaces.

The entity resolver has to merge "Sarah Chen" and "S. Chen" without merging "Sarah Chen" and "Sarah Park".

The Memory Worth score has to reward outcomes without letting popular units run away with the ranking.

Every one of those is a numerical decision that needs a measurement loop behind it.

The active measurement work is the internal evaluation suite — and, increasingly, how a real agent drives Memex through it.

That suite is not large enough to do principled tuning. It is enough to catch the obvious case where you ship a change and something visible breaks.

Memex also carries machinery for external benchmarks — borrowed published datasets like LoCoMo and LongMemEval. The code exists, but it is not where the effort goes today, and you should not read its occasional numbers as a maintained scoreboard.

That gap — between "catches regressions" and "tunes defaults" — is the empirical caveat in the design doc, and it carries through everything below.

## Model: where the effort goes

Memex has two evaluation surfaces, and they are not equal partners. One is live and growing; the other is on-demand tooling that happens to exist.

| Surface | Lives at | What it answers | Status |
|---|---|---|---|
| Internal evaluation suite | `packages/eval/src/memex_eval/suites/<name>/` | "Did this commit break a known-good case?" and "Can a real agent use the tool surface to answer this?" | Active — almost all new eval work lands here |
| External benchmarks | `external/locomo_*.py`, `longmemeval_*.py` | "How does Memex score on a published long-memory benchmark?" | Present but not the current focus; run rarely, on demand |

The internal suite owns its corpus, and it is where the investment goes. It has grown two layers.

**Retrieval and extraction regression.** A scenario asserts a specific thing about a specific markdown document the author wrote — this query returns "Sarah Chen", these gold units land in the top five. The `api` backend calls the live server directly and the outcome scores the result. Seven suites ship today.

**Agent integration.** The same scenario shape, but the answer comes from a *real agent* driving Memex's tool surface — Claude Code over MCP, or Hermes over the plugin — instead of a direct API call. <code-ref path="packages/eval/src/memex_eval/suite/agents.py" lines="777-778" /> The `agent_integration` suite — 38 scenarios across ten groups (triage, temporal, entity, survey, faithfulness, navigation, feedback, KV, lifecycle) — measures whether the agent picks the right tool, cites honestly, and routes each write to the right place. This is the layer under active expansion.

The external surface borrows someone else's corpus. LoCoMo is one long conversation with 50 QA pairs; LongMemEval is 500 questions across six categories. Each has its own answer / judge / report module family, and they share the runner's shape. But the code is the whole story here — these benchmarks are not run on a cadence, and any numbers you see are occasional snapshots, not a maintained scoreboard. Read them as "Memex *can* be compared against published work", not "Memex *does*, continuously".

The internal suite and the external pipelines share the LLM judge (Gemini via DSPy); otherwise they are separate.

### What you get from each

The internal suite is the one that runs in CI. It runs in seconds against a snapshot-cached vault and gives you a pass-rate per suite. <code-ref path="packages/eval/src/memex_eval/suite/runner.py" lines="1601-1607" /> When a scenario fails, you know which query stopped working and which outcome rule fired. That is enough to bisect a regression; it is not enough to set `mw_alpha`.

The external surface runs on demand. The LoCoMo pipeline runs against the LoCoMo conversation dataset. <code-ref path="packages/eval/README.md" lines="93-114" /> It produces a five-category score breakdown — single-hop, multi-hop, temporal, open-domain, adversarial — and a Markdown report with retrieval-efficiency plots. The LongMemEval pipeline runs against its 500-question dataset and scores per cognitive-ability category, with its own ingest, judge, report, and efficiency modules.

Any numbers live in `docs/reference/evaluation-results.md`, from whenever someone last ran the pipeline — which is seldom. The cost of one full external pass is dominated by the answering agent (Claude Code calling MCP tools per question), so it never runs in CI and is not run on a schedule.

## Mechanism: a worked example

Pick the scenario `who_leads_alpha` from the `acme_corp` suite. <code-ref path="packages/eval/src/memex_eval/suites/acme_corp/__init__.py" lines="120-127" />

```python
suite.register(
    id='who_leads_alpha',
    description='Query about Alpha leadership returns Sarah Chen.',
    query='Who leads Project Alpha?',
    top_k=10,
    group='extraction',
    expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen']),
)
```

Here is what happens when you run `memex-eval suite run acme_corp`.

**Sources ingest.** The runner loads every `.md` file under `packages/eval/src/memex_eval/suites/acme_corp/sources/` into a temporary vault.

It then runs the same extraction pipeline a real user would — chunking, fact extraction via DSPy, entity resolution, embedding.

The first run takes minutes because the LLM extracts facts.

Subsequent runs hit the snapshot cache (`--from-snapshot=auto`) and finish in seconds. <code-ref path="packages/eval/README.md" lines="76-91" />

**Answer mode.** The runner asks each scenario for its `answer_mode`.

`who_leads_alpha` inherits `'api'`, so the `DirectApiBackend` calls `api.search('Who leads Project Alpha?', top_k=10)` against the live server and packages the result into an `AgentAnswer`. <code-ref path="packages/eval/src/memex_eval/suite/agents.py" lines="56-101" />

Other scenarios can route through `claude-code` or `hermes` backends, which drive a real agent and capture its answer text plus tool-call trace.

The same outcome scores all three the same way — outcomes consume whichever `AgentAnswer` fields the backend populated, and they do not care which backend produced them.

**Score.** The runner calls `outcome.score(answer, scenario, ...)`.

For `KeywordsPresent` that is a single word-boundary regex per keyword against the joined text of the retrieved units. <code-ref path="packages/eval/src/memex_eval/suite/base.py" lines="440-451" />

If "Sarah Chen" shows up anywhere in the top ten units, the scenario passes with `{'pass': 1.0}`.

If it does not, the scenario fails.

There is no partial credit on this outcome type — the next outcome down the list, `GoldUnitIds`, gives you that.

**Aggregate.** The runner collects every scenario's `pass` key and computes the suite-level pass rate. <code-ref path="packages/eval/src/memex_eval/suite/runner.py" lines="1351-1361" />

An MLflow recorder logs every metric, every override, and the full session log.

If you skipped `--mlflow-uri`, `NullRecorder` swallows the calls and you still get the terminal report. <code-ref path="packages/eval/src/memex_eval/cli.py" lines="66-78" />

### The five outcome types you will see most

`acme_corp` and its siblings lean on five outcome shapes. Each one consumes the same `AgentAnswer` but asks a different question.

**`KeywordsPresent`** — does the answer mention the right things? Deterministic, regex-bounded, no LLM. The whole-word boundary matching protects you from substring false positives (the word "alpha" inside "alphabet" does not satisfy a "Project Alpha" keyword). <code-ref path="packages/eval/src/memex_eval/suite/base.py" lines="440-451" />

**`GoldUnitIds`** — did the retriever return the *right* memory units? You declare a set of source `note_keys` (the filename stems of the markdown sources you wrote). The runner maps those keys to actual unit IDs at ingest time and the outcome computes `recall@k`, `mrr`, and optionally `ndcg@k` against the retrieved unit list. <code-ref path="packages/eval/src/memex_eval/suite/base.py" lines="538-583" /> Anchoring on note_keys instead of raw unit IDs is what lets the same baseline survive a re-ingest on a different machine.

**`RankingOrder`** — did the retriever rank things in the right order? Checks that the first occurrence of each expected keyword in the answer is in the declared order. <code-ref path="packages/eval/src/memex_eval/suite/base.py" lines="586-612" />

**`UsefulAtK`** — for the top *k* retrieved units, how many would a judge call relevant? Uses the LLM judge and returns a `useful_at_k` ratio. <code-ref path="packages/eval/src/memex_eval/suite/base.py" lines="970-1010" /> A `threshold` lets the scenario decide what counts as a pass — `threshold=0.5` means "at least half the top-five are relevant".

**`LLMJudge`** — does a free-form answer satisfy a rubric? The judge grades on a continuous score; the outcome passes when the score crosses a threshold (default 0.75). <code-ref path="packages/eval/src/memex_eval/suite/base.py" lines="918-967" /> Use this when the right answer can be worded many ways — a keyword check would be too brittle and a strict gold-set would be too narrow.

About twenty more outcome types exist for narrower assertions — temporal ordering, entity resolution, contradiction-link presence, KV roundtrip, lint-finding presence — but the five above carry most of the day-to-day work.

## Trade-offs

**Why a ~30-query hand-verified suite and not a 10,000-query held-out set?**

Two reasons.

First, the corpus has to be hand-verified to be useful as a regression gate. Every scenario asserts a specific thing about a specific document the author wrote, and if the assertion is wrong the gate is worse than nothing.

Hand-verification does not scale past a few hundred queries per maintainer.

Second, the suite gates against *known-good behaviour on representative shapes* — it is not a held-out test set in the statistical sense.

Using it as one would tune the system into doing well on those thirty queries and worse on everything else.

The design doc names this explicitly: the suite catches obvious-case regressions but cannot tune the system's eight-plus knobs without overfitting.

**Why LLM-as-judge for graded scenarios?**

Memex returns prose. Some answers are right but worded differently from the reference.

A regex-bounded check would flag those as failures and the suite would optimise toward stilted phrasing.

`LLMJudge` passes the query, a rubric, and the candidate answer to Gemini via DSPy and accepts the judge's score above a threshold. <code-ref path="packages/eval/src/memex_eval/suite/runner.py" lines="1609-1631" />

The cost is judge variance — two runs can disagree on borderline cases — and a small per-scenario billing burden.

The benefit is that scenarios can assert "the answer must identify Sarah Chen as the lead of Project Alpha" instead of brittle keyword lists.

The judge probes itself with a dummy call at startup, and the runner skips judge-needing scenarios when it cannot reach the model. That keeps a missing API key from masquerading as a regression.

**Why MLflow is optional.**

The framework treats MLflow as a recorder, not a runner. <code-ref path="packages/eval/src/memex_eval/cli.py" lines="66-78" />

When you pass `--mlflow-uri`, every metric, parameter, and session log is captured to your tracking server.

When you don't, `NullRecorder` no-ops the calls and you still get the terminal report.

The trade-off is that without MLflow you have no historical record. You cannot diff this week's pass rate against last week's. You cannot plot ranking-stability drift over time. You cannot replay a six-month-old run with the exact knobs it used.

For CI that is fine because the report on each PR is enough. For tuning work it is not.

**The empirical caveat.**

Every default in Memex was set from literature precedent and rules of thumb, then validated against the suite. The suite is too small to tune them. So the system runs on plausible defaults, the suite catches the cases where someone made the defaults dramatically worse, and the gap in between — sensitivity sweeps, calibration of the Memory Worth score, ablation deltas per signal — is pending work tracked in the design doc.

You will see this caveat surface in concrete ways. The Memory Worth distribution histogram lives in Prometheus but no Gini or Herfindahl summary derived from it ships. The FSFM weights have not been swept across a held-out corpus. The entity-resolver threshold of 0.65 is "the number that does not over-merge on `acme_corp`", not "the number that minimises pairwise F1 on a labelled benchmark". Each of those is a known gap, not an oversight.

## Implications

**How to read a pass-rate.**

The pass-rate the runner prints at the end of a suite is the fraction of scenarios whose top-level `pass` key came back `1.0`. <code-ref path="packages/eval/src/memex_eval/suite/runner.py" lines="1351-1361" />

It is not a percentage of "how good is Memex" — it is a percentage of "how many of these specific assertions held".

A pass-rate of 1.0 means no scenario in the suite regressed.

A pass-rate of 0.97 with one failure means exactly one assertion broke. Go read the failure and decide whether the assertion or the code is wrong; sometimes the assertion was wrong and your job is to update it.

**What "no calibration" means for Memory Worth.**

The MW score blends success counts, failure counts, recency, and a few other signals into a single number that nudges ranking. The blend weights were picked to behave reasonably on the suites. Nobody has shown that `mw_alpha=0.3` is statistically optimal across a representative corpus.

If you read the design doc and wonder why a knob is *exactly* `0.3` and not `0.27` or `0.42`, the answer is: nobody knows, and the suite is not big enough to tell you.

The MW score's *shape* — its formula, its inputs, its log-additive composition with other signals — is the load-bearing part. The exact numeric defaults will move when calibration work lands.

**When to run the suite.**

Run it in CI on every PR that touches `packages/core/` or `packages/eval/`. Run it locally before you push a change to extraction, retrieval, or reflection.

Run it with `--from-snapshot=auto` so you skip the LLM extraction cost on reruns. First invocation populates the cache; every subsequent invocation imports a frozen vault and goes straight to scoring. <code-ref path="packages/eval/README.md" lines="76-91" />

Run the LLM judge for ad-hoc work too. Pass `--judge-model` (or set `EVAL_JUDGE_MODEL`) on `memex-eval suite run`; without a reachable judge the runner skips `LLMJudge` and `UsefulAtK` scenarios with `status='skip'`, and you are running a strictly weaker check and calling it green.

The external benchmarks — LoCoMo, LongMemEval — are available if you want to compare against a published dataset, but they are not part of the routine loop and are run rarely. Reach for the `agent_integration` suite instead when you change the answering-agent prompt or the tool surface; reach for the retrieval suites when you change extraction, retrieval, or reflection.

**What to do when a default moves.**

If you change a default in the design doc, three things have to move with it. The numeric default in code. The mention in the design doc itself. The expectation embedded in the suites that depend on that default. The empirical caveat applies in both directions: a default was chosen without held-out data, and a default cannot be moved without held-out data either. The right path is small steps, suite green, and a note in `BACKLOG.md` that the change is provisional pending calibration.

## See also

- [Tutorial: Getting started](../tutorials/getting-started.md)
- [How-to: Run the evaluation suite](../how-to/evaluation-results.md)
- [Reference: evaluation results](../reference/evaluation-results.md)
- [Explanation: the Hindsight framework](how-memex-works/high-level-architecture.md)
