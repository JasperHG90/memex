"""LiteLLM-backed NLI classifier adapter.

Wraps ``litellm.acompletion`` to satisfy the ``NLIClassifierModel`` protocol,
allowing any litellm-supported chat completion provider to be used as the
NLI backend.

NLI classification is performed by sending a structured prompt that asks the
model to classify the relationship between a premise and hypothesis as
entailment, neutral, or contradiction, and return JSON probabilities.
"""

from __future__ import annotations

import json
import logging

import litellm

from memex_common.config import LitellmNLIBackend

logger = logging.getLogger('memex.core.memory.models.backends.litellm_nli')

_SYSTEM_PROMPT = (
    'You are a natural language inference classifier. Given a premise and a '
    'hypothesis, classify their relationship as one of: entailment, neutral, '
    'contradiction.\n\n'
    'Return ONLY a JSON object with three keys: "entailment", "neutral", and '
    '"contradiction", where each value is a probability (float between 0 and 1) '
    'and the three values sum to approximately 1.0.\n\n'
    'Example output:\n'
    '{"entailment": 0.85, "neutral": 0.10, "contradiction": 0.05}'
)

_USER_TEMPLATE = (
    'Premise: {premise}\n\n'
    'Hypothesis: {hypothesis}\n\n'
    'Classify the relationship between the premise and hypothesis. '
    'Return only the JSON object with probabilities.'
)


class LiteLLMNLI:
    """NLI classifier adapter backed by litellm chat completion.

    Satisfies ``NLIClassifierModel`` protocol via structural subtyping.
    ``classify()`` is async and calls ``litellm.acompletion`` directly —
    no ``asyncio.to_thread`` wrapping is needed since litellm provides
    native async support.
    """

    def __init__(self, config: LitellmNLIBackend) -> None:
        self._model = config.model
        self._api_base = str(config.api_base) if config.api_base else None
        self._api_key = config.api_key.get_secret_value() if config.api_key else None
        logger.info(
            'LiteLLM NLI classifier initialised: model=%s api_base=%s',
            self._model,
            self._api_base,
        )

    @property
    def model_version(self) -> str:
        return f'litellm:{self._model}'

    async def classify(self, premise: str, hypothesis: str) -> dict[str, float]:
        kwargs: dict = {}
        if self._api_base:
            kwargs['api_base'] = self._api_base
        if self._api_key:
            kwargs['api_key'] = self._api_key

        user_message = _USER_TEMPLATE.format(premise=premise, hypothesis=hypothesis)

        response = await litellm.acompletion(
            model=self._model,
            messages=[
                {'role': 'system', 'content': _SYSTEM_PROMPT},
                {'role': 'user', 'content': user_message},
            ],
            temperature=0.0,
            **kwargs,
        )

        content = response.choices[0].message.content
        return _parse_nli_response(content)


def _parse_nli_response(content: str) -> dict[str, float]:
    """Parse the LLM response into a probabilities dict.

    Handles common failure modes: extra text before/after JSON, markdown
    code fences, and missing keys. Raises ``ValueError`` on unparseable
    output so the caller (PolarityClassifier) can fall back gracefully.
    """
    text = content.strip()
    if text.startswith('```'):
        first_newline = text.index('\n') if '\n' in text else len(text)
        text = text[first_newline + 1 :] if first_newline < len(text) else ''
        if text.endswith('```'):
            text = text[:-3].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and start < end:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                raise ValueError(f'NLI classifier returned unparseable response: {content!r}')
        else:
            raise ValueError(f'NLI classifier returned unparseable response: {content!r}')

    if not isinstance(parsed, dict):
        raise ValueError(f'NLI classifier returned non-dict response: {content!r}')

    normalised = {
        k.lower(): float(v)
        for k, v in parsed.items()
        if k.lower() in ('entailment', 'neutral', 'contradiction')
    }

    required = {'entailment', 'neutral', 'contradiction'}
    missing = required - set(normalised.keys())
    if missing:
        raise ValueError(f'NLI classifier response missing keys {missing}: {content!r}')

    total = sum(normalised.values())
    if total <= 0:
        raise ValueError(f'NLI classifier probabilities sum to {total}, expected > 0: {content!r}')
    return {k: v / total for k, v in normalised.items()}
