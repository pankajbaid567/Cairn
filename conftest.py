"""Shared pytest setup for every test directory in the repo.

Hypothesis profiles (design §2), selected with the HYPOTHESIS_PROFILE
environment variable:

- ``dev``: 100 examples per property. The default on a laptop.
- ``ci``: 200 examples per property, on top of Hypothesis's built-in ``ci``
  profile (derandomized, no deadline, no example database, failure blobs
  printed). The default when Hypothesis detects a CI environment.

An unknown profile name fails the run at startup.

The database fixtures ``admin_config``, ``admin_conn`` and ``fresh_db`` come from
``cairn_core.testing`` (design §15). They skip when the compose db is not up.
"""

import os

from hypothesis import settings

from cairn_core.testing import admin_config, admin_conn, fresh_db
from cairn_eval.pytest_fixtures import seeded

PROFILE_ENV_VAR = "HYPOTHESIS_PROFILE"
PROFILE_EXAMPLES = {"dev": 100, "ci": 200}

# Hypothesis loads its own "ci" profile at import time when it detects CI.
_default_profile = "ci" if settings.get_current_profile_name() == "ci" else "dev"

settings.register_profile("dev", max_examples=PROFILE_EXAMPLES["dev"])
settings.register_profile(
    "ci", parent=settings.get_profile("ci"), max_examples=PROFILE_EXAMPLES["ci"]
)
settings.load_profile(os.environ.get(PROFILE_ENV_VAR, _default_profile))

# Re-exported so pytest finds the fixtures in every test directory.
__all__ = ["admin_config", "admin_conn", "fresh_db", "seeded"]
