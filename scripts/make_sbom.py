"""Write the software bill of materials (plan 6.1): CycloneDX 1.5 for Python and for the web app.

    python scripts/make_sbom.py

Python comes from ``uv export --format cyclonedx1.5`` over the frozen lock (workspace members left
out, since they are this project). The web SBOM is built from ``apps/web/pnpm-lock.yaml`` (pnpm 10.8
has no ``sbom`` command): every locked package with its version, purl and integrity hash. Both read
lock files only, so they need no install and no network.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "evidence" / "phase-6"
_KEY = re.compile(r"^(?P<name>@?[^@]+)@(?P<version>[^()@]+)")


def web_components(lock: dict[str, object]) -> list[dict[str, object]]:
    packages = lock.get("packages", {})
    assert isinstance(packages, dict)  # noqa: S101
    out = []
    for key in sorted(packages):
        m = _KEY.match(str(key))
        if m is None:
            continue
        name, version = m["name"], m["version"]
        res = packages[key].get("resolution", {}) if isinstance(packages[key], dict) else {}
        comp: dict[str, object] = {
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:npm/{name.replace('@', '%40', 1) if name.startswith('@') else name}@{version}",  # noqa: E501
        }
        integrity = res.get("integrity") if isinstance(res, dict) else None
        if isinstance(integrity, str) and integrity.startswith("sha512-"):
            comp["hashes"] = [{"alg": "SHA-512", "content": base64.b64decode(integrity[7:]).hex()}]
        out.append(comp)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    py = OUT / "sbom-python.cdx.json"
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--all-packages",
            "--no-emit-workspace",
            "--format",
            "cyclonedx1.5",
            "--output-file",
            str(py),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    lock = yaml.safe_load((ROOT / "apps" / "web" / "pnpm-lock.yaml").read_text(encoding="utf-8"))
    comps = web_components(lock)
    web = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "cairn-web"}},
        "components": comps,
    }
    (OUT / "sbom-web.cdx.json").write_text(
        json.dumps(web, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    n_py = len(json.loads(py.read_text()).get("components", []))
    print(f"sbom: {n_py} Python components, {len(comps)} web components -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
