"""Properties 1 to 3 (design): normalization, revision planning and chunk spans."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from cairn_connectors.chunking import chunk_chat, chunk_markdown, estimate_tokens
from cairn_connectors.normalize import (
    LatestRevision,
    RevisionPlan,
    content_hash,
    normalize_text,
    plan_revision,
)

TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # type: ignore[arg-type]  # hypothesis wants a Literal category
        blacklist_characters="\x00",
    ),
    max_size=400,
)


@given(TEXT)
def test_normalization_is_idempotent_and_hash_stable(text: str) -> None:
    once = normalize_text(text)
    assert normalize_text(once) == once
    assert content_hash(once) == content_hash(normalize_text(once))
    assert "\r" not in once
    assert all(line == line.rstrip() for line in once.split("\n"))
    assert once == once.strip("\n")


def test_normalization_examples() -> None:
    assert normalize_text("\n\nab  \r\ncd\t\r\n\r\n") == "ab\ncd"
    assert normalize_text("é") == "é"  # NFC
    assert normalize_text("") == ""


T0 = datetime(2026, 3, 1, tzinfo=UTC)


def test_plan_revision_rules() -> None:
    latest = LatestRevision("a" * 64, T0)
    assert plan_revision(None, "b" * 64, T0) is RevisionPlan.INSERT
    assert plan_revision(latest, "a" * 64, T0 - timedelta(days=9)) is RevisionPlan.SKIP_UNCHANGED
    assert plan_revision(latest, "b" * 64, T0 - timedelta(seconds=1)) is RevisionPlan.REJECT_OLDER
    assert plan_revision(latest, "b" * 64, T0) is RevisionPlan.INSERT
    assert plan_revision(latest, "b" * 64, T0 + timedelta(days=1)) is RevisionPlan.INSERT


WORDS = st.lists(
    st.text(alphabet="abcdefghij", min_size=1, max_size=9), min_size=0, max_size=40
).map(" ".join)
PARAS = st.lists(WORDS, min_size=0, max_size=8).map("\n\n".join)


@given(PARAS, st.integers(min_value=3, max_value=60))
def test_markdown_chunks_are_exact_disjoint_and_cover_the_words(body: str, target: int) -> None:
    text = normalize_text("# H1\n\n" + body + "\n\n## H2\n\n" + body)
    chunks = chunk_markdown(text, target=target, hard_max=target * 2)
    covered = 0
    last_end = 0
    for i, c in enumerate(chunks):
        assert c.ordinal == i
        assert text[c.start : c.end] == c.text
        assert c.start >= last_end  # disjoint and ordered
        assert c.token_estimate >= 1
        last_end = c.end
        covered += len(c.text.split())
    assert covered == len(text.split())  # every word is in exactly one chunk


def test_chunks_do_not_cross_headings_and_carry_the_heading_path() -> None:
    text = normalize_text("# A\n\none two three\n\n## B\n\nfour five\n")
    chunks = chunk_markdown(text, target=300)
    assert [c.heading_path for c in chunks] == [("A",), ("A", "B")]
    assert "four" not in chunks[0].text


def test_chat_messages_are_never_split_across_chunks() -> None:
    lines = [f"[2026-03-01T10:{i:02d}:00Z] ben: " + "word " * 30 for i in range(20)]
    text = normalize_text("\n".join(lines))
    chunks = chunk_chat(text, target=100, hard_max=200)
    assert len(chunks) > 1
    for c in chunks:
        for line in c.text.split("\n"):
            assert line.startswith("[2026-03-01")  # whole message lines only


def test_a_huge_unit_is_split_at_sentences_then_words() -> None:
    sentence = " ".join(["word"] * 40) + "."
    text = normalize_text(" ".join([sentence] * 30))
    chunks = chunk_markdown(text, target=100, hard_max=150)
    assert len(chunks) > 3
    assert all(c.token_estimate <= 150 for c in chunks)
    assert all(text[c.start : c.end] == c.text for c in chunks)


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("one") == 2
    assert estimate_tokens("a b c d e f g h i j") == 13
