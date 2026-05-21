# Set a default LLM model

This guide shows you how to set the default LLM that Memex uses for every server-side language-model call — extraction, write-time classification, reflection, query expansion, contradiction detection, document synthesis, and vault summarization. You will set one global default, then optionally override the model on a per-stage basis, then restart the server and confirm the new model is in use.

The default model lives at `server.default_model`. Every sub-stage with `model: null` inherits it when the server boots, so a single change to `server.default_model` is usually all you need.

## Prerequisites

- A running Memex install (`memex --version` returns a version).
- A reachable Postgres metastore — `memex config show` prints without errors.
- An API key for the provider you intend to use, unless you are running locally with Ollama.
- Write access to either `~/.config/memex/config.yaml` (global) or a `.memex.yaml` in your project root.

## Procedure

### Set a global default model

Edit your global config file and set `server.default_model`:

```yaml
server:
  default_model:
    model: 'gemini/gemini-3-flash-preview'
    api_key: '${GEMINI_API_KEY}'
    temperature: 0.0
    timeout: 120
    num_retries: 3
```

The `model` string is a LiteLLM provider identifier. Examples that work today: `gemini/gemini-3-flash-preview`, `openai/gpt-4o-mini`, `anthropic/claude-haiku-4-5`, `ollama_chat/llama3`. The minimum-required field is `model`; the rest carry sane defaults — `timeout` defaults to 120 seconds, `num_retries` to 3. `base_url` is optional and only needed for self-hosted endpoints.

For local inference with Ollama, leave the key out and point at the daemon:

```yaml
server:
  default_model:
    model: 'ollama_chat/llama3'
    base_url: 'http://localhost:11434'
```

You can also set the model from the environment, which is the cleanest path in CI:

```bash
export MEMEX_SERVER__DEFAULT_MODEL__MODEL='openai/gpt-4o-mini'
export MEMEX_SERVER__DEFAULT_MODEL__API_KEY="$OPENAI_API_KEY"
```

### Override the model for a specific stage

The default propagates to every stage with `model: null`. To upgrade one stage without touching the others, set its `model` block explicitly. The stages that inherit the default are:

- `server.memory.extraction.model` — fact extraction (every chunk).
- `server.memory.extraction.text_splitting.model` — PageIndex structural scan on large documents.
- `server.memory.reflection.model` — per-entity reflection (Phases 0/1/3/4/6) and vault summarization-by-extension.
- `server.memory.contradiction.model` — contradiction classification at extraction time.
- `server.document.model` — document-search skeleton-tree reasoning and answer synthesis.
- `server.vault_summary.model` — vault-summary LLM calls.

A common pattern: keep extraction on a cheap fast model, upgrade reflection to a stronger one because reflection is where mental-model quality compounds.

```yaml
server:
  default_model:
    model: 'gemini/gemini-3-flash-preview'
    api_key: '${GEMINI_API_KEY}'

  memory:
    reflection:
      model:
        model: 'anthropic/claude-sonnet-4-6'
        api_key: '${ANTHROPIC_API_KEY}'
```

Query expansion does not have its own `model` field. It reuses the extraction model — Memex builds one DSPy LM from `extraction.model` after the default-propagation step and passes it to the retrieval engine. To change the query-expansion model, change the extraction model.

Reranking uses a separate model backend (`server.memory.retrieval.reranker`), not a `ModelConfig`. It defaults to the built-in ONNX cross-encoder. To switch to a LiteLLM reranker, see the reranker how-to linked below — the `default_model` setting does not affect it.

The write-time intent and risk classifier was folded into the extraction signature itself. It runs as part of the extraction call and uses the same model. You can disable it with `server.memory.extraction.intent_risk_classifier_enabled: false`, but you cannot give it a different model.

### Restart the server

The model is read at startup. Stop the running server, then start it again:

```bash
memex server stop
memex server start
```

If you ran the server in the foreground, kill it and start it again the same way. Memex reads the config file on boot and resolves `default_model` into each sub-stage during the Pydantic validation pass — there is no live reload.

## Verification

Print the resolved config and confirm the model strings line up:

```bash
memex config show | grep -A2 'model:'
```

You should see your chosen model under `default_model` and under every stage that did not get an explicit override.

Then ingest a small note and watch the LLM call land. Start the server in the foreground so the extraction log is visible, then in a second terminal:

```bash
memex note add --title "model-check" --content "Testing the new default model."
```

The first terminal prints a log line that names the model and reports a successful response. If the model string is wrong, the extraction step raises early and the note is not saved. If the API key is wrong, the LLM call returns an auth error and you will see it in the log.

For a deeper check, query the metrics endpoint:

```bash
curl -s http://localhost:8000/api/v1/metrics | grep memex_llm
```

The `memex_llm_calls_total` counter increments per stage. Compare the labels against the stages you configured to confirm each one ran on the model you expected.

## Troubleshooting

### "Model not recognized" or LiteLLM provider error

The `model` string must be a LiteLLM identifier with the provider prefix. `gpt-4o` is wrong; `openai/gpt-4o` is right. `llama3` is wrong; `ollama_chat/llama3` is right. Check the LiteLLM docs for the exact identifier your provider expects, then re-run `memex config show` and confirm the string.

### "API key missing" or `AuthenticationError`

If you used `${VAR}` interpolation, confirm the variable is exported in the shell that started the server — not just in your terminal. For a daemonized server, set the variable in the unit file or the launcher script. As a quick check, `memex config show` masks the key but prints `**********` when the field resolved successfully and an empty string when it did not.

If you set the key inline rather than through an env var, make sure it sits under the same `model` block as the `model` string — a key under the wrong stage does not propagate down from `default_model`.

### Rate limit or quota errors

The provider returned 429. Memex retries `num_retries` times (default 3) with exponential backoff, then raises. Two things to try:

- Reduce concurrency for the offending stage. For extraction, set `server.memory.extraction.max_concurrency` to something lower than the default 5.
- Raise `num_retries` and `timeout` on the model block. The `timeout` field has a minimum of 10 seconds; `num_retries` has a minimum of 1.

If you keep hitting the limit, you need either a higher-tier API plan or a cheaper model for the high-volume stages (extraction and the classifier). Reflection runs orders of magnitude less often, so leaving a stronger model there is usually fine.

### LLM call hangs or never returns

A slow provider — most often a local Ollama instance — can stall the call past the default 120-second timeout. Raise `timeout` on the relevant model block. For genuinely slow remote endpoints, 300 seconds is a reasonable upper bound; longer than that and you are masking a different bug.

### Fallback behaviour

Memex does not silently swap models when the configured one fails. An auth error, an unknown-model error, or an exceeded retry budget surfaces as a failed extraction or a failed reflection cycle. The exception is the surprise-gated lint pass and the write-time classifier, which have rate-cap and feature-flag fallbacks documented in their own how-tos.

If you want graceful degradation under provider outage, use a second config file with a different `default_model` and swap configs with `MEMEX_CONFIG_PATH` rather than relying on Memex to switch for you.

## See also

- [Tutorial: Getting started](../../tutorials/getting-started.md)
- [How-to: Configure Memex](../configure-memex.md)
- [Reference: Configuration options](../../reference/configuration.md)
- [Explanation: Inference model backends](../../explanation/inference-model-backends.md)
