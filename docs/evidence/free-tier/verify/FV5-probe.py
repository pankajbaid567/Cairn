"""FV5 probe: DBOS 3.3.0 inside one long-lived process (free-tier task 5.1).

Run from the repo root with the core stack up:  ``uv run python docs/evidence/free-tier/verify/FV5-probe.py``

Everything happens in throwaway objects: a database ``cairn_test_dbos_fv5_<hex>`` on the core
``db`` (created and dropped through ``bootstrap_database`` / ``drop_database``) and, for the TLS
part, a scratch container ``cairn-fv5-tls-<hex>`` that is removed at the end. The live
``cairn_dbos`` database is never touched. Output is plain text; nothing secret is printed.
"""  # noqa: E501

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import closing
from importlib import metadata
from pathlib import Path
from typing import Any

import psycopg
import uvicorn
from dbos import DBOS
from fastapi import FastAPI
from pydantic import SecretStr

from cairn_core.bootstrap import bootstrap_database, dbos_spec
from cairn_core.config import WorkerSettings
from cairn_core.testing import TEST_DB_PREFIX, connect_admin, drop_database, load_config
from cairn_worker import app as worker

QUICK_CRON = "*/2 * * * * *"
COMBINED_CRON = "0 */5 * * * *"
IDLE_WINDOW_S = 60


def say(text: str = "") -> None:
    print(text, flush=True)


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sqlstate(exc: BaseException) -> str | None:
    found: BaseException | None = exc
    while found is not None:
        state = getattr(found, "sqlstate", None)
        if isinstance(state, str):
            return state
        found = found.__cause__ or found.__context__
    return None


def describe(exc: BaseException) -> str:
    """Class and SQLSTATE only, as the runner will log it."""
    return f"{type(exc).__name__} sqlstate={sqlstate(exc)}"


def thread_names() -> list[str]:
    return sorted(t.name for t in threading.enumerate() if t is not threading.main_thread())


def attempt(
    settings: WorkerSettings, label: str, *, wait_success_s: float = 0.0, destroy: bool = True
) -> bool:
    """One construct, launch, schedule and (optionally) destroy. Returns whether it launched."""
    started = time.monotonic()
    stage = "construct"
    launched = False
    try:
        DBOS(config=worker.dbos_config(settings))
        stage = "launch"
        DBOS.launch()
        stage = "apply_schedules"
        DBOS.apply_schedules([worker.schedule_spec(settings)])
        launched = True
        if wait_success_s:
            deadline = time.monotonic() + wait_success_s
            runs: list[Any] = []
            while not runs and time.monotonic() < deadline:
                runs = list(
                    DBOS.list_workflows(schedule_name=worker.SCHEDULE_NAME, status="SUCCESS")
                )
                time.sleep(0.5)
            say(f"  {label}: heartbeat SUCCESS runs seen: {len(runs)}")
        say(f"  {label}: launched in {time.monotonic() - started:.1f} s")
    except Exception as exc:
        say(
            f"  {label}: failed at {stage} after {time.monotonic() - started:.1f} s: {describe(exc)}"  # noqa: E501
        )
    finally:
        if destroy:
            t0 = time.monotonic()
            DBOS.destroy()
            say(f"  {label}: destroy took {time.monotonic() - t0:.2f} s")
    return launched


def phase_relaunch(base: WorkerSettings, dbname: str) -> None:
    say("== 1. Relaunch after a failed launch, same process")
    say(f"threads before: {len(thread_names())}")
    closed = base.model_copy(update={"db_port": free_port()})
    badpw = base.model_copy(update={"dbos_db_password": SecretStr("wrong-password-fv5")})
    nodb = base.model_copy(
        update={"dbos_db_name": f"{TEST_DB_PREFIX}dbos_missing_{os.urandom(3).hex()}"}
    )
    good = base.model_copy(update={"dbos_db_name": dbname, "heartbeat_cron": QUICK_CRON})
    for label, bad in (
        ("closed port", closed),
        ("wrong password", badpw),
        ("missing database", nodb),
    ):
        attempt(bad, label)
        say(f"  threads after destroy: {len(thread_names())} {thread_names()}")
    say("-- then a good attempt in the same process (3 failures happened before it)")
    ok = attempt(good, "good after 3 failures", wait_success_s=10.0)
    say(f"  RESULT relaunch works: {ok}")
    say(f"  threads after final destroy: {len(thread_names())} {thread_names()}")
    say("-- failed launch WITHOUT destroy, then DBOS(config=good) and launch again")
    try:
        DBOS(config=worker.dbos_config(closed))
        DBOS.launch()
    except Exception as exc:
        say(f"  first launch failed: {describe(exc)}")
    try:
        DBOS(config=worker.dbos_config(good))
        DBOS.launch()
        DBOS.apply_schedules([worker.schedule_spec(good)])
        say("  second launch without destroy: no exception")
    except Exception as exc:
        say(f"  second launch without destroy raised: {describe(exc)}")
    finally:
        DBOS.destroy()
    say("-- and a good launch after that cleanup")
    say(f"  RESULT: {attempt(good, 'good after no-destroy case')}")


def phase_backfill(base: WorkerSettings, dbname: str) -> None:
    say("== 2. automatic_backfill default")
    good = base.model_copy(update={"dbos_db_name": dbname, "heartbeat_cron": COMBINED_CRON})
    DBOS(config=worker.dbos_config(good))
    try:
        DBOS.launch()
        spec = worker.schedule_spec(good)
        spec.pop("automatic_backfill", None)
        DBOS.apply_schedules([spec])
        say(
            f"  without the key: {[(s['schedule_name'], s['automatic_backfill']) for s in DBOS.list_schedules()]}"  # noqa: E501
        )
        DBOS.apply_schedules([{**spec, "automatic_backfill": False}])
        say(
            f"  with False:      {[(s['schedule_name'], s['automatic_backfill']) for s in DBOS.list_schedules()]}"  # noqa: E501
        )
        DBOS.apply_schedules([{**spec, "automatic_backfill": True}])
        say(
            f"  with True:       {[(s['schedule_name'], s['automatic_backfill']) for s in DBOS.list_schedules()]}"  # noqa: E501
        )
        DBOS.apply_schedules([{**spec, "automatic_backfill": False}])
    finally:
        DBOS.destroy()


def stats(admin: psycopg.Connection[Any], dbname: str) -> tuple[int, int, int]:
    row = admin.execute(
        "SELECT xact_commit + xact_rollback, tup_returned, tup_fetched "
        "FROM pg_stat_database WHERE datname = %s",
        (dbname,),
    ).fetchone()
    assert row is not None  # noqa: S101
    return int(row[0]), int(row[1]), int(row[2])


def phase_idle(base: WorkerSettings, dbname: str, admin: psycopg.Connection[Any]) -> None:
    say(f"== 3. Idle query rate (launched, cron {COMBINED_CRON}, {IDLE_WINDOW_S} s window)")
    good = base.model_copy(update={"dbos_db_name": dbname, "heartbeat_cron": COMBINED_CRON})
    DBOS(config=worker.dbos_config(good))
    try:
        DBOS.launch()
        DBOS.apply_schedules([worker.schedule_spec(good)])
        time.sleep(15)  # let launch-time work settle
        admin.execute("SELECT pg_stat_clear_snapshot()")
        t0, s0 = time.monotonic(), stats(admin, dbname)
        samples = []
        for _ in range(IDLE_WINDOW_S // 10):
            time.sleep(10)
            admin.execute("SELECT pg_stat_clear_snapshot()")
            samples.append(stats(admin, dbname))
        elapsed = time.monotonic() - t0
        s1 = samples[-1]
        say(
            f"  transactions in {elapsed:.0f} s: {s1[0] - s0[0]} ({(s1[0] - s0[0]) / elapsed:.2f}/s)"  # noqa: E501
        )
        say(f"  tuples returned: {s1[1] - s0[1]}, fetched: {s1[2] - s0[2]}")
        say(f"  per 10 s: {[b[0] - a[0] for a, b in zip([s0, *samples], samples, strict=False)]}")
        rows = admin.execute(
            "SELECT state, count(*) FROM pg_stat_activity WHERE datname = %s GROUP BY 1 ORDER BY 1",
            (dbname,),
        ).fetchall()
        say(f"  connections to the DBOS database by state: {rows}")
    finally:
        DBOS.destroy()


def phase_thread_under_uvicorn(base: WorkerSettings, dbname: str) -> None:
    say("== 4. Launch from a non-main thread while uvicorn serves in the main thread")
    good = base.model_copy(update={"dbos_db_name": dbname, "heartbeat_cron": QUICK_CRON})
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
    )
    result: dict[str, Any] = {}

    def background() -> None:
        try:
            while not server.started:
                time.sleep(0.05)
            t0 = time.monotonic()
            launched = attempt(good, "thread launch", wait_success_s=10.0, destroy=False)
            result["launched"] = launched
            result["launch_s"] = time.monotonic() - t0
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
                result["healthz_while_up"] = resp.status
        except Exception as exc:
            result["error"] = describe(exc)
        finally:
            t0 = time.monotonic()
            DBOS.destroy()
            result["destroy_s"] = time.monotonic() - t0
            server.should_exit = True

    thread = threading.Thread(target=background, name="fv5-jobs", daemon=True)
    thread.start()
    server.run()
    thread.join(30)
    say(f"  result: {result}")


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)  # noqa: S603, S607


def phase_tls(base: WorkerSettings) -> None:
    say("== 5. TLS parameters in the DBOS URL")
    say("-- against the core db (ssl is off there)")
    for mode in ("disable", "prefer", "require"):
        probe = base.model_copy(update={"db_sslmode": mode, "dbos_db_name": "cairn_dbos"})
        try:
            with psycopg.connect(
                host=probe.db_host,
                port=probe.db_port,
                dbname="postgres",
                user="cairn_dbos",
                password=probe.dbos_db_password.get_secret_value(),
                connect_timeout=3,
                **worker.libpq_tls(probe),
            ) as conn:
                conn.execute("SELECT 1")
            say(f"  sslmode={mode}: connected")
        except Exception as exc:
            say(f"  sslmode={mode}: {type(exc).__name__}")
    image = docker("inspect", "cairn-db-1", "--format", "{{.Image}}").stdout.strip()
    name = f"cairn-fv5-tls-{os.urandom(3).hex()}"
    port = free_port()
    password = secrets.token_hex(12)
    boot = (
        "openssl req -x509 -newkey rsa:2048 -nodes -keyout /tmp/server.key -out /tmp/server.crt "
        "-days 1 -subj /CN=localhost "
        '-addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1 '
        "&& chown postgres:postgres /tmp/server.key /tmp/server.crt && chmod 600 /tmp/server.key "
        "&& exec docker-entrypoint.sh postgres -c ssl=on "
        "-c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key"
    )
    docker(
        "run", "-d", "--name", name, "--memory", "256m", "-p", f"127.0.0.1:{port}:5432",
        "-e", f"POSTGRES_PASSWORD={password}", "--entrypoint", "sh", image, "-c", boot,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 90
        admin: psycopg.Connection[Any] | None = None
        while admin is None:
            try:
                admin = psycopg.connect(
                    host="127.0.0.1", port=port, dbname="postgres", user="postgres",
                    password=password, sslmode="require", connect_timeout=2, autocommit=True,
                )  # fmt: skip
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        role_pw = secrets.token_hex(12)
        admin.execute(f"CREATE ROLE cairn_dbos LOGIN PASSWORD '{role_pw}'")
        admin.execute("CREATE DATABASE fv5_tls OWNER cairn_dbos")
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "server.crt"
            docker("cp", f"{name}:/tmp/server.crt", str(cert))
            tls_base = base.model_copy(
                update={
                    "db_host": "127.0.0.1",
                    "db_port": port,
                    "dbos_db_name": "fv5_tls",
                    "dbos_db_password": SecretStr(role_pw),
                    "heartbeat_cron": COMBINED_CRON,
                }
            )
            cases = {
                "verify-full + sslrootcert=server cert": {
                    "db_sslmode": "verify-full", "db_sslrootcert": str(cert)},
                "verify-full + missing sslrootcert file": {
                    "db_sslmode": "verify-full", "db_sslrootcert": str(Path(tmp) / "none.crt")},
                "verify-full + no sslrootcert (system roots)": {"db_sslmode": "verify-full"},
                "disable": {"db_sslmode": "disable"},
            }  # fmt: skip
            for label, update in cases.items():
                case = tls_base.model_copy(update=update)
                launched = attempt(case, label)
                if launched and update.get("db_sslrootcert") == str(cert):
                    pass
            say("-- connections made by DBOS with verify-full: encrypted?")
            case = tls_base.model_copy(
                update={"db_sslmode": "verify-full", "db_sslrootcert": str(cert)}
            )
            DBOS(config=worker.dbos_config(case))
            try:
                DBOS.launch()
                rows = admin.execute(
                    "SELECT s.ssl, count(*) FROM pg_stat_ssl s "
                    "JOIN pg_stat_activity a USING (pid) WHERE a.usename = 'cairn_dbos' "
                    "GROUP BY 1"
                ).fetchall()
                say(f"  pg_stat_ssl for cairn_dbos sessions (ssl, count): {rows}")
            finally:
                DBOS.destroy()
        admin.close()
    finally:
        docker("rm", "-f", "-v", name, check=False)


def main() -> int:
    config = load_config()
    admin = connect_admin(config)
    admin.autocommit = True
    base = WorkerSettings()  # reads .env: db host and port of the core db, cairn_dbos password
    worker.prepare_dbos_logging()
    logging.getLogger("dbos").setLevel(logging.ERROR)
    dbname = f"{TEST_DB_PREFIX}dbos_fv5_{os.urandom(4).hex()}"
    say(f"throwaway database: {dbname}; core db at {base.db_host}:{base.db_port}")
    say(f"python {sys.version.split()[0]}, dbos {metadata.version('dbos')}")
    bootstrap_database(admin, dbname, spec=dbos_spec(dbname))
    try:
        phase_relaunch(base, dbname)
        phase_backfill(base, dbname)
        phase_thread_under_uvicorn(base, dbname)
        phase_idle(base, dbname, admin)
        phase_tls(base)
    finally:
        drop_database(admin, dbname)
        admin.close()
        say("throwaway database dropped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
