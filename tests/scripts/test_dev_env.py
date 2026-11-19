"""Env generator: placeholders, overwrite refusal, file mode, append-only update.

Phase 0 Requirement 22; free-tier Requirement 13.
"""

import datetime as dt
import itertools
import re
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import dev_env
from scripts.dev_env import PLACEHOLDER, USER_PROVIDED, EnvTemplateError

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / ".env.example"
NEON_EXAMPLE = REPO_ROOT / ".env.neon.example"
TEMPLATES = [EXAMPLE, NEON_EXAMPLE]

SECRET_KEYS = [
    "POSTGRES_PASSWORD",
    "CAIRN_OWNER_DB_PASSWORD",
    "CAIRN_APP_DB_PASSWORD",
    "CAIRN_DBOS_DB_PASSWORD",
    "CAIRN_INGEST_DB_PASSWORD",
    "CAIRN_EDGE_DB_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
    "CAIRN_MANAGED_ADMIN_PASSWORD",
    "CAIRN_PROXY_TOKEN",
    "CAIRN_SESSION_SECRET",
    "CAIRN_AUDIT_HASH_KEY",
]
USER_PROVIDED_KEYS = [
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CAIRN_GITHUB_ORG",
    "CAIRN_GITHUB_APP_ID",
    "CAIRN_GITHUB_INSTALLATION_ID",
    "CAIRN_GITHUB_PRIVATE_KEY",
    "CAIRN_GITHUB_WEBHOOK_SECRET",
    "CAIRN_GITHUB_USER_MAP",
]
REQUIRED_KEYS = [
    *SECRET_KEYS,
    *USER_PROVIDED_KEYS,
    "CAIRN_ENV",
    "CAIRN_LOG_LEVEL",
    "CAIRN_DB_HOST",
    "CAIRN_DB_PORT",
    "CAIRN_DB_NAME",
    "CAIRN_DB_TIMEOUT_S",
    "CAIRN_APP_DB_USER",
    "CAIRN_DBOS_DB_NAME",
    "CAIRN_DB_MEM_LIMIT",
    "CAIRN_HEARTBEAT_CRON",
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "CAIRN_TEST_ADMIN_HOST",
    "CAIRN_TEST_ADMIN_PORT",
    "CAIRN_TEST_ADMIN_USER",
    "CAIRN_DB_SSLMODE",
    "CAIRN_FREETIER_DB_PORT",
    "CAIRN_FREETIER_APP_PORT",
    "CAIRN_WORKERS_AI_BASE_URL",
    "CAIRN_WORKERS_AI_DAILY_NEURONS",
    "CAIRN_WORKERS_AI_BUDGET_STORE",
    "CAIRN_WORKERS_AI_CONNECT_TIMEOUT_S",
    "CAIRN_WORKERS_AI_READ_TIMEOUT_S",
    "CAIRN_WORKERS_AI_MAX_ATTEMPTS",
    "CAIRN_EMBEDDER",
    "CAIRN_EMBED_MODEL",
    "CAIRN_EMBED_DIM",
    "CAIRN_EMBED_QUERY_PREFIX",
    "CAIRN_RERANKER",
    "CAIRN_RERANK_MODEL",
    "CAIRN_ANSWER_PROVIDER",
    "CAIRN_ANSWER_MODEL",
    "CAIRN_JUDGE_PROVIDER",
    "CAIRN_JUDGE_MODEL",
]
# design §14, in file order
NEON_KEYS = [
    "CAIRN_ENV",
    "CAIRN_DB_HOST",
    "CAIRN_DB_PORT",
    "CAIRN_DB_NAME",
    "CAIRN_DB_SSLMODE",
    "CAIRN_DB_SSLROOTCERT",
    "CAIRN_DB_ADMIN_USER",
    "CAIRN_DB_ADMIN_DATABASE",
    "CAIRN_DB_ADMIN_MODE",
    "CAIRN_EMBED_DIM",
    "CAIRN_DB_ADMIN_PASSWORD",
    "CAIRN_OWNER_DB_PASSWORD",
    "CAIRN_APP_DB_PASSWORD",
    "CAIRN_DBOS_DB_PASSWORD",
    "CAIRN_INGEST_DB_PASSWORD",
    "CAIRN_EDGE_DB_PASSWORD",
]

TEMPLATE = """\
# comment with a = sign and change-me inside
PLAIN=value
POSTGRES_PASSWORD=change-me

QUOTED="change-me"
SINGLE='change-me'
WITH_COMMENT=change-me # note
LOOKS_LIKE=change-me-later
SPACED = change-me
CRON="*/30 * * * * *"
EMPTY=
"""


def counter_source() -> Callable[[], str]:
    """A deterministic token source: token-000...-1, token-000...-2, ..."""
    numbers = itertools.count(1)
    return lambda: f"token-{next(numbers):022d}"


def parse(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


def test_each_placeholder_is_replaced_in_file_order() -> None:
    out = dev_env.render(TEMPLATE, counter_source()).splitlines()
    assert out[2] == "POSTGRES_PASSWORD=token-0000000000000000000001"
    # Quotes around a placeholder are dropped: tokens are URL-safe and need none.
    assert out[4] == "QUOTED=token-0000000000000000000002"
    assert out[5] == "SINGLE=token-0000000000000000000003"
    assert out[6] == "WITH_COMMENT=token-0000000000000000000004 # note"
    assert out[8] == "SPACED = token-0000000000000000000005"


def test_everything_else_is_copied_unchanged() -> None:
    template_lines = TEMPLATE.splitlines()
    out = dev_env.render(TEMPLATE, counter_source()).splitlines()
    assert len(out) == len(template_lines)
    for index in (0, 1, 3, 7, 9, 10):
        assert out[index] == template_lines[index]
    assert PLACEHOLDER in out[0]  # comments are never touched
    assert out[7] == "LOOKS_LIKE=change-me-later"  # only the exact placeholder is a secret


def test_secret_keys_lists_placeholder_keys_only() -> None:
    assert dev_env.secret_keys(TEMPLATE) == [
        "POSTGRES_PASSWORD",
        "QUOTED",
        "SINGLE",
        "WITH_COMMENT",
        "SPACED",
    ]


def test_render_ends_with_one_newline() -> None:
    assert dev_env.render("A=1", counter_source()) == "A=1\n"
    assert dev_env.render("A=1\n", counter_source()) == "A=1\n"


@pytest.mark.parametrize(
    "bad_token",
    ["short", PLACEHOLDER, "x" * 30 + " y", "x" * 30 + '"', "x" * 30 + "#"],
)
def test_render_rejects_unusable_tokens(bad_token: str) -> None:
    with pytest.raises(EnvTemplateError):
        dev_env.render("A=change-me\n", lambda: bad_token)


def test_default_token_source_is_long_random_and_url_safe() -> None:
    tokens = {dev_env.random_token() for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) >= 24 and re.fullmatch(r"[A-Za-z0-9_-]+", t) for t in tokens)


def test_write_env_creates_the_file_with_mode_0600(tmp_path: Path) -> None:
    template = tmp_path / ".env.example"
    template.write_text(TEMPLATE, encoding="utf-8")
    output = tmp_path / ".env"

    count = dev_env.write_env(template, output, counter_source())

    assert count == 5
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.read_text(encoding="utf-8") == dev_env.render(TEMPLATE, counter_source())


def test_write_env_refuses_to_overwrite_and_leaves_the_file_alone(tmp_path: Path) -> None:
    template = tmp_path / ".env.example"
    template.write_text(TEMPLATE, encoding="utf-8")
    output = tmp_path / ".env"
    output.write_text("MINE=1\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        dev_env.write_env(template, output, counter_source())

    assert output.read_text(encoding="utf-8") == "MINE=1\n"


def test_write_env_refuses_a_dangling_symlink(tmp_path: Path) -> None:
    template = tmp_path / ".env.example"
    template.write_text(TEMPLATE, encoding="utf-8")
    output = tmp_path / ".env"
    output.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(FileExistsError):
        dev_env.write_env(template, output, counter_source())

    assert not (tmp_path / "elsewhere").exists()


def test_main_exit_codes_and_silence_about_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = tmp_path / ".env.example"
    template.write_text(TEMPLATE, encoding="utf-8")
    output = tmp_path / ".env"
    args = ["--template", str(template), "--output", str(output)]

    assert dev_env.main(args) == 0
    out = capsys.readouterr()
    written = parse(output.read_text(encoding="utf-8"))
    assert all(written[key] not in out.out + out.err for key in ("POSTGRES_PASSWORD", "QUOTED"))

    before = output.read_bytes()
    assert dev_env.main(args) == 1
    assert "already exists" in capsys.readouterr().err
    assert output.read_bytes() == before

    missing = ["--template", str(tmp_path / "nope"), "--output", str(tmp_path / ".env2")]
    assert dev_env.main(missing) == 2
    assert not (tmp_path / ".env2").exists()


def test_example_declares_every_required_variable_once() -> None:
    keys = [line.key for line in dev_env.parse_template(EXAMPLE.read_text("utf-8")) if line.key]
    assert len(keys) == len(set(keys))
    assert set(REQUIRED_KEYS) <= set(keys)


def test_example_placeholders_are_the_expected_keys() -> None:
    text = EXAMPLE.read_text("utf-8")
    assert dev_env.secret_keys(text) == SECRET_KEYS
    assert dev_env.user_provided_keys(text) == USER_PROVIDED_KEYS


def test_example_selects_the_fake_providers() -> None:
    values = parse(EXAMPLE.read_text("utf-8"))
    for key in (
        "CAIRN_EMBEDDER",
        "CAIRN_RERANKER",
        "CAIRN_ANSWER_PROVIDER",
        "CAIRN_JUDGE_PROVIDER",
    ):
        assert values[key] == "fake"


def test_example_no_longer_mentions_paradedb() -> None:
    assert "paradedb" not in EXAMPLE.read_text("utf-8").lower()


def test_neon_example_matches_the_design() -> None:
    text = NEON_EXAMPLE.read_text("utf-8")
    assert [line.key for line in dev_env.parse_template(text) if line.key] == NEON_KEYS
    assert dev_env.secret_keys(text) == [
        "CAIRN_OWNER_DB_PASSWORD",
        "CAIRN_APP_DB_PASSWORD",
        "CAIRN_DBOS_DB_PASSWORD",
        "CAIRN_INGEST_DB_PASSWORD",
        "CAIRN_EDGE_DB_PASSWORD",
    ]
    assert dev_env.user_provided_keys(text) == ["CAIRN_DB_HOST", "CAIRN_DB_ADMIN_PASSWORD"]
    assert parse(text)["CAIRN_DB_ADMIN_MODE"] == "managed"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_credential_like_variable_is_a_placeholder(template: Path) -> None:
    lines = dev_env.parse_template(template.read_text("utf-8"))
    credential_like = re.compile(r"PASSWORD|SECRET|TOKEN|KEY", re.IGNORECASE)
    for line in lines:
        if line.key and credential_like.search(line.key):
            assert line.secret or line.user_provided, (
                f"{line.key} must be {PLACEHOLDER} or {USER_PROVIDED}"
            )


def test_generated_example_has_no_placeholders_and_long_secrets(tmp_path: Path) -> None:
    output = tmp_path / ".env"
    dev_env.write_env(EXAMPLE, output)

    generated = parse(output.read_text("utf-8"))
    template = parse(EXAMPLE.read_text("utf-8"))
    assert list(generated) == list(template)  # same keys, same order
    for key, value in template.items():
        if key in SECRET_KEYS:
            assert len(generated[key]) >= 24
            assert generated[key] != PLACEHOLDER
        elif key in USER_PROVIDED_KEYS:
            assert generated[key] == ""
        else:
            assert generated[key] == value
    assert len({generated[k] for k in SECRET_KEYS}) == len(SECRET_KEYS)
    assert PLACEHOLDER not in generated.values()
    assert USER_PROVIDED not in generated.values()


def test_gitignore_ignores_env_but_not_the_example() -> None:
    patterns = (REPO_ROOT / ".gitignore").read_text("utf-8").splitlines()
    assert ".env" in patterns
    for negation in ("!.env.example", "!.env.neon.example"):
        assert negation in patterns
        assert patterns.index(negation) > patterns.index(".env.*")


# --- user-provided and the append-only update (free-tier Requirement 13) --------

TODAY = dt.date(2026, 11, 1)
UPDATE_TEMPLATE = """\
# template comment
CAIRN_DB_PORT=5432
POSTGRES_PASSWORD=change-me
NEW_SECRET=change-me
NEW_PLAIN=plain-value
NEW_API_KEY=user-provided
NEW_QUOTED_KEY="user-provided"
"""
EXISTING = """\
# my local settings
CAIRN_DB_PORT=55432

POSTGRES_PASSWORD=already-set-secret-value-0000
LOCAL_ONLY="kept as is"
# NEW_PLAIN=commented-out-does-not-count
"""


def test_user_provided_is_written_as_an_empty_value() -> None:
    template = 'API_KEY=user-provided\nQUOTED="user-provided"\nLATER=user-provided-later\n'
    assert dev_env.render(template, counter_source()) == (
        "API_KEY=\nQUOTED=\nLATER=user-provided-later\n"
    )
    assert dev_env.user_provided_keys(template) == ["API_KEY", "QUOTED"]
    assert dev_env.secret_keys(template) == []


def test_add_missing_appends_only_absent_keys_after_a_dated_comment() -> None:
    suffix, keys = dev_env.add_missing(EXISTING, UPDATE_TEMPLATE, counter_source(), TODAY)
    assert keys == ["NEW_SECRET", "NEW_PLAIN", "NEW_API_KEY", "NEW_QUOTED_KEY"]
    assert suffix.splitlines() == [
        "# Added by make env-update on 2026-11-01: keys missing from the template.",
        "NEW_SECRET=token-0000000000000000000001",
        "NEW_PLAIN=plain-value",
        "NEW_API_KEY=",
        "NEW_QUOTED_KEY=",
    ]
    assert suffix.endswith("\n")


def test_add_missing_adds_a_newline_before_the_comment_when_the_file_lacks_one() -> None:
    suffix, _ = dev_env.add_missing("A=1", "A=x\nB=2\n", counter_source(), TODAY)
    assert suffix.startswith("\n# Added by make env-update on 2026-11-01")
    assert suffix.endswith("\nB=2\n")


def test_update_env_keeps_every_existing_byte_and_the_file_mode(tmp_path: Path) -> None:
    template = tmp_path / ".env.example"
    template.write_text(UPDATE_TEMPLATE, encoding="utf-8")
    output = tmp_path / ".env"
    output.write_bytes(EXISTING.encode("utf-8"))
    output.chmod(0o600)

    keys = dev_env.update_env(template, output, counter_source(), TODAY)

    after = output.read_bytes()
    assert keys == ["NEW_SECRET", "NEW_PLAIN", "NEW_API_KEY", "NEW_QUOTED_KEY"]
    assert after.startswith(EXISTING.encode("utf-8"))
    values = parse(after.decode("utf-8"))
    assert values["CAIRN_DB_PORT"] == "55432"
    assert values["NEW_SECRET"] == "token-0000000000000000000001"
    assert values["NEW_API_KEY"] == ""
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_update_env_is_a_no_op_on_a_complete_file(tmp_path: Path) -> None:
    template = tmp_path / ".env.example"
    template.write_text(UPDATE_TEMPLATE, encoding="utf-8")
    output = tmp_path / ".env"
    output.write_bytes(EXISTING.encode("utf-8"))
    dev_env.update_env(template, output, counter_source(), TODAY)
    complete = output.read_bytes()
    mtime = output.stat().st_mtime_ns

    assert dev_env.update_env(template, output, counter_source(), TODAY) == []

    assert output.read_bytes() == complete
    assert output.stat().st_mtime_ns == mtime  # not even opened for writing


def test_main_add_missing_appends_names_keys_and_never_prints_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = tmp_path / ".env.example"
    template.write_text(UPDATE_TEMPLATE, encoding="utf-8")
    output = tmp_path / ".env"
    output.write_bytes(EXISTING.encode("utf-8"))
    args = ["--add-missing", "--template", str(template), "--out", str(output)]

    assert dev_env.main(args) == 0
    out = capsys.readouterr()
    values = parse(output.read_text(encoding="utf-8"))
    assert "NEW_SECRET" in out.out
    for key in ("NEW_SECRET", "POSTGRES_PASSWORD"):
        assert values[key] not in out.out + out.err
    before = output.read_bytes()

    assert dev_env.main(args) == 0
    assert "unchanged" in capsys.readouterr().out
    assert output.read_bytes() == before


def test_main_add_missing_creates_a_missing_file_like_make_env(tmp_path: Path) -> None:
    template = tmp_path / ".env.neon.example"
    template.write_text(UPDATE_TEMPLATE, encoding="utf-8")
    output = tmp_path / ".env.neon"
    assert dev_env.main(["--add-missing", "--template", str(template), "--out", str(output)]) == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert parse(output.read_text(encoding="utf-8"))["NEW_API_KEY"] == ""


def test_main_out_alias_refuses_to_overwrite(tmp_path: Path) -> None:
    template = tmp_path / ".env.neon.example"
    template.write_text(UPDATE_TEMPLATE, encoding="utf-8")
    output = tmp_path / ".env.neon"
    output.write_text("MINE=1\n", encoding="utf-8")
    assert dev_env.main(["--template", str(template), "--out", str(output)]) == 1
    assert output.read_text(encoding="utf-8") == "MINE=1\n"
