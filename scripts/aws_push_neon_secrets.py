#!/usr/bin/env python3
"""Copy the Neon runtime settings from ``.env.neon`` into AWS Systems Manager Parameter Store.

Run by you, on your laptop, with your own AWS credentials (``AWS_PROFILE`` or ``--profile``):

    make aws-secrets            # or: python scripts/aws_push_neon_secrets.py --profile pankaj-aws

It sends only what the running app needs: the Neon host and the passwords of the ``cairn_app``,
``cairn_dbos`` and ``cairn_ingest`` roles, as SecureString parameters ``/cairn/<NAME>`` in
ap-south-1. The owner, admin and edge passwords never leave the laptop (a test enforces it). The
script prints parameter names and counts, never a value. It refuses an empty value and a pooled
Neon host (DBOS needs the direct endpoint).

Nothing here reads ``.env`` (the local compose file); only the file you name, ``.env.neon``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REGION = "ap-south-1"
PREFIX = "/cairn/"
PUSH_KEYS = (
    "CAIRN_DB_HOST",
    "CAIRN_APP_DB_PASSWORD",
    "CAIRN_DBOS_DB_PASSWORD",
    "CAIRN_INGEST_DB_PASSWORD",
)
NEVER_PUSH = (
    "CAIRN_DB_ADMIN_PASSWORD",
    "CAIRN_OWNER_DB_PASSWORD",
    "CAIRN_EDGE_DB_PASSWORD",
)
_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class SecretsError(Exception):
    """The input cannot be pushed. The message names keys, never values."""


def parse_env(text: str) -> dict[str, str]:
    """KEY=value lines; ``#`` comments and blank lines skipped; matching quotes stripped."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if m is None:
            continue
        value = m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[m.group(1)] = value
    return out


def select(values: Mapping[str, str]) -> dict[str, str]:
    """The parameters to push, validated. Raises ``SecretsError``."""
    problems = [k for k in PUSH_KEYS if not values.get(k)]
    if problems:
        raise SecretsError(f"empty or missing in .env.neon: {', '.join(problems)}")
    host = values["CAIRN_DB_HOST"]
    if "-pooler" in host:
        raise SecretsError("CAIRN_DB_HOST is a pooled endpoint; use the direct one (no -pooler)")
    if "neon.tech" not in host:
        raise SecretsError("CAIRN_DB_HOST does not look like a Neon host (*.neon.tech)")
    return {k: values[k] for k in PUSH_KEYS}


def push(ssm: Any, params: Mapping[str, str]) -> list[str]:
    names = []
    for key, value in params.items():
        ssm.put_parameter(
            Name=f"{PREFIX}{key}",
            Value=value,
            Type="SecureString",
            Overwrite=True,
            Tier="Standard",
            Description="Cairn runtime setting copied from .env.neon (aws_push_neon_secrets.py)",
        )
        names.append(f"{PREFIX}{key}")
    return names


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--env-file", type=Path, default=Path(".env.neon"))
    ap.add_argument("--profile", default=None, help="AWS profile (default: the environment's)")
    ap.add_argument("--region", default=REGION)
    ap.add_argument(
        "--dry-run", action="store_true", help="check the file and list names; send nothing"
    )
    a = ap.parse_args(argv)
    if a.env_file.name == ".env":
        print("refusing to read .env: pass the Neon file (.env.neon)", file=sys.stderr)
        return 2
    try:
        params = select(parse_env(a.env_file.read_text(encoding="utf-8")))
    except OSError:
        print(f"cannot read {a.env_file}", file=sys.stderr)
        return 2
    except SecretsError as exc:
        print(f"not pushed: {exc}", file=sys.stderr)
        return 2
    if a.dry_run:
        print("would push: " + ", ".join(f"{PREFIX}{k}" for k in params))
        return 0
    import boto3  # imported here: the checks above need no AWS

    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    names = push(session.client("ssm"), params)
    print(f"pushed {len(names)} parameters to {a.region}: " + ", ".join(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
