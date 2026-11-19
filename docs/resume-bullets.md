# Resume bullets (measured numbers only)

Each bullet names its evidence. Do not add a number that is not in a file listed here. Do not claim anything about answer quality, deployment or real users: none of that is measured yet.

* Designed and built a permission-aware retrieval system in **Python and PostgreSQL**: ACLs enforced twice (row-level security and explicit query predicates), with a **50-test canary matrix and 0 observed leaks** across search, answers, decisions, sync states and all five MCP tools. Evidence: `docs/evidence/phase-5/matrix-2026-11-04.txt`.
* Modelled engineering decisions as **bitemporal records** (valid time x recorded time, GiST exclusion constraint) so questions can be answered as of a date and as known on a date; found and fixed a leak where a stored rectangle exposed the timing of events a reader could not see. Evidence: `docs/temporal-model.md`, `test_decisions.py`.
* Built fail-closed **live permission sync** (edge webhook receiver with an insert-only database role, ACL-first drain, 503 until applied): freshness **p95 0.78 s** and revocation fail-closed **p95 0.015 s** over 20 local trials each against a fake GitHub (excludes network and cold start). Evidence: `docs/evidence/phase-3/timing/`.
* Added an **answer verifier** and a read-only **MCP server** (5 tools, per-agent signed tokens, injected-instruction test): unsupported claims are dropped and the reply falls back to one constant "not found". Evidence: `docs/security.md`, `test_verify.py`, `test_injection.py`.
* Chose the vector ACL-filter strategy from a **benchmark at 1/5/20/50% selectivity**: recall@10 **0.99** at 20% against 0.75 for the default setting, on synthetic vectors. Evidence: `docs/spikes/S1/2026-11-04-report.md`.
* Ran the whole system inside a **512 MB, 0.1 CPU free-tier container at about 102 MiB** and fixed a shutdown regression found only by the gate. Evidence: `docs/evidence/free-tier/`.

Do not say: "deployed", "production", "accurate answers", "Recall@5 of ..." (the only run uses fake models), or any number from the fake-model tables as a quality result.
