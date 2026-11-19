# Provider targets (free-tier design §13; Requirements 16.10 and 16.11).
#
#   make workers-ai-live-check   USER-RUN (FU8): one tiny embed, rerank and text call against the
#                                real Cloudflare Workers AI API
#
# The target uses CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN from .env (put them there
# first; see docs/deploy.md). It makes three small calls and reserves under 100 of the 10,000
# free neurons of the day. This is the only place that sets CAIRN_LIVE_CHECK=1: the tests in
# packages/retrieval/tests/live skip with a message unless that variable and the token are both
# set, and `make test-unit`, `make test-integration` and the CI workflow exclude the `live`
# marker.
#
# It is not part of `make gates` and not in .github/workflows/ci.yml, and agents never run it:
# it spends the user's quota with the user's token (tests/scripts/test_live_check.py pins this).
.PHONY: workers-ai-live-check
workers-ai-live-check: ## User-run (FU8): three small real Workers AI calls (needs the token in .env)
	@echo "workers-ai-live-check: three small real calls to Cloudflare Workers AI (under 100 neurons)"
	CAIRN_LIVE_CHECK=1 $(UV_RUN) pytest -m live packages/retrieval/tests/live -rs
