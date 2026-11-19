"""The rendered compose configuration and the db-reset guard (free-tier retarget, task 2.1).

Requirements 1.1 to 1.5: the `db` service runs a digest-pinned `pgvector/pgvector:<ver>-pg18`
image on the volume `pgdata-pgvector`, the old volume `pgdata` (Docker name
`cairn_pgdata`) is neither declared nor mounted, and `make db-reset-guard` refuses every
other volume name. No test here runs `db-reset` itself.

Task 6.1 adds the `freetier` profile checks (Requirements 6.3, 10.1 to 10.7): `ft-app` limits
and `PORT`, loopback-only ports, `POSTGRES_PASSWORD` only on `ft-db` and `ft-admin-init`, no
admin, owner or superuser password on `ft-app`, and `ft-db` on the same image as `db`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PG_IMAGE: Final = re.compile(
    r"^pgvector/pgvector:(?P<version>\d+\.\d+\.\d+)-pg18@sha256:[0-9a-f]{64}$"
)
NEW_VOLUME: Final = "pgdata-pgvector"
OLD_VOLUME: Final = "pgdata"

needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def compose_config(*profiles: str) -> dict[str, Any]:
    """`docker compose config` as JSON, rendered from the tracked template (no secrets)."""
    args = ["docker", "compose", "--env-file", str(REPO_ROOT / ".env.example")]
    for profile in profiles:
        args += ["--profile", profile]
    result = subprocess.run(
        [*args, "config", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    config: dict[str, Any] = json.loads(result.stdout)
    return config


@pytest.fixture(scope="module")
def core_config() -> dict[str, Any]:
    return compose_config("core")


@needs_docker
def test_db_image_is_pgvector_pg18_pinned_by_digest(core_config: dict[str, Any]) -> None:
    image = core_config["services"]["db"]["image"]
    match = PG_IMAGE.match(image)
    assert match is not None, image
    version = tuple(int(part) for part in match["version"].split("."))
    assert version >= (0, 8, 0), image


@needs_docker
def test_db_mounts_only_the_new_volume(core_config: dict[str, Any]) -> None:
    mounts = core_config["services"]["db"]["volumes"]
    assert [(m["type"], m["source"], m["target"]) for m in mounts] == [
        ("volume", NEW_VOLUME, "/var/lib/postgresql")
    ]
    assert core_config["volumes"][NEW_VOLUME]["name"] == f"cairn_{NEW_VOLUME}"


@needs_docker
def test_old_volume_is_neither_declared_nor_mounted() -> None:
    config = compose_config("core", "obs")
    assert OLD_VOLUME not in config.get("volumes", {})
    for name, service in config["services"].items():
        sources = {m.get("source") for m in service.get("volumes", [])}
        assert OLD_VOLUME not in sources, name
        assert f"cairn_{OLD_VOLUME}" not in sources, name


def run_guard(tmp_path: Path, *make_args: str) -> subprocess.CompletedProcess[str]:
    """`make db-reset-guard` with a `docker` on PATH that leaves a marker if it is called."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "docker"
    stub.write_text(f'#!/bin/sh\ntouch "{tmp_path / "docker-called"}"\nexit 1\n', encoding="utf-8")
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in {"DB_VOLUME", "MAKEFLAGS"}}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["make", "--no-print-directory", "db-reset-guard", *make_args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@needs_make
def test_db_reset_guard_refuses_the_old_volume(tmp_path: Path) -> None:
    result = run_guard(tmp_path, "DB_VOLUME=cairn_pgdata")
    assert result.returncode != 0
    assert "refusing DB_VOLUME=cairn_pgdata" in result.stderr
    assert not (tmp_path / "docker-called").exists()


@needs_make
@pytest.mark.parametrize("volume", ["cairn_grafana-data", "other_pgdata-pgvector", ""])
def test_db_reset_guard_refuses_any_other_name(tmp_path: Path, volume: str) -> None:
    result = run_guard(tmp_path, f"DB_VOLUME={volume}")
    assert result.returncode != 0
    assert not (tmp_path / "docker-called").exists()


@needs_make
def test_db_reset_guard_accepts_the_new_volume(tmp_path: Path) -> None:
    assert run_guard(tmp_path).returncode == 0
    assert run_guard(tmp_path, "DB_VOLUME=cairn_pgdata-pgvector").returncode == 0
    assert not (tmp_path / "docker-called").exists()


FREETIER_SERVICES: Final = {"ft-db", "ft-admin-init", "ft-db-init", "ft-app"}
# Keys that carry an admin, owner or superuser password (design §9, Requirement 6.3).
PRIVILEGED_PASSWORD_KEYS: Final = {
    "POSTGRES_PASSWORD",
    "CAIRN_OWNER_DB_PASSWORD",
    "CAIRN_DB_ADMIN_PASSWORD",
    "CAIRN_MANAGED_ADMIN_PASSWORD",
}
FT_APP_SECRET_KEYS: Final = {
    "CAIRN_APP_DB_PASSWORD",
    "CAIRN_DBOS_DB_PASSWORD",
    "CAIRN_INGEST_DB_PASSWORD",  # Phase 1, DV-10: the combined process writes content as cairn_ingest  # noqa: E501
    "CAIRN_PROXY_TOKEN",
}


@pytest.fixture(scope="module")
def freetier_config() -> dict[str, Any]:
    """The freetier profile on its own, as `make compose-check` renders it."""
    return compose_config("freetier")


@needs_docker
def test_freetier_profile_has_exactly_its_four_services(freetier_config: dict[str, Any]) -> None:
    assert set(freetier_config["services"]) == FREETIER_SERVICES


@needs_docker
def test_ft_app_limits_and_port(freetier_config: dict[str, Any]) -> None:
    app = freetier_config["services"]["ft-app"]
    assert int(app["mem_limit"]) == 512 * 1024 * 1024
    assert int(app["memswap_limit"]) == 512 * 1024 * 1024
    assert float(app["cpus"]) == pytest.approx(0.1)
    assert app["environment"]["PORT"] == "10000"
    assert app["read_only"] is True
    assert app["user"] == "10001:10001"
    assert app["image"] == "cairn-combined:local"
    assert app["build"]["args"]["PACKAGE"] == "cairn-combined"


@needs_docker
def test_freetier_ports_bind_loopback_only(freetier_config: dict[str, Any]) -> None:
    published = {
        name: [(p["host_ip"], int(p["published"]), p["target"]) for p in service.get("ports", [])]
        for name, service in freetier_config["services"].items()
    }
    assert published["ft-db"] == [("127.0.0.1", 55433, 5432)]
    assert published["ft-app"] == [("127.0.0.1", 8010, 10000)]
    assert published["ft-admin-init"] == []
    assert published["ft-db-init"] == []


@needs_docker
def test_postgres_password_only_on_ft_db_and_ft_admin_init(
    freetier_config: dict[str, Any],
) -> None:
    holders = {
        name
        for name, service in freetier_config["services"].items()
        if "POSTGRES_PASSWORD" in service.get("environment", {})
    }
    assert holders == {"ft-db", "ft-admin-init"}


@needs_docker
def test_ft_app_holds_no_admin_owner_or_superuser_password(
    freetier_config: dict[str, Any],
) -> None:
    environment = freetier_config["services"]["ft-app"]["environment"]
    assert not PRIVILEGED_PASSWORD_KEYS & set(environment)
    secret_like = {key for key in environment if "PASSWORD" in key or "TOKEN" in key}
    assert secret_like == FT_APP_SECRET_KEYS


@needs_docker
def test_ft_db_init_runs_managed_mode_as_cairn_admin(freetier_config: dict[str, Any]) -> None:
    init = freetier_config["services"]["ft-db-init"]
    assert init["command"] == ["cairn-db", "init"]
    assert "--allow-plaintext-password" not in init["command"]
    environment = init["environment"]
    assert environment["CAIRN_DB_ADMIN_USER"] == "cairn_admin"
    assert environment["CAIRN_DB_ADMIN_MODE"] == "managed"
    assert "POSTGRES_PASSWORD" not in environment


@needs_docker
def test_ft_admin_init_runs_the_emulation_in_ci_mode(freetier_config: dict[str, Any]) -> None:
    admin = freetier_config["services"]["ft-admin-init"]
    assert admin["command"] == ["cairn-db", "emulate-managed-admin"]
    assert admin["environment"]["CAIRN_ENV"] == "ci"
    assert set(admin["environment"]) >= {"POSTGRES_PASSWORD", "CAIRN_MANAGED_ADMIN_PASSWORD"}


@needs_docker
def test_ft_db_image_equals_db_image_and_data_is_tmpfs(freetier_config: dict[str, Any]) -> None:
    core_db = compose_config("core")["services"]["db"]
    ft_db = freetier_config["services"]["ft-db"]
    assert ft_db["image"] == core_db["image"]
    assert not ft_db.get("volumes")
    assert any(entry.startswith("/var/lib/postgresql:") for entry in ft_db["tmpfs"])


@needs_docker
def test_freetier_declares_no_volume_of_its_own(freetier_config: dict[str, Any]) -> None:
    for name, service in freetier_config["services"].items():
        assert not service.get("volumes"), name


def test_compose_check_validates_freetier_on_its_own() -> None:
    db_mk = (REPO_ROOT / "mk" / "db.mk").read_text(encoding="utf-8")
    assert re.search(r"for profile in core obs experiments freetier;", db_mk)
    assert re.search(r"core\|freetier\) set -- --profile", db_mk)


def test_db_reset_depends_on_the_guard() -> None:
    db_mk = (REPO_ROOT / "mk" / "db.mk").read_text(encoding="utf-8")
    assert re.search(r"^db-reset: db-reset-guard(\s|$)", db_mk, re.MULTILINE), (
        "db-reset must list db-reset-guard as its first prerequisite"
    )
