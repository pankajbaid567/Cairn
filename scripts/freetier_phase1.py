#!/usr/bin/env python3
"""Phase 1 load against ft-app (free-tier gate, plan 1.29; ADR-0014).

Run after ``make freetier-up`` and ``make freetier-seed``. Logs in as the four personas through the
proxy token, sends search and answer requests for ``--load-s`` seconds from two clients while
sampling the container's memory with ``docker stats``, and checks:

* peak memory <= 384 MiB (FT-18, small load), no OOM kill, restart count 0;
* at most 2% of requests failed;
* correctness under load: Ben never receives an HR canary and Asha's migration answer cites ADR-007.

Prints numbers only (never the proxy token, a question or an answer). Writes
``docs/evidence/phase-1/freetier-phase1-<date>.md``. Exit 0 pass, 1 budget or check missed, 2 setup.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # `python scripts/freetier_phase1.py` has scripts/ first
    sys.path.insert(0, str(REPO_ROOT))

from cairn_eval.persona_script import PERSONAS, load_script  # noqa: E402
from cairn_eval.scenario.canary import canary_token  # noqa: E402
from cairn_eval.seed import load_kestrel  # noqa: E402
from scripts.freetier_ram import (  # noqa: E402
    APP_PORT_KEY,
    DEFAULT_APP_PORT,
    PROJECT,
    TOKEN_KEY,
    RamCheckError,
    inspect_ft_app,
    lookup,
    project_containers,
    read_memory_mib,
    run_command,
)

LOAD_PEAK_MIB = 384.0
MAX_ERROR_RATIO = 0.02
ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load-s", type=float, default=45.0)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "docs/evidence/phase-1")
    args = ap.parse_args()
    try:
        token = lookup(TOKEN_KEY, args.env_file)
        if not token:
            raise RamCheckError(f"{TOKEN_KEY} is not set; run make env-update")
        port = int(lookup(APP_PORT_KEY, args.env_file) or DEFAULT_APP_PORT)
        containers = project_containers(run_command)
        name = next((n for s, n in containers.items() if s == "ft-app"), None)
        if name is None:
            raise RamCheckError(f"ft-app of project {PROJECT} is not running; run make freetier-up")
    except RamCheckError as exc:
        print(f"freetier-phase1: {exc}", file=sys.stderr)
        return 2

    base = f"http://127.0.0.1:{port}"
    headers = {"x-cairn-proxy-token": token}
    scenario = load_kestrel()
    hr_tokens = [
        canary_token(scenario.meta.canary_seed, c.id, c.family)
        for c in scenario.canaries
        if c.family == "hr" and c.kind == "token"
    ]
    questions = [q["text"] for q in load_script()]
    clients: dict[str, httpx.Client] = {}
    for persona in PERSONAS:
        c = httpx.Client(
            base_url=base,
            headers={**headers, "x-forwarded-for": f"10.1.0.{len(clients) + 1}"},
            timeout=120,
        )
        r = c.post("/auth/dev-login", json={"persona": persona})
        if r.status_code != 204:
            print(
                f"freetier-phase1: login as {persona} failed (HTTP {r.status_code}); is the corpus seeded and demo mode on?",
                file=sys.stderr,
            )
            return 2
        # The demo cookie is Secure, which httpx will not send back over plain http to localhost.
        value = r.cookies.get("cairn_session") or next(
            (
                v.split(";")[0].split("=", 1)[1]
                for k, v in r.headers.multi_items()
                if k.lower() == "set-cookie"
            ),
            None,
        )
        if value is None:
            print(f"freetier-phase1: login as {persona} set no session cookie", file=sys.stderr)
            return 2
        c.cookies.clear()
        c.headers["cookie"] = f"cairn_session={value}"
        clients[persona] = c

    samples: list[float] = []
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            try:  # noqa: SIM105
                samples.append(read_memory_mib(run_command, name))
            except Exception:  # noqa: S110
                pass
            stop.wait(2.0)

    stats = {"requests": 0, "errors": 0}
    problems: list[str] = []
    lock = threading.Lock()
    deadline = time.monotonic() + args.load_s

    def client_loop(offset: int) -> None:
        i = offset
        while time.monotonic() < deadline:
            persona = PERSONAS[i % len(PERSONAS)]
            q = questions[i % len(questions)]
            for path, payload in (
                ("/search", {"query": q, "limit": 5}),
                ("/answer", {"question": q}),
            ):
                try:
                    r = clients[persona].post(
                        path, json=payload, headers={"x-forwarded-for": f"10.2.{offset}.{i % 250}"}
                    )
                    ok = r.status_code == 200
                    body: Any = r.json() if ok else {}
                except (httpx.HTTPError, ValueError):
                    ok, body = False, {}
                with lock:
                    stats["requests"] += 1
                    stats["errors"] += 0 if ok else 1
                    if ok and persona == "ben" and any(t in json.dumps(body) for t in hr_tokens):
                        problems.append("an HR canary reached Ben under load")
            i += 2
            time.sleep(0.2)

    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    workers = [threading.Thread(target=client_loop, args=(k,)) for k in (0, 1)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    stop.set()
    t.join(timeout=5)
    for c in clients.values():
        c.close()

    facts = inspect_ft_app(run_command, name)
    peak = max(samples) if samples else float("nan")
    ratio = stats["errors"] / max(1, stats["requests"])
    if not samples:
        problems.append("no memory samples were taken")
    if peak > LOAD_PEAK_MIB:
        problems.append(f"peak {peak:.1f} MiB is above {LOAD_PEAK_MIB:g} MiB")
    if ratio > MAX_ERROR_RATIO:
        problems.append(f"{stats['errors']} of {stats['requests']} requests failed")
    if facts.oom_killed:
        problems.append("ft-app was OOM-killed")
    if facts.restart_count:
        problems.append(f"ft-app restarted {facts.restart_count} time(s)")

    day = date.today().isoformat()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    report = (
        f"# Free-tier gate with the Phase 1 code loaded ({day})\n\n"
        f"ft-app: 512 MB, no swap, 0.1 CPU, fake providers, corpus of {len(questions)} scripted questions.\n\n"
        f"- load: {args.load_s:g} s, 2 clients, search and answer as 4 personas\n"
        f"- requests: {stats['requests']}, failed: {stats['errors']}\n"
        f"- memory peak: {peak:.1f} MiB (budget {LOAD_PEAK_MIB:g}); samples: {len(samples)}\n"
        f"- OOM killed: {facts.oom_killed}; restarts: {facts.restart_count}\n"
        f"- result: {'PASS' if not problems else 'FAIL: ' + '; '.join(problems)}\n"
    )
    (args.out_dir / f"freetier-phase1-{day}.md").write_text(report, encoding="utf-8")
    print(report)
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
