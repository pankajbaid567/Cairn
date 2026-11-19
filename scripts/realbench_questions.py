"""Write the benchmark questions for the PEP corpus (run scripts/realbench_corpus.py first).

Four kinds, so the numbers mean something:
  content      an LLM drafts a question from one passage of a PEP and copies the supporting quote;
               the quote must appear verbatim in the PEP or the question is dropped.
  header       made by code from the PEP header (author, status, replaced-by): no model, no drafting.
  unanswerable PEPs that are not in the corpus, PEP numbers that do not exist, topics it never
               covers: the right behaviour is to abstain.
  permission   the same content questions asked by someone who may not read the PEP (expected:
               abstain, and the PEP must not be cited) and by someone who may (expected: answer).

Questions are AI-drafted. A person must confirm a sample (see scripts/realbench_review.py).

    AWS_PROFILE=... uv run --with awscrt python scripts/realbench_questions.py --out var/realbench
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

from cairn_core.config import SearchSettings
from cairn_retrieval.bedrock.client import BedrockClient

MODEL = "openai.gpt-oss-120b-1:0"
PROMPT = (
    "You write evaluation questions for a retrieval system over Python Enhancement Proposals.\n"
    "Read the passage from one PEP and write ONE {kind} question that this passage answers.\n"
    "Rules: the question must make sense to someone who has not seen the passage; name the topic "
    "in plain words but never write the PEP number or say 'the passage'; paraphrase, do not copy "
    "phrases from the passage; the answer must be fully contained in the passage. Then give a "
    "one or two sentence answer, and copy ONE supporting quote (at most 300 characters) EXACTLY as "
    "written in the passage.\n"
    'Reply with JSON only: {{"question":"...","answer":"...","quote":"..."}}\n\n'
    "PEP title: {title}\n\nPassage:\n{passage}"
)
KINDS = ("why (reason or motivation)", "what (a concrete detail or rule)", "how (a mechanism)")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def passage_of(body: str, rng: random.Random) -> str:
    paras = [p for p in re.split(r"\n\s*\n", body) if len(p.strip()) > 120]
    if not paras:
        return ""
    i = rng.randrange(len(paras))
    out, size = [], 0
    for p in paras[i : i + 4]:
        out.append(p.strip())
        size += len(p)
        if size > 1500:
            break
    return "\n\n".join(out)[:2200]


async def draft(
    client: BedrockClient, title: str, body: str, kind: str, rng: random.Random
) -> dict[str, str] | None:
    for _ in range(4):
        passage = passage_of(body, rng)
        if not passage:
            return None
        try:
            res = await client.converse(
                MODEL,
                system="You output JSON only.",
                user=PROMPT.format(kind=kind, title=title, passage=passage),
                max_tokens=700,
                temperature=0.3,
                estimate_micro_usd=2000,
                reasoning_effort="low",
            )
            m = re.search(r"\{.*\}", res.text, re.S)
            d = json.loads(m.group(0)) if m else {}
        except Exception:  # noqa: S112
            continue
        q, a, quote = (str(d.get(k, "")).strip() for k in ("question", "answer", "quote"))
        if not (q.endswith("?") and a and quote and norm(quote) in norm(body)):
            continue
        if re.search(r"\bPEP\s*\d+", q, re.I) or len(quote) > 400:
            continue
        return {"text": q, "answer": a, "quote": quote}
    return None


def qid(kind: str, key: str) -> str:
    return f"{kind[:3]}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


async def main_async(out: Path, seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311, F841
    peps = json.loads((out / "peps.json").read_text())
    docs = {
        p["number"]: (out / "corpus/docs/peps" / f"pep-{p['number']:04d}.md")
        .read_text()
        .split("\n---\n", 1)[1]
        for p in peps
    }
    client = BedrockClient(
        SearchSettings.model_validate(
            {
                "CAIRN_DB_HOST": "h",
                "CAIRN_APP_DB_PASSWORD": "x",
                "CAIRN_SESSION_SECRET": "x" * 40,
                "CAIRN_AUDIT_HASH_KEY": "y" * 40,
                "CAIRN_BEDROCK_REGION": "ap-south-1",
                "CAIRN_BEDROCK_DAILY_USD": "5",
            }
        )
    )
    gate = asyncio.Semaphore(4)

    async def make(p: dict[str, Any], kind: str, k: int) -> dict[str, Any] | None:
        async with gate:
            r = random.Random(f"{seed}-{p['number']}-{k}")  # noqa: S311
            d = await draft(client, p["title"], docs[p["number"]], kind, r)
        if d is None:
            return None
        return {"pep": p["number"], "acl": p["acl"], **d}

    by_acl = {a: [p for p in peps if p["acl"] == a] for a in ("public", "core", "steering")}
    jobs = []
    for i, p in enumerate(by_acl["public"][:95]):
        jobs.append(make(p, KINDS[i % 3], 0))
    for i, p in enumerate(by_acl["core"][:14]):
        jobs.append(make(p, KINDS[i % 3], 1))
    for i, p in enumerate(by_acl["steering"][:10]):
        jobs.append(make(p, KINDS[i % 3], 2))
    drafts = [d for d in await asyncio.gather(*jobs) if d]
    questions: list[dict[str, Any]] = []

    def add(
        kind: str,
        asker: str,
        text: str,
        gold: list[int],
        expected: str,
        quote: str = "",
        answer: str = "",
        must_not: list[int] | None = None,
        key: str = "",
    ) -> None:
        questions.append(
            {
                "id": qid(kind, f"{asker}|{text}|{key}"),
                "kind": kind,
                "asker": asker,
                "text": text,
                "gold_peps": gold,
                "expected": expected,
                "gold_quote": quote,
                "gold_answer": answer,
                "must_not_cite": must_not or [],
            }
        )

    for d in drafts:
        n, acl = d["pep"], d["acl"]
        if acl == "public":
            add("content", "alice", d["text"], [n], "answer", d["quote"], d["answer"])
        elif acl == "core":
            add("permission", "alice", d["text"], [n], "answer", d["quote"], d["answer"])
            add("permission", "bob", d["text"], [], "abstain", must_not=[n])
        else:
            add("permission", "carol", d["text"], [n], "answer", d["quote"], d["answer"])
            add("permission", "bob", d["text"], [], "abstain", must_not=[n])
            add("permission", "alice", d["text"], [], "abstain", must_not=[n])
    # header questions, built by code
    pub = [p for p in peps if p["acl"] == "public"]
    for p in pub[::6][:10]:
        add(
            "header",
            "alice",
            f"Who wrote the proposal titled '{p['title']}'?",
            [p["number"]],
            "answer",
            p["author"],
            p["author"],
        )
    for p in pub[3::6][:10]:
        add(
            "header",
            "alice",
            f"What is the status of the proposal titled '{p['title']}'?",
            [p["number"]],
            "answer",
            f"Status: {p['status']}",
            p["status"],
        )
    for p in pub:
        if p["superseded_by"]:
            add(
                "header",
                "alice",
                f"Which proposal superseded the one titled '{p['title']}'?",
                [p["number"]],
                "answer",
                p["superseded_by"],
                p["superseded_by"],
            )
    # unanswerable
    tree = (
        json.loads(Path(out / "all_titles.json").read_text())  # noqa: ASYNC240
        if (out / "all_titles.json").exists()
        else []
    )
    present = {p["number"] for p in peps}
    for t in [x for x in tree if x["number"] not in present][:12]:
        add(
            "unanswerable",
            "alice",
            f"What does the proposal titled '{t['title']}' decide?",
            [],
            "abstain",
            key=str(t["number"]),
        )
    for n in (9001, 9100, 9250, 9500, 9999):
        add(
            "unanswerable",
            "alice",
            f"What does PEP {n} propose and who approved it?",
            [],
            "abstain",
            key=str(n),
        )
    for text in (
        "What did the Rust RFC process decide about async traits?",
        "What is the release date of Python 4.0?",
        "Which company funds the Python Software Foundation's largest grant?",
        "How many contributors voted for the Go generics proposal?",
        "What did the Java Community Process decide about records?",
        "Who is the current Steering Council chair's employer?",
        "What is the default garbage collection threshold in the PyPy interpreter?",
    ):
        add("unanswerable", "alice", text, [], "abstain")
    for q in questions:
        q["split"] = (
            "dev" if int(hashlib.sha256(q["id"].encode()).hexdigest(), 16) % 2 == 0 else "test"
        )
    (out / "questions.json").write_text(json.dumps(questions, indent=2) + "\n")
    kinds: dict[str, int] = {}
    for q in questions:
        kinds[q["kind"]] = kinds.get(q["kind"], 0) + 1
    print(len(questions), "questions", kinds, "dev", sum(q["split"] == "dev" for q in questions))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="var/realbench", type=Path)
    ap.add_argument("--seed", default=7, type=int)
    a = ap.parse_args()
    asyncio.run(main_async(a.out, a.seed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
