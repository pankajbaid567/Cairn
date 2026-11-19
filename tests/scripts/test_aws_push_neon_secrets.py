"""``scripts/aws_push_neon_secrets.py``: what it sends, what it refuses, no value ever printed."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("push", ROOT / "scripts/aws_push_neon_secrets.py")
assert spec is not None
assert spec.loader is not None
push = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push)

HOST = "ep-quiet-wave-123456.ap-southeast-1.aws.neon.tech"
ENV = f"""
# comment
CAIRN_ENV=demo
CAIRN_DB_HOST={HOST}
CAIRN_DB_ADMIN_PASSWORD=ADMIN-SECRET-VALUE
CAIRN_OWNER_DB_PASSWORD=OWNER-SECRET-VALUE
CAIRN_APP_DB_PASSWORD="APP-SECRET-VALUE"
CAIRN_DBOS_DB_PASSWORD='DBOS-SECRET-VALUE'
CAIRN_INGEST_DB_PASSWORD=INGEST-SECRET-VALUE
CAIRN_EDGE_DB_PASSWORD=EDGE-SECRET-VALUE
"""


class FakeSsm:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def put_parameter(self, **kw: object) -> None:
        self.calls.append(kw)


def test_parse_strips_quotes_and_skips_comments() -> None:
    v = push.parse_env(ENV)
    assert v["CAIRN_APP_DB_PASSWORD"] == "APP-SECRET-VALUE"
    assert v["CAIRN_DBOS_DB_PASSWORD"] == "DBOS-SECRET-VALUE"
    assert "#" not in "".join(v)


def test_only_the_runtime_keys_are_selected_never_owner_admin_or_edge() -> None:
    params = push.select(push.parse_env(ENV))
    assert set(params) == set(push.PUSH_KEYS)
    assert not set(params) & set(push.NEVER_PUSH)
    assert not set(push.PUSH_KEYS) & set(push.NEVER_PUSH)


def test_push_writes_securestrings_under_the_cairn_prefix_and_overwrites() -> None:
    ssm = FakeSsm()
    names = push.push(ssm, push.select(push.parse_env(ENV)))
    assert names == [f"/cairn/{k}" for k in push.PUSH_KEYS]
    assert {c["Type"] for c in ssm.calls} == {"SecureString"}
    assert all(c["Overwrite"] is True and str(c["Name"]).startswith("/cairn/") for c in ssm.calls)
    sent = {str(c["Value"]) for c in ssm.calls}
    for never in ("ADMIN-SECRET-VALUE", "OWNER-SECRET-VALUE", "EDGE-SECRET-VALUE"):
        assert never not in sent


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda v: v.update(CAIRN_APP_DB_PASSWORD=""), "CAIRN_APP_DB_PASSWORD"),
        (lambda v: v.pop("CAIRN_DB_HOST"), "CAIRN_DB_HOST"),
        (lambda v: v.update(CAIRN_DB_HOST="ep-x-pooler.ap-southeast-1.aws.neon.tech"), "pooled"),
        (lambda v: v.update(CAIRN_DB_HOST="db.example.com"), "Neon host"),
    ],
)
def test_invalid_input_is_refused_naming_keys_not_values(mutate, message) -> None:  # type: ignore[no-untyped-def]
    values = push.parse_env(ENV)
    mutate(values)
    with pytest.raises(push.SecretsError, match=message) as exc:
        push.select(values)
    assert not any(
        secret in str(exc.value) for secret in ("APP-SECRET", "DBOS-SECRET", "INGEST-SECRET")
    )


def test_dry_run_prints_names_only_and_refuses_the_local_env_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / ".env.neon"
    f.write_text(ENV)
    assert push.main(["--env-file", str(f), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "/cairn/CAIRN_APP_DB_PASSWORD" in out
    assert "SECRET-VALUE" not in out
    assert HOST not in out
    plain = tmp_path / ".env"
    plain.write_text(ENV)
    assert push.main(["--env-file", str(plain), "--dry-run"]) == 2
    assert "refusing to read .env" in capsys.readouterr().err
    assert push.main(["--env-file", str(tmp_path / "missing"), "--dry-run"]) == 2
