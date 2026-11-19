"""Unit tests for cairn_core.config (design §7, Requirements 7.11 and 7.12).

Every test runs in an empty temporary directory with all Cairn variables removed
from the environment, so neither the developer's shell nor the repo's .env can
change the result.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from cairn_core.config import (
    AdminSettings,
    ApiSettings,
    CommonSettings,
    DemoSettings,
    IngestSettings,
    LlmSettings,
    ModelCacheSettings,
    ModelSettings,
    SearchSettings,
    WorkersAiSettings,
    WorkerSettings,
    env_keys,
    libpq_tls,
    load_settings,
)
from scripts import dev_env

# Fake values, distinctive enough to grep for in captured output.
APP_PASSWORD = "app-pw-FAKE-0123456789"
DBOS_PASSWORD = "dbos-pw-FAKE-0123456789"
OWNER_PASSWORD = "owner-pw-FAKE-0123456789"
SUPERUSER_PASSWORD = "super-pw-FAKE-0123456789"
ALL_FAKE_SECRETS = (APP_PASSWORD, DBOS_PASSWORD, OWNER_PASSWORD, SUPERUSER_PASSWORD)

SETTINGS_CLASSES = (
    CommonSettings,
    ApiSettings,
    WorkerSettings,
    AdminSettings,
    WorkersAiSettings,
    ModelSettings,
    LlmSettings,
    DemoSettings,
    ModelCacheSettings,
    SearchSettings,
    IngestSettings,
)


def _all_aliases() -> set[str]:
    return {key for cls in SETTINGS_CLASSES for key in env_keys(cls)}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for alias in _all_aliases():
        monkeypatch.delenv(alias, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


def _set(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


# --- defaults ---------------------------------------------------------------------


def test_common_defaults_match_the_design() -> None:
    s = CommonSettings()
    assert s.env == "local"
    assert s.log_level == "INFO"
    assert (s.db_host, s.db_port, s.db_name) == ("127.0.0.1", 5432, "cairn")
    assert s.db_timeout_s == 2.0
    assert s.otel_traces_exporter == "none"
    assert s.otel_endpoint is None


def test_api_defaults_and_role(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, CAIRN_APP_DB_PASSWORD=APP_PASSWORD)
    s = load_settings(ApiSettings)
    assert s.app_db_user == "cairn_app"
    assert s.app_db_password.get_secret_value() == APP_PASSWORD


def test_worker_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, CAIRN_DBOS_DB_PASSWORD=DBOS_PASSWORD)
    s = load_settings(WorkerSettings)
    assert s.dbos_db_name == "cairn_dbos"
    assert s.heartbeat_cron == "*/30 * * * * *"
    assert s.dbos_db_password.get_secret_value() == DBOS_PASSWORD


def _admin_role_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(
        monkeypatch,
        CAIRN_OWNER_DB_PASSWORD=OWNER_PASSWORD,
        CAIRN_APP_DB_PASSWORD=APP_PASSWORD,
        CAIRN_DBOS_DB_PASSWORD=DBOS_PASSWORD,
    )


def test_admin_defaults_match_the_design(monkeypatch: pytest.MonkeyPatch) -> None:
    _admin_role_secrets(monkeypatch)
    s = load_settings(AdminSettings)
    assert (s.admin_user, s.admin_database, s.admin_mode) == ("postgres", "postgres", "auto")
    assert s.admin_password is None  # optional: status and migrate connect as the owner


def test_admin_password_falls_back_to_postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    _admin_role_secrets(monkeypatch)
    monkeypatch.setenv("POSTGRES_PASSWORD", SUPERUSER_PASSWORD)
    s = load_settings(AdminSettings)
    assert s.admin_password is not None
    assert s.admin_password.get_secret_value() == SUPERUSER_PASSWORD
    # An empty CAIRN_DB_ADMIN_PASSWORD counts as unset, so the fallback still applies.
    monkeypatch.setenv("CAIRN_DB_ADMIN_PASSWORD", "")
    s = load_settings(AdminSettings)
    assert s.admin_password is not None
    assert s.admin_password.get_secret_value() == SUPERUSER_PASSWORD


def test_admin_password_prefers_the_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _admin_role_secrets(monkeypatch)
    _set(monkeypatch, POSTGRES_PASSWORD=SUPERUSER_PASSWORD, CAIRN_DB_ADMIN_PASSWORD="admin-FAKE-1")
    s = load_settings(AdminSettings)
    assert s.admin_password is not None
    assert s.admin_password.get_secret_value() == "admin-FAKE-1"
    assert "admin-FAKE-1" not in repr(s)


def test_admin_keys_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _admin_role_secrets(monkeypatch)
    _set(
        monkeypatch,
        CAIRN_DB_ADMIN_USER="neondb_owner",
        CAIRN_DB_ADMIN_DATABASE="neondb",
        CAIRN_DB_ADMIN_MODE="managed",
    )
    s = load_settings(AdminSettings)
    assert (s.admin_user, s.admin_database, s.admin_mode) == ("neondb_owner", "neondb", "managed")


def test_invalid_admin_mode_exits_2_naming_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _admin_role_secrets(monkeypatch)
    monkeypatch.setenv("CAIRN_DB_ADMIN_MODE", "root-ish-VALUE")
    with pytest.raises(SystemExit) as exc:
        load_settings(AdminSettings)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "CAIRN_DB_ADMIN_MODE" in err
    assert "root-ish-VALUE" not in err


# --- TLS (free-tier FT-14, Requirements 4.3 to 4.5) -----------------------------------


def test_tls_defaults_to_prefer_without_a_root_certificate() -> None:
    s = CommonSettings()
    assert (s.db_sslmode, s.db_sslrootcert) == ("prefer", None)
    assert libpq_tls(s) == {"sslmode": "prefer"}


def test_libpq_tls_carries_the_root_certificate_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(
        monkeypatch,
        CAIRN_DB_SSLMODE="verify-full",
        CAIRN_DB_SSLROOTCERT="/etc/ssl/certs/ca-certificates.crt",
    )
    assert libpq_tls(CommonSettings()) == {
        "sslmode": "verify-full",
        "sslrootcert": "/etc/ssl/certs/ca-certificates.crt",
    }


@pytest.mark.parametrize("mode", ["disable", "prefer", "require", "verify-ca", "verify-full"])
def test_every_libpq_sslmode_is_accepted(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("CAIRN_DB_SSLMODE", mode)
    assert libpq_tls(CommonSettings())["sslmode"] == mode


@pytest.mark.parametrize("bad_mode", ["allow-SECRETISH", "VERIFY-FULL", "verify_full", "always-on"])
def test_invalid_sslmode_exits_2_naming_the_key_not_the_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bad_mode: str
) -> None:
    _set(monkeypatch, CAIRN_APP_DB_PASSWORD=APP_PASSWORD, CAIRN_DB_SSLMODE=bad_mode)
    with pytest.raises(SystemExit) as exc:
        load_settings(ApiSettings)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "CAIRN_DB_SSLMODE" in captured.err
    assert bad_mode not in captured.err
    assert captured.out == ""


# --- aliases ----------------------------------------------------------------------


def test_every_alias_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(
        monkeypatch,
        CAIRN_ENV="ci",
        CAIRN_LOG_LEVEL="DEBUG",
        CAIRN_DB_HOST="db",
        CAIRN_DB_PORT="6543",
        CAIRN_DB_NAME="cairn_test_ab12cd34",
        CAIRN_DB_TIMEOUT_S="0.5",
        OTEL_TRACES_EXPORTER="otlp",
        OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4318",
        CAIRN_APP_DB_USER="app_user",
        CAIRN_APP_DB_PASSWORD=APP_PASSWORD,
        CAIRN_DBOS_DB_NAME="dbos_x",
        CAIRN_DBOS_DB_PASSWORD=DBOS_PASSWORD,
        CAIRN_HEARTBEAT_CRON="*/5 * * * * *",
        CAIRN_OWNER_DB_PASSWORD=OWNER_PASSWORD,
        POSTGRES_PASSWORD=SUPERUSER_PASSWORD,
    )
    api = load_settings(ApiSettings)
    assert (api.env, api.log_level) == ("ci", "DEBUG")
    assert (api.db_host, api.db_port, api.db_name) == ("db", 6543, "cairn_test_ab12cd34")
    assert api.db_timeout_s == 0.5
    assert api.otel_traces_exporter == "otlp"
    assert str(api.otel_endpoint) == "http://otel-collector:4318/"
    assert api.app_db_user == "app_user"

    worker = load_settings(WorkerSettings)
    assert worker.dbos_db_name == "dbos_x"
    assert worker.heartbeat_cron == "*/5 * * * * *"

    admin = load_settings(AdminSettings)
    assert admin.owner_db_password.get_secret_value() == OWNER_PASSWORD
    assert admin.app_db_password.get_secret_value() == APP_PASSWORD
    assert admin.dbos_db_password.get_secret_value() == DBOS_PASSWORD


def test_field_names_are_not_environment_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, DB_PORT="1234", db_host="elsewhere")
    s = CommonSettings()
    assert (s.db_host, s.db_port) == ("127.0.0.1", 5432)


def test_settings_are_read_from_dotenv_and_the_environment_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        f"CAIRN_DB_PORT=7000\nCAIRN_APP_DB_PASSWORD={APP_PASSWORD}\n"
        'CAIRN_HEARTBEAT_CRON="*/30 * * * * *"\n'
        "POSTGRES_PASSWORD=ignored-by-api\n",
        encoding="utf-8",
    )
    s = load_settings(ApiSettings)  # extra keys in .env are ignored
    assert s.db_port == 7000
    monkeypatch.setenv("CAIRN_DB_PORT", "7001")
    assert load_settings(ApiSettings).db_port == 7001


def test_generated_env_from_the_template_loads_every_settings_class(
    tmp_path: Path,
) -> None:
    """.env.example and the aliases must agree: a generated .env loads cleanly."""
    template = (Path(__file__).resolve().parents[4] / ".env.example").read_text(encoding="utf-8")
    neon = (Path(__file__).resolve().parents[4] / ".env.neon.example").read_text(encoding="utf-8")
    keys = {
        line.key for text in (template, neon) for line in dev_env.parse_template(text) if line.key
    }
    # Every setting has a documented variable: in .env.example, or (admin and TLS keys that
    # only Neon needs, defaults suffice locally) in .env.neon.example.
    assert _all_aliases() <= keys
    (tmp_path / ".env").write_text(dev_env.render(template, dev_env.random_token), encoding="utf-8")

    api = load_settings(ApiSettings)
    worker = load_settings(WorkerSettings)
    admin = load_settings(AdminSettings)
    assert (api.db_host, api.db_port, api.db_name) == ("127.0.0.1", 5432, "cairn")
    assert api.app_db_user == "cairn_app"
    assert worker.dbos_db_name == "cairn_dbos"
    assert worker.heartbeat_cron == "*/30 * * * * *"
    assert admin.admin_password is not None
    assert api.otel_traces_exporter == "none"
    assert str(api.otel_endpoint) == "http://127.0.0.1:4318/"


def test_empty_variable_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, CAIRN_DB_PORT="", CAIRN_APP_DB_PASSWORD=APP_PASSWORD)
    assert load_settings(ApiSettings).db_port == 5432
    monkeypatch.setenv("CAIRN_APP_DB_PASSWORD", "")
    with pytest.raises(SystemExit) as exc:
        load_settings(ApiSettings)
    assert exc.value.code == 2


# --- secrets ----------------------------------------------------------------------


def test_secrets_are_masked_in_repr_and_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, CAIRN_APP_DB_PASSWORD=APP_PASSWORD)
    s = load_settings(ApiSettings)
    assert isinstance(s.app_db_password, SecretStr)
    assert APP_PASSWORD not in repr(s)
    assert APP_PASSWORD not in str(s)
    assert APP_PASSWORD not in s.model_dump_json()


def test_settings_are_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, CAIRN_APP_DB_PASSWORD=APP_PASSWORD)
    s = load_settings(ApiSettings)
    with pytest.raises(ValidationError):
        s.db_port = 1  # type: ignore[misc]


# --- failures: load_settings exits 2 naming keys, never values ----------------------


def test_missing_required_secret_exits_2_and_names_the_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        load_settings(ApiSettings)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "CAIRN_APP_DB_PASSWORD" in err
    assert "missing" in err


def test_each_missing_admin_secret_is_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(monkeypatch, CAIRN_APP_DB_PASSWORD=APP_PASSWORD)
    with pytest.raises(SystemExit) as exc:
        load_settings(AdminSettings)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "CAIRN_OWNER_DB_PASSWORD" in err
    assert "CAIRN_DBOS_DB_PASSWORD" in err
    assert "CAIRN_APP_DB_PASSWORD" not in err
    assert APP_PASSWORD not in err


@pytest.mark.parametrize("bad_port", ["not-a-port", "0", "65536", "-1", "54.32"])
def test_malformed_port_exits_2_without_echoing_the_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bad_port: str
) -> None:
    _set(monkeypatch, CAIRN_APP_DB_PASSWORD=APP_PASSWORD, CAIRN_DB_PORT=bad_port)
    with pytest.raises(SystemExit) as exc:
        load_settings(ApiSettings)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "CAIRN_DB_PORT" in captured.err
    assert APP_PASSWORD not in captured.err
    assert bad_port not in captured.err.replace("CAIRN_DB_PORT", "")
    assert captured.out == ""


def test_all_invalid_keys_are_reported_at_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(
        monkeypatch,
        CAIRN_APP_DB_PASSWORD=APP_PASSWORD,
        CAIRN_ENV="production-SECRETISH",
        CAIRN_DB_TIMEOUT_S="0",
        OTEL_TRACES_EXPORTER="zipkin",
        OTEL_EXPORTER_OTLP_ENDPOINT="not a url",
    )
    with pytest.raises(SystemExit) as exc:
        load_settings(ApiSettings)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    for key in (
        "CAIRN_ENV",
        "CAIRN_DB_TIMEOUT_S",
        "OTEL_TRACES_EXPORTER",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        assert key in err
    for value in ("production-SECRETISH", "zipkin", "not a url", APP_PASSWORD):
        assert value not in err


@pytest.mark.parametrize("cron", ["*/30 * * *", "* * * * * * *", "nonsense"])
def test_malformed_heartbeat_cron_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cron: str
) -> None:
    _set(monkeypatch, CAIRN_DBOS_DB_PASSWORD=DBOS_PASSWORD, CAIRN_HEARTBEAT_CRON=cron)
    with pytest.raises(SystemExit) as exc:
        load_settings(WorkerSettings)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "CAIRN_HEARTBEAT_CRON" in err
    assert cron not in err


def test_five_field_cron_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, CAIRN_DBOS_DB_PASSWORD=DBOS_PASSWORD, CAIRN_HEARTBEAT_CRON="*/5 * * * *")
    assert load_settings(WorkerSettings).heartbeat_cron == "*/5 * * * *"


def test_failure_output_never_contains_any_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(
        monkeypatch,
        CAIRN_OWNER_DB_PASSWORD=OWNER_PASSWORD,
        CAIRN_APP_DB_PASSWORD=APP_PASSWORD,
        POSTGRES_PASSWORD=SUPERUSER_PASSWORD,
        CAIRN_DB_PORT="oops",
    )
    with pytest.raises(SystemExit):
        load_settings(AdminSettings)
    captured = capsys.readouterr()
    for secret in ALL_FAKE_SECRETS:
        assert secret not in captured.err
        assert secret not in captured.out


# --- provider settings (free-tier FT-24, Requirements 16.6 to 16.9) -----------------------

CF_TOKEN = "cf-token-FAKE-0123456789"
ANTHROPIC_KEY = "anthropic-key-FAKE-0123456789"
OPENAI_KEY = "openai-key-FAKE-0123456789"
PROVIDER_SECRETS = (CF_TOKEN, ANTHROPIC_KEY, OPENAI_KEY)
ACCOUNT_ID = "0123456789abcdef0123456789abcdef"


def test_model_defaults_equal_the_ft24_models(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, CLOUDFLARE_API_TOKEN=CF_TOKEN)
    models = load_settings(ModelSettings)
    assert (models.embedder, models.embed_model, models.embed_dim) == (
        "workers_ai",
        "@cf/baai/bge-small-en-v1.5",
        384,
    )
    assert (models.reranker, models.rerank_model) == ("workers_ai", "@cf/baai/bge-reranker-base")
    assert models.embed_query_prefix.startswith("Represent this sentence")
    llm = load_settings(LlmSettings)
    assert (llm.answer_provider, llm.answer_model) == (
        "workers_ai",
        "@cf/google/gemma-4-26b-a4b-it",
    )
    assert (llm.judge_provider, llm.judge_model) == ("workers_ai", "@cf/zai-org/glm-4.7-flash")


def test_workers_ai_defaults_need_no_credential() -> None:
    s = load_settings(WorkersAiSettings)
    assert (s.account_id, s.api_token) == (None, None)
    assert str(s.base_url) == "https://api.cloudflare.com/client/v4"
    assert (s.daily_neurons, s.budget_store, s.max_attempts) == (9000, "memory", 3)
    assert (s.connect_timeout_s, s.read_timeout_s) == (5.0, 30.0)


def test_workers_ai_keys_are_read_and_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(
        monkeypatch,
        CLOUDFLARE_ACCOUNT_ID=ACCOUNT_ID,
        CLOUDFLARE_API_TOKEN=CF_TOKEN,
        CAIRN_WORKERS_AI_DAILY_NEURONS="5000",
        CAIRN_WORKERS_AI_BUDGET_STORE="postgres",
    )
    s = load_settings(WorkersAiSettings)
    assert s.account_id == ACCOUNT_ID
    assert s.api_token is not None
    assert s.api_token.get_secret_value() == CF_TOKEN
    assert (s.daily_neurons, s.budget_store) == (5000, "postgres")
    assert CF_TOKEN not in repr(s)


def test_malformed_account_id_exits_2_naming_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(monkeypatch, CLOUDFLARE_ACCOUNT_ID="not-hex-VALUE")
    with pytest.raises(SystemExit) as exc:
        load_settings(WorkersAiSettings)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "CLOUDFLARE_ACCOUNT_ID" in err
    assert "not-hex-VALUE" not in err


def test_the_env_example_selects_the_fakes_and_needs_no_token(tmp_path: Path) -> None:
    template = (Path(__file__).resolve().parents[4] / ".env.example").read_text(encoding="utf-8")
    (tmp_path / ".env").write_text(dev_env.render(template, dev_env.random_token), encoding="utf-8")
    models = load_settings(ModelSettings)
    llm = load_settings(LlmSettings)
    assert (models.embedder, models.reranker) == ("fake", "fake")
    assert (llm.answer_provider, llm.judge_provider) == ("fake", "fake")
    # The fakes keep the production model names, so only the provider switch differs.
    assert llm.answer_model == "@cf/google/gemma-4-26b-a4b-it"
    assert models.embed_model == "@cf/baai/bge-small-en-v1.5"
    assert load_settings(WorkersAiSettings).api_token is None


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"CAIRN_EMBEDDER": "workers_ai", "CAIRN_RERANKER": "fake"}, "CLOUDFLARE_API_TOKEN"),
        ({"CAIRN_EMBEDDER": "fake", "CAIRN_RERANKER": "workers_ai"}, "CLOUDFLARE_API_TOKEN"),
    ],
)
def test_model_settings_name_the_missing_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    missing: str,
) -> None:
    _set(monkeypatch, **env)
    with pytest.raises(SystemExit) as exc:
        load_settings(ModelSettings)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert missing in err
    assert "credential_missing" in err


def test_model_settings_with_fake_or_no_reranker_need_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, CAIRN_EMBEDDER="fake", CAIRN_RERANKER="none")
    assert load_settings(ModelSettings).reranker == "none"


@pytest.mark.parametrize(
    ("answer", "judge", "missing"),
    [
        ("workers_ai", "fake", ["CLOUDFLARE_API_TOKEN"]),
        ("fake", "anthropic", ["ANTHROPIC_API_KEY"]),
        ("openai", "fake", ["OPENAI_API_KEY"]),
        ("anthropic", "openai", ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]),
    ],
)
def test_llm_settings_name_each_missing_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    judge: str,
    missing: list[str],
) -> None:
    _set(
        monkeypatch,
        CAIRN_ANSWER_PROVIDER=answer,
        CAIRN_JUDGE_PROVIDER=judge,
        CAIRN_ANSWER_MODEL="claude-sonnet-x" if answer == "anthropic" else "gpt-x",
        CAIRN_JUDGE_MODEL="gpt-x" if judge == "openai" else "claude-x",
    )
    with pytest.raises(SystemExit) as exc:
        load_settings(LlmSettings)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    for key in missing:
        assert key in err
    held = {"CLOUDFLARE_API_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"} - set(missing)
    for key in held:
        assert key not in err


def test_llm_settings_with_every_credential_load(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(
        monkeypatch,
        CLOUDFLARE_API_TOKEN=CF_TOKEN,
        CAIRN_JUDGE_PROVIDER="anthropic",
        CAIRN_JUDGE_MODEL="claude-judge-x",
        ANTHROPIC_API_KEY=ANTHROPIC_KEY,
    )
    llm = load_settings(LlmSettings)
    assert llm.judge_provider == "anthropic"
    assert all(secret not in repr(llm) for secret in PROVIDER_SECRETS)


def test_same_family_answer_and_judge_names_both_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(
        monkeypatch,
        CLOUDFLARE_API_TOKEN=CF_TOKEN,
        CAIRN_JUDGE_MODEL="@cf/google/gemma-3-12b-it",
    )
    with pytest.raises(SystemExit) as exc:
        load_settings(LlmSettings)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "CAIRN_ANSWER_MODEL" in captured.err
    assert "CAIRN_JUDGE_MODEL" in captured.err
    assert "same_model_family" in captured.err
    assert "gemma-3-12b-it" not in captured.err
    assert CF_TOKEN not in captured.err
    assert captured.out == ""


def test_same_family_is_allowed_when_one_side_is_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(
        monkeypatch,
        CLOUDFLARE_API_TOKEN=CF_TOKEN,
        CAIRN_JUDGE_PROVIDER="fake",
        CAIRN_JUDGE_MODEL="@cf/google/gemma-3-12b-it",
    )
    assert load_settings(LlmSettings).judge_provider == "fake"


def test_same_family_across_providers_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(
        monkeypatch,
        CAIRN_ANSWER_PROVIDER="openai",
        CAIRN_ANSWER_MODEL="gpt-x",
        CAIRN_JUDGE_PROVIDER="workers_ai",
        CAIRN_JUDGE_MODEL="@cf/openai/gpt-oss-20b",
        OPENAI_API_KEY=OPENAI_KEY,
        CLOUDFLARE_API_TOKEN=CF_TOKEN,
    )
    with pytest.raises(SystemExit):
        load_settings(LlmSettings)
    err = capsys.readouterr().err
    assert "CAIRN_ANSWER_MODEL" in err
    assert "CAIRN_JUDGE_MODEL" in err
    assert OPENAI_KEY not in err


def test_unknown_model_family_names_the_model_key_not_the_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(monkeypatch, CLOUDFLARE_API_TOKEN=CF_TOKEN, CAIRN_JUDGE_MODEL="@cf/newvendor/model-x")
    with pytest.raises(SystemExit):
        load_settings(LlmSettings)
    err = capsys.readouterr().err
    assert "CAIRN_JUDGE_MODEL" in err
    assert "unknown_model_family" in err
    assert "newvendor" not in err


def test_a_missing_credential_and_a_family_clash_are_reported_together(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(monkeypatch, CAIRN_JUDGE_MODEL="@cf/google/gemma-3-12b-it")
    with pytest.raises(SystemExit):
        load_settings(LlmSettings)
    err = capsys.readouterr().err
    assert "CLOUDFLARE_API_TOKEN" in err
    assert "same_model_family" in err
