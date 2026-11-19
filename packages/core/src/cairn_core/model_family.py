"""Model families for the answer and judge rule (free-tier FT-24, Requirement 16.7).

The answer model and the judge model must come from different model families, so a judge
never grades the output of its own family (plan D16). The settings validator in
``cairn_core.config`` needs the rule, so it lives in core; ``cairn_retrieval.llm.family``
re-exports it.
"""

from __future__ import annotations

from typing import Final

FAKE_PROVIDER: Final = "fake"
"""Provider name of the deterministic fakes."""

FAKE_FAMILY: Final = "fake"
"""Family of every model served by the fake provider."""

FAMILY_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("@cf/google/", "google"),
    ("@cf/zai-org/", "zhipu"),
    ("@cf/meta/", "meta"),
    ("@cf/qwen/", "alibaba"),
    ("@cf/mistral", "mistral"),
    ("@cf/nvidia/", "nvidia"),
    ("@cf/openai/", "openai"),
    ("@cf/deepseek-ai/", "deepseek"),
    ("@cf/ibm-granite/", "ibm"),
    # Amazon Bedrock model ids (``<vendor>.<model>``); each vendor is one family.
    ("openai.", "openai"),
    ("qwen.", "alibaba"),
    ("zai.", "zhipu"),
    ("anthropic.", "anthropic"),
    ("amazon.", "amazon"),
    ("deepseek.", "deepseek"),
    ("mistral.", "mistral"),
    ("moonshotai.", "moonshot"),
    ("moonshot.", "moonshot"),
    ("google.", "google"),
    ("meta.", "meta"),
    ("nvidia.", "nvidia"),
    ("minimax.", "minimax"),
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o", "openai"),
)
"""Model-name prefix to family, first match wins. The bare ``o`` prefix (``o1``, ``o3``) applies
only to provider ``openai``, so it never claims a Workers AI model."""

_BARE_PREFIX: Final = "o"
_BARE_PREFIX_PROVIDER: Final = "openai"


class UnknownModelFamily(ValueError):
    """No prefix in ``FAMILY_PREFIXES`` matches the model. Add one before using the model."""

    def __init__(self, provider: str, model: str) -> None:
        super().__init__(f"unknown model family for provider {provider!r}, model {model!r}")
        self.provider = provider
        self.model = model


def model_family(provider: str, model: str) -> str:
    """The family of ``model`` served by ``provider``.

    ``"fake"`` for the fake provider. Otherwise the family of the first matching prefix; the
    bare ``o`` prefix only counts for provider ``openai``. Raises ``UnknownModelFamily``.
    """
    if provider == FAKE_PROVIDER:
        return FAKE_FAMILY
    for prefix, family in FAMILY_PREFIXES:
        if prefix == _BARE_PREFIX and provider != _BARE_PREFIX_PROVIDER:
            continue
        if model.startswith(prefix):
            return family
    raise UnknownModelFamily(provider, model)
