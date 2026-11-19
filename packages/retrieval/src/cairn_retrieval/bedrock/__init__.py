"""Amazon Bedrock adapters: embeddings, answer/judge models and an LLM reranker.

All calls are made in one AWS Region (``CAIRN_BEDROCK_REGION``, ap-south-1 by default) with the
AWS credential chain: on EC2 that is the instance role, so no key is stored or configured. Every
attempt first reserves its estimated cost, in micro-dollars, against a daily cap (the same
``cairn_meta.provider_usage`` store as the Workers AI neuron budget), so a runaway loop cannot
spend past ``CAIRN_BEDROCK_DAILY_USD``.
"""

from cairn_retrieval.bedrock.adapters import BedrockEmbedder, BedrockLlm, BedrockLlmReranker
from cairn_retrieval.bedrock.client import BedrockClient

__all__ = ["BedrockClient", "BedrockEmbedder", "BedrockLlm", "BedrockLlmReranker"]
