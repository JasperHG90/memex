# Choose models for extraction, reflection, and embeddings

This tutorial walks you through swapping the models Memex uses at three different jobs — fact extraction, reflection, and embeddings — and watching the trade-offs land in the numbers. By the end you will have run the same small ingestion four times under four different model choices, watched latency and token cost shift, and read enough of the extracted facts to judge quality with your own eyes.

You will learn by doing. Each step ends with a measurable observation — a Prometheus metric, a CLI line, or a count of extracted facts — that you can compare to what came before. The goal is not a single "right" config. It is a feel for which dial moves what.

Plan on 45 minutes. You will spend more time waiting on ingestion than typing.

## Prerequisites

- **Memex installed and running.** Follow [Get started with Memex](getting-started.md) first if you have not already. You need a running server, a working vault, and the `memex` CLI on your PATH.
- **A working LLM API key.** This tutorial swaps between hosted models — Gemini Flash, Claude Haiku, Claude Sonnet — so you need at least one provider key. Pick whichever you already have. If you only have one, the swaps still work; you will just see latency change less than cost.
- **A test document.** Use any markdown file with a few hundred words of prose. A meeting note, a design memo, a copy of a blog post — anything you do not mind re-ingesting four times. The file in this tutorial is called `test-doc.md` and lives in your current directory.
- **curl** for poking the metrics endpoint.

## Step 1: Capture the baseline

Before you change anything, ingest your test document once under the default config so you have something to compare against. The default extraction model is `gemini/gemini-3-flash-preview` — set in `ServerConfig.default_model` at <code-ref path="packages/common/src/memex_common/config.py" lines="2328-2331" />.

Create a vault for this tutorial so the four runs do not contaminate your other notes:

```bash
memex vault create models-tutorial
export MEMEX_VAULT__ACTIVE=models-tutorial
```

Ingest the test document:

```bash
memex note add --file test-doc.md
```

You will see lines like:

```
Reading file test-doc.md...
Uploading and summarizing 1 file(s)...
Note added successfully! UUID: <uuid>
Extracted 12 memory units.
```

The exact count depends on your document. Write down the number from the `Extracted N memory units.` line — you will compare it to the other runs. **Twelve facts** is the baseline I will refer to from here on; substitute your own number when you read along.

Now you have a baseline run on disk. Time to look at what it cost.

## Step 2: Read the latency and token cost off the metrics endpoint

Memex emits Prometheus metrics for every LLM call. The two you care about for this tutorial are `memex_llm_call_duration_seconds` (a histogram of how long each call took, in seconds) and `memex_llm_calls_total` (a counter of how many calls happened). Both are defined in <code-ref path="packages/core/src/memex_core/metrics.py" lines="182-190" />.

Hit the metrics endpoint:

```bash
curl -s http://localhost:8000/api/v1/metrics | grep -E "memex_llm_call(s_total|_duration)"
```

You will see something like:

```
memex_llm_calls_total{status="success"} 14.0
memex_llm_call_duration_seconds_bucket{le="0.5"} 0.0
memex_llm_call_duration_seconds_bucket{le="1.0"} 0.0
memex_llm_call_duration_seconds_bucket{le="2.5"} 8.0
memex_llm_call_duration_seconds_bucket{le="5.0"} 13.0
memex_llm_call_duration_seconds_bucket{le="10.0"} 14.0
memex_llm_call_duration_seconds_sum 38.7
memex_llm_call_duration_seconds_count 14.0
```

Three numbers to record:

- **Call count.** The `_count` line — 14 calls in this example. PageIndex chunking plus per-chunk extraction adds up fast even on small documents.
- **Total wall-clock.** The `_sum` line in seconds — about 38 seconds here. Divide by the count for a mean: 2.8 seconds per call.
- **Distribution.** Bucket counts tell you the tail. In this example, 8 calls finished under 2.5 seconds; the slow ones pushed the mean.

Tokens are not exported as a Prometheus metric. You read those from the server log instead. Tail the log and grep for the litellm cost line:

```bash
tail -200 ~/.cache/memex/logs/server.log | grep -E "prompt_tokens|cost"
```

Sum the `prompt_tokens` and `completion_tokens` across the lines that belong to your ingestion. Track all three numbers — call count, mean duration, total tokens — in a notes file. You will read them again three times today.

## Step 3: Swap extraction to Claude Haiku and re-ingest

Now change the extraction model. Edit your config file — `~/.config/memex/config.yaml` if you are on Linux, `~/Library/Application Support/memex/config.yaml` on macOS — and set:

```yaml
server:
  memory:
    extraction:
      model:
        model: anthropic/claude-haiku-4-5
        api_key: ${ANTHROPIC_API_KEY}
```

The `server.memory.extraction.model` field is a `ModelConfig` (defined at <code-ref path="packages/common/src/memex_common/config.py" lines="252-290" />); the nested `.model` string takes any [LiteLLM-format identifier](https://docs.litellm.ai/docs/providers). `api_key` accepts `${VAR}` interpolation so you do not have to paste the secret into the file.

Restart the server so the new config loads:

```bash
memex server stop
memex server start
```

Confirm the swap took:

```bash
memex config show | grep -A2 "extraction:"
```

You should see `claude-haiku-4-5` where you used to see `gemini-3-flash-preview`.

Now re-ingest with a fresh content body so the idempotency check does not short-circuit. Either edit `test-doc.md` (change a sentence) or use a sibling file. Run:

```bash
memex note add --file test-doc.md --key test-doc-haiku
```

The `--key` flag pins a unique stable identifier so Memex treats this as a new note rather than a re-upload. Wait for it to finish.

Hit the metrics endpoint again:

```bash
curl -s http://localhost:8000/api/v1/metrics | grep -E "memex_llm_call(s_total|_duration)"
```

The counts are cumulative — they include both runs. Subtract the baseline numbers from Step 2 to get the Haiku-only deltas.

Open the server log and pull the same `prompt_tokens` / `completion_tokens` lines for the new ingestion. Note them.

## Step 4: Compare the two runs

Pull out your notes file and lay the two runs side by side:

| Measurement | Gemini Flash (baseline) | Claude Haiku |
|---|---|---|
| LLM calls | 14 | _your number_ |
| Mean call duration (s) | 2.8 | _your number_ |
| Total prompt tokens | _your number_ | _your number_ |
| Total completion tokens | _your number_ | _your number_ |
| Units extracted | 12 | _your number_ |

Two questions to ask the table:

**Cost shape.** Flash is roughly 4–8× cheaper per million tokens than Haiku at current pricing (see [LiteLLM cost data](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)). If your token counts come out close, Haiku will cost more. If Haiku used noticeably fewer tokens, the higher per-token price might still leave it the loser — or it might catch up. Multiply each provider's per-million rates against your token counts and compare totals.

**Latency.** Flash is usually 1.5–2× faster on extraction calls. If your mean duration dropped on Haiku, your network to Anthropic is faster than to Google. Both providers' p95 tails sometimes spike — the histogram buckets show you whether a single slow call dragged the mean.

Now look at quality. Pull the units extracted by each run:

```bash
memex memory search "any phrase from test-doc.md" --limit 20 --json | jq '.[] | {text, note_id}'
```

Compare the unit bodies for `test-doc` against `test-doc-haiku`. Are the same facts captured? Are any rephrased poorly? Is one more precise about quantities or dates?

You will usually find them similar. The fact-extraction schema is structured — the DSPy signature forces both models into the same output shape — so quality differences are subtle. The design doc says this directly: extraction quality is gated by the schema, not raw model power (DESIGN_DOCUMENT §4.1). Cheap-fast almost always wins here.

## Step 5 (optional): Upgrade the reflection model to Sonnet

Reflection is the highest-leverage LLM call in the system — four to five sequential calls per cycle, weaving evidence into a mental model. A stronger model pays off here in a way it does not at extraction. Try it.

Edit the same config file and add a reflection override:

```yaml
server:
  memory:
    reflection:
      model:
        model: anthropic/claude-sonnet-4-6
        api_key: ${ANTHROPIC_API_KEY}
```

The field lives at <code-ref path="packages/common/src/memex_common/config.py" lines="530-533" />. Same `ModelConfig` shape; this one overrides reflection specifically. If you leave it `None`, reflection inherits whatever extraction is using (see the `sync_default_model` validator at <code-ref path="packages/common/src/memex_common/config.py" lines="2564-2581" />).

Restart the server.

To see reflection actually run, you need an entity with enough mentions for the priority gate to fire. Ingest a few more notes that all reference the same name or topic so an `Entity` row builds up cooccurrence weight. Three or four notes is usually enough.

Trigger reflection manually on that entity:

```bash
memex entity list --query "your topic name"
# pick the entity ID for your topic
memex memory reconsolidate <entity-uuid>
```

`memex entity list` takes no `--vault` flag — it ranks entities across every vault on the server, so filter by name with `--query` to find the one you just built up. The `reconsolidate` command runs the full Hindsight cycle on one entity (see <code-ref path="packages/cli/src/memex_cli/memory.py" lines="218-265" />). It accepts `--vault` and falls back to the active vault you exported in Step 1 when you omit it. Output is JSON — look for the `mental_model` block.

Re-run the metrics query. Reflection calls are tagged the same way as extraction calls (the `status` label is the only dimension on the counter), so to isolate them you compare counts before and after. Subtract.

Now read the mental model body itself:

```bash
memex memory search "your entity name" --limit 5
```

Compare to a Flash-on-reflection run, if you have one to compare against. Sonnet's mental models tend to be more precise about contradictions and more careful about hedging — *"this person worked on X starting around mid-2025, with some ambiguity about the start date"* versus *"this person worked on X in 2025"*. Whether that precision matters depends on what you ask the vault.

## Step 6 (optional): Swap the embedding model — and read the warning twice

Embeddings are different. Extraction and reflection models can be swapped freely; runs A and B will produce different facts, but those facts coexist fine. **Embeddings cannot be mixed.** If you change the embedding model, every vector in your database becomes nonsense — the new model's vector space is unrelated to the old one's.

Memex guards against the silent version of this. At server startup, the embedding backend is probed and the output dimension is checked against the database schema's expected width (384, at <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="35" />). If the new model produces a different dimension, the server refuses to start with an explicit error (see <code-ref path="packages/core/src/memex_core/server/__init__.py" lines="185-199" />).

The guard catches dimension mismatches. It does **not** catch space mismatches — two 384-dimensional models can both pass the startup check yet produce wildly different vectors for the same text. The only safe path is to re-embed everything.

If you want to try OpenAI embeddings on this tutorial vault, do this:

1. Drop the vault first. The whole point is that the old vectors are now stale:
   ```bash
   memex vault delete models-tutorial --force
   memex vault create models-tutorial
   ```

2. Edit the config:
   ```yaml
   server:
     embedding_model:
       type: litellm
       model: openai/text-embedding-3-small
       api_key: ${OPENAI_API_KEY}
       dimensions: 384
   ```
   The `dimensions: 384` is required — OpenAI's `text-embedding-3-small` defaults to 1536 dimensions, which would fail the startup probe. The model supports Matryoshka truncation so you can ask for 384 and get a vector that fits the schema (see [OpenAI's embedding docs](https://platform.openai.com/docs/guides/embeddings)). The `dimensions` field on `LitellmEmbeddingBackend` lives at <code-ref path="packages/common/src/memex_common/config.py" lines="329-333" />.

3. Restart the server. Watch the logs for the probe — you should see one line confirming the dimension matched. If the probe fails, the server will refuse to start and tell you exactly which dimension it got.

4. Re-ingest your test document into the now-empty vault.

5. Run the same search query you ran in Step 4. Compare the result ordering. Different embedding models will order results differently even when the same facts are present.

Whether the new ordering is *better* is a judgment call. For most personal-scale vaults, the built-in ONNX embedder is fine — it is fine-tuned on Memex's own data format and runs locally with zero external dependency. The case for swapping is narrow: you have a large vault, you have measurable retrieval-quality complaints, and you have benchmark evidence that the candidate model helps on data like yours.

If you have not got that evidence, do not swap. The default works.

## Step 7: Decide what to use

You now have four runs' worth of data on the same vault. Three rules of thumb fall out of them:

**Extraction: pick the cheap-fast option.** The extraction schema does most of the quality work — Flash and Haiku produce near-identical outputs. Pay attention to total cost; you will run extraction on every chunk of every note you ever ingest. Default to whichever provider you already have a key for. Flash is the ship default; Haiku is the documented alternative (DESIGN_DOCUMENT §4.1).

**Reflection: upgrade selectively.** Reflection runs less often than extraction but each call is more expensive and the output more durable — a mental model lives until contradicted. Upgrade to Sonnet (or Gemini Pro) when *and only when* you can name a concrete complaint: "the mental models miss contradictions"; "the synthesis paragraphs read flat"; "I want better hedging on ambiguous evidence". If you cannot name the complaint, do not pay the upgrade.

**Embeddings: stick with the default.** The built-in ONNX model is fine-tuned, fast, free, and local. The only honest reasons to swap are (1) you have measurable retrieval-quality failures on your specific data and (2) you have a tested alternative in hand. If you are swapping because someone on the internet said OpenAI's embeddings are "better", you are about to make your vault worse and pay a per-call fee for the privilege.

If you decide later that you want to swap and your vault matters, plan for a re-embed migration. There is no in-place upgrade; the only path is a new vault or a wipe-and-reload.

## What you built

You ran the same small ingestion four times under three different extraction models and one different embedding backend. You watched LLM call counts, mean latency, and token usage shift as you changed the model — and watched the fact-extraction quality stay roughly constant. You read a reflection-cycle mental model written by Sonnet and noticed the differences from one written by Flash. You learned why the embedding model is the one swap you should not make casually.

Most importantly: you now have a way to evaluate a future model choice for yourself, on your own data, before you commit to it. The rule is the same as the rule for any infrastructure choice — *measure the change you actually got*, not the change someone else got on someone else's data.

## Next steps

- [How to configure Memex](../how-to/configuring-server/default-model.md)
- [Reference: configuration keys](../reference/configuration-options.md)
- [Explanation: inference model backends](../explanation/how-memex-works/retrieval.md)

## See also

- [Tutorial: Get started with Memex](getting-started.md)
- [How-to: Configure Memex](../how-to/configuring-server/default-model.md)
- [Reference: configuration options](../reference/configuration-options.md)
- [Explanation: Inference model backends](../explanation/how-memex-works/retrieval.md)
