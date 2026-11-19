#!/usr/bin/env python3
"""Run the Phase 1 persona script against a running API (local compose or the demo deployment).

    scripts/persona_script.py --base-url http://127.0.0.1:8000 [--proxy-token-env CAIRN_PROXY_TOKEN]
                              [--corpus var/corpus/kestrel_pay]

Needs the corpus manifest (``make scenario-render``) to know which sources exist. Uses persona login,
so the target must have it enabled (local, or demo with CAIRN_DEMO_PERSONAS=on). Prints findings
only, never response text. Exit status 0 passes, 1 findings, 2 setup problem.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from cairn_eval.persona_script import PERSONAS, run_script
from cairn_eval.seed import load_kestrel


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--corpus", default="var/corpus/kestrel_pay")
    ap.add_argument(
        "--proxy-token-env", default="", help="name of the env var holding the proxy token"
    )
    args = ap.parse_args()
    manifest_path = Path(args.corpus) / "manifest.json"
    if not manifest_path.exists():
        print("no corpus manifest: run make scenario-render", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())
    headers = {}
    if args.proxy_token_env:
        token = os.environ.get(args.proxy_token_env)
        if not token:
            print(f"{args.proxy_token_env} is not set", file=sys.stderr)
            return 2
        headers["x-cairn-proxy-token"] = token
    clients = {
        p: httpx.Client(base_url=args.base_url, headers=headers, timeout=120) for p in PERSONAS
    }
    try:
        for persona, client in clients.items():
            r = client.post("/auth/dev-login", json={"persona": persona})
            if r.status_code != 204:
                print(f"login as {persona} failed (HTTP {r.status_code})", file=sys.stderr)
                return 2

        def ask(persona: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            r = clients[persona].post(path, json=payload)
            r.raise_for_status()
            return r.json()

        findings = run_script(load_kestrel(), manifest, ask)
    finally:
        for c in clients.values():
            c.close()
    for f in findings:
        print(f"FINDING persona={f.persona} question={f.question}: {f.problem}")
    print("persona script: " + ("FAILED" if findings else "ok"))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
