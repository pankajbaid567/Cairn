"""Build the real-corpus benchmark: about 150 Python Enhancement Proposals (PEPs).

PEPs are public documents (the python/peps repository, public domain or CC0). They are a good
stand-in for "decisions an engineering organisation made": each has a rationale, a status
(Final, Rejected, Withdrawn, Superseded), authors, dates and links to the PEPs it replaces.

What is real: the text, titles, authors, dates and status of every PEP.
What is synthetic: the people who ask (alice, bob, carol) and the access rules. About one PEP in
ten is marked steering-only and one in six core-only, chosen by a hash of the PEP number, so the
permission questions have something to test. This is stated in the benchmark's report.

    uv run python scripts/realbench_corpus.py --out var/realbench
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

TREE = "https://api.github.com/repos/python/peps/git/trees/main?recursive=1"
RAW = "https://raw.githubusercontent.com/python/peps/main/peps/"
WANT = 150
MIN_CHARS, MAX_CHARS = 5_000, 60_000
FINAL_STATUSES = {"Final", "Rejected", "Withdrawn", "Superseded", "Accepted", "Active"}
FIELD = re.compile(r"^([A-Za-z-]+):[ \t]*(.*)$")


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cairn-realbench"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        return bytes(resp.read())


def parse(rst: str) -> tuple[dict[str, str], str] | None:
    head, sep, body = rst.partition("\n\n")
    if not sep:
        return None
    fields: dict[str, str] = {}
    last = ""
    for line in head.splitlines():
        m = FIELD.match(line)
        if m:
            last = m.group(1)
            fields[last] = m.group(2).strip()
        elif last and line.startswith((" ", "\t")):
            fields[last] += " " + line.strip()
    return (fields, body) if "Title" in fields and "Status" in fields else None


def acl_for(number: int) -> str:
    h = int(hashlib.sha256(f"pep-{number}".encode()).hexdigest(), 16) % 60
    if h < 6:
        return "steering"
    if h < 16:
        return "core"
    return "public"


def clean(body: str) -> str:
    body = re.sub(r"^\.\. .*?(?=\n\S|\Z)", "", body, flags=re.S | re.M)  # rst directives, comments
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="var/realbench", type=Path)
    out: Path = ap.parse_args().out
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    tree = json.loads(get(TREE))["tree"]
    names = sorted(
        t["path"].split("/")[-1]
        for t in tree
        if re.fullmatch(r"peps/pep-\d{4}\.rst", t["path"])
    )
    candidates: list[dict[str, Any]] = []
    for name in names:
        number = int(name[4:8])
        if number > 800:
            continue
        path = raw_dir / name
        if not path.exists():
            path.write_bytes(get(RAW + name))
        parsed = parse(path.read_text(encoding="utf-8"))
        if parsed is None:
            continue
        fields, body = parsed
        status = fields["Status"].split()[0]
        text = clean(body)
        if status not in FINAL_STATUSES or not MIN_CHARS <= len(text) <= MAX_CHARS:
            continue
        candidates.append({"number": number, "fields": fields, "status": status, "text": text})
    step = max(1, len(candidates) // WANT)
    chosen_numbers = {c["number"] for c in candidates[::step][:WANT]}
    (out / "all_titles.json").write_text(json.dumps([{"number": c["number"], "title": c["fields"]["Title"]} for c in candidates if c["number"] not in chosen_numbers][:60], indent=1) + "\n")
    chosen = candidates[::step][:WANT]
    docs = out / "corpus" / "docs" / "peps"
    docs.mkdir(parents=True, exist_ok=True)
    for old in docs.glob("*.md"):
        old.unlink()
    meta: list[dict[str, Any]] = []
    for c in chosen:
        f, n = c["fields"], c["number"]
        acl = acl_for(n)
        read = {"public": ["group:public"], "core": ["team:core", "team:steering"], "steering": ["team:steering"]}[acl]
        created = f.get("Created", "")
        m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", created)
        months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
        date = f"{m.group(3)}-{months.index(m.group(2)) + 1:02d}-{int(m.group(1)):02d}" if m else "2000-01-01"
        title = f"PEP {n}: {f['Title']}"
        front = (
            "---\ncairn_format: doc-v1\nkind: adr_file\nrepo: peps\n"
            f"path: pep-{n:04d}.md\ntitle: {json.dumps(title)}\nauthor: {json.dumps(f.get('Author', ''))}\n"
            f"date: '{date}'\nread:\n" + "".join(f"- {r}\n" for r in read) + "---\n"
        )
        facts = [f"Status: {c['status']}", f"Author: {f.get('Author', 'unknown')}", f"Created: {created}"]
        for key in ("Replaces", "Superseded-By"):
            if f.get(key):
                facts.append(f"{key}: PEP {f[key]}")
        (docs / f"pep-{n:04d}.md").write_text(
            front + f"# {title}\n\n" + "; ".join(facts) + ".\n\n" + c["text"] + "\n", encoding="utf-8"
        )
        meta.append(
            {
                "number": n,
                "external_id": f"doc:peps/pep-{n:04d}.md",
                "title": f["Title"],
                "status": c["status"],
                "type": f.get("Type", ""),
                "author": f.get("Author", ""),
                "created": created,
                "replaces": f.get("Replaces", ""),
                "superseded_by": f.get("Superseded-By", ""),
                "acl": acl,
                "chars": len(c["text"]),
            }
        )
    directory = {
        "as_of": "2026-11-19",
        "format": "cairn-directory-v1",
        "groups": [{"id": "team:core", "name": "Core developers"}, {"id": "team:steering", "name": "Steering council"}],
        "memberships": [
            {"from": "2020-01-01", "group": "team:core", "user": "alice"},
            {"from": "2020-01-01", "group": "team:core", "user": "carol"},
            {"from": "2020-01-01", "group": "team:steering", "user": "carol"},
        ],
        "users": [
            {"id": "alice", "name": "Alice", "external": False},
            {"id": "bob", "name": "Bob", "external": True},
            {"id": "carol", "name": "Carol", "external": False},
        ],
    }
    (out / "corpus" / "directory").mkdir(parents=True, exist_ok=True)
    (out / "corpus" / "directory" / "directory.json").write_text(json.dumps(directory, indent=2) + "\n")
    (out / "peps.json").write_text(json.dumps(meta, indent=2) + "\n")
    by_status: dict[str, int] = {}
    for m in meta:
        by_status[m["status"]] = by_status.get(m["status"], 0) + 1
    acls = {a: sum(1 for m in meta if m["acl"] == a) for a in ("public", "core", "steering")}
    print(f"{len(meta)} PEPs, {sum(m['chars'] for m in meta) // 1000} k chars; statuses {by_status}; acl {acls}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
