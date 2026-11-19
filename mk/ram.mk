# Core profile RAM report (design §17; Requirement 20).
#
#   make ram-report   start the core profile, warm it up for 60 s, sample container memory
#                     every 5 s for 60 s and write docs/evidence/phase-0/core-ram-<date>.md
#                     and .json; exits 1 when the total peak is above 6 GiB
#
# It runs `docker compose --profile core up --wait` (with --build unless `UP_BUILD=` is
# empty, like `make up`), so a stack that is already up and current is left as it is; the
# database container is recreated only if its compose configuration changed. The report
# measures the db, api, worker and web containers of project `cairn` and lists any other
# running container without counting it.
#
# Options through RAM_ARGS, for example `make ram-report RAM_ARGS="--no-up --warmup 10"`:
#   --no-up            measure what is running; do not run `compose up`
#   --warmup/--duration/--interval   seconds (60/60/5)
#   --out-dir DIR      write the report somewhere else
# Exit codes: 0 within the ceiling, 1 over 6 GiB, 2 Docker or the stack is not usable.
# Needs Docker running; the Python script uses the standard library only.
RAM_ARGS ?=

.PHONY: ram-report
ram-report: ## Measure the core profile's memory and write docs/evidence/phase-0/core-ram-<date>.md/.json
	$(UV_RUN) python scripts/measure_ram.py $(if $(strip $(UP_BUILD)),--build) $(RAM_ARGS)
