"""Deterministic extractive answer model for tests and CI (design §16).

With no evidence block in the prompt it abstains. Otherwise its claims are the first sentence of
each of the first two blocks, each citing its own label. It echoes evidence text, which makes it
the strictest leak detector for the canary matrix: if hidden text ever reached the prompt, it
would come out in the answer.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from cairn_retrieval.cloudflare.budget import estimate_tokens
from cairn_retrieval.llm.types import LlmRequest, LlmResponse

PROVIDER: Final = "fake"
MODEL: Final = "fake-extractive-v1"
_BLOCK: Final = re.compile(r'<evidence id="(E\d+)"[^>]*>\n(.*?)\n</evidence>', re.DOTALL)
_SENTENCE: Final = re.compile(r"(?<=[.!?])\s")


def _unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&amp;", "&")


class FakeProvider:
    provider = PROVIDER
    model = MODEL

    async def complete(self, req: LlmRequest) -> LlmResponse:
        blocks = [(m.group(1), _unescape(m.group(2))) for m in _BLOCK.finditer(req.user)]
        body: dict[str, Any]
        if not blocks:
            body = {"abstain": True}
        else:
            claims = []
            for lab, text in blocks[:2]:
                first = _SENTENCE.split(text.strip(), maxsplit=1)[0].strip()
                claims.append({"text": first, "evidence": [lab]})
            body = {
                "abstain": False,
                "answer": " ".join(f"{c['text']} [{c['evidence'][0]}]" for c in claims),
                "claims": claims,
            }
        text = json.dumps(body)
        return LlmResponse(
            text=text,
            tokens_in=estimate_tokens(req.system) + estimate_tokens(req.user),
            tokens_out=estimate_tokens(text),
            latency_ms=0.0,
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
        )
