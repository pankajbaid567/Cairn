# Cairn task runner (design §10, PD-17).
#
# This file holds the shared variables. Each area adds its targets in one
# fragment under mk/ (python.mk, env.mk, db.mk, ...), so tasks never have to
# edit the same file. CI calls the same targets.
#
# Run from the repo root (`make <target>`) or with `make -C <repo root>`.
# Works with GNU Make 3.81 (macOS) and 4.x (CI). No target needs git.

ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

# Every recipe line runs in bash with errexit, nounset and pipefail. GNU Make
# 3.81 has no .SHELLFLAGS, so the flags ride on SHELL.
SHELL := /bin/bash -eu -o pipefail

.SUFFIXES:
.DELETE_ON_ERROR:
.DEFAULT_GOAL := help

# uv runs every Python tool in the locked workspace environment with all
# members installed, and fails instead of rewriting uv.lock. The interpreter is
# always uv-managed CPython 3.12 (python-preference in pyproject.toml).
UV ?= uv
UV_RUN := $(UV) run --locked --all-packages

# Property tests read HYPOTHESIS_PROFILE (dev: 100 examples, ci: 200), for
# example `make test-unit HYPOTHESIS_PROFILE=ci`. Unset, conftest.py picks dev,
# or ci when Hypothesis detects a CI environment.

.PHONY: help
help: ## List the available targets
	@awk 'BEGIN { FS = ":.*## " } /^[a-zA-Z0-9_.-]+:.*## / { printf "  %-18s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

include $(sort $(wildcard $(ROOT)/mk/*.mk))
