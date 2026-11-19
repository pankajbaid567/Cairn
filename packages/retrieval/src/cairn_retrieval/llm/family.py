"""Model families for the answer and judge rule. The rule lives in ``cairn_core.model_family``
(the settings validator needs it); this module re-exports it for retrieval code."""

from cairn_core.model_family import (
    FAKE_FAMILY,
    FAKE_PROVIDER,
    FAMILY_PREFIXES,
    UnknownModelFamily,
    model_family,
)

__all__ = ["FAKE_FAMILY", "FAKE_PROVIDER", "FAMILY_PREFIXES", "UnknownModelFamily", "model_family"]
