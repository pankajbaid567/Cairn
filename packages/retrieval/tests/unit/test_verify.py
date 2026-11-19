"""The verifier drops what does not check out, and the answer abstains when nothing survives
(plan 4.2, 4.13; L6)."""

from __future__ import annotations

from datetime import date

from cairn_retrieval.answer import NOT_FOUND_TEXT, AnswerBody, CitationInfo, ClaimOut, citation_id
from cairn_retrieval.types import Candidate
from cairn_retrieval.verify import CANARY, dates_in, support, verify_answer

HIDDEN = "KP-HR-ABCDEFGHIJKLMNOP"
VISIBLE = "KP-SEC-QRSTUVWXYZ234567"


def cand(n: int, text: str) -> Candidate:
    return Candidate(
        n, n, f"doc:eng-docs/f{n}.md", "a" * 64, 0, len(text), text, f"Doc {n}", "doc", n
    )


A = cand(1, "ADR-007 moved payment-service from MongoDB to PostgreSQL. Accepted on 2026-03-07.")
B = cand(2, f"Security note with token {VISIBLE} for rotation.")


def answer(*claims: tuple[str, list[Candidate]]) -> AnswerBody:
    out = [ClaimOut(t, tuple(citation_id(c) for c in cs)) for t, cs in claims]
    cites = {citation_id(c): c for _, cs in claims for c in cs}
    infos = tuple(
        CitationInfo(i, c.title, c.source_kind, c.external_id, c.text[:100])
        for i, c in cites.items()
    )
    text = " ".join(f"{t} [{n}]" for n, (t, _) in enumerate(claims, start=1))
    return AnswerBody(answer=text, claims=tuple(out), citations=infos, searched=("docs_folder",))


def test_a_supported_claim_survives_untouched() -> None:
    a = answer(("payment-service moved from MongoDB to PostgreSQL", [A]))
    out, report = verify_answer(a, [A, B])
    assert out.claims == a.claims
    assert report.kept == 1
    assert report.dropped == {}
    assert out.verification["checked"] == 1


def test_unsupported_claims_are_dropped_and_the_answer_is_rebuilt_from_the_rest() -> None:
    a = answer(
        ("payment-service moved from MongoDB to PostgreSQL", [A]),
        ("The team also adopted Kubernetes and Terraform for deployment", [A]),
    )
    out, report = verify_answer(a, [A])
    assert [c.text for c in out.claims] == ["payment-service moved from MongoDB to PostgreSQL"]
    assert report.dropped == {"unsupported": 1}
    assert "Kubernetes" not in out.answer
    assert out.answer.endswith("[1]")
    assert [c.id for c in out.citations] == [citation_id(A)]


def test_the_answer_abstains_with_the_constant_reply_when_nothing_survives() -> None:
    a = answer(("Completely invented statement about quantum ledgers", [A]))
    out, report = verify_answer(a, [A])
    assert out.abstained
    assert out.answer == NOT_FOUND_TEXT
    assert out.claims == ()
    assert report.kept == 0
    assert out.searched == ("docs_folder",)


def test_a_citation_outside_the_visible_set_is_dropped() -> None:
    hidden = cand(9, "Hidden HR review notes about compensation.")
    a = answer(("HR review notes about compensation", [hidden]))
    out, report = verify_answer(a, [A])  # `hidden` was not packed for this caller
    assert out.abstained
    assert report.dropped == {"citation_not_visible": 1}


def test_dates_must_come_from_the_cited_text_and_not_precede_the_decision() -> None:
    ok = answer(("ADR-007 was accepted on 2026-03-07", [A]))
    assert not verify_answer(ok, [A])[0].abstained
    invented = answer(("ADR-007 was accepted on 2026-04-01", [A]))
    assert verify_answer(invented, [A])[1].dropped == {"date_not_in_evidence": 1}
    early = cand(3, "ADR-007 was accepted on 2026-01-01 according to this note.")
    before = answer(("ADR-007 was accepted on 2026-01-01", [early]))
    out, report = verify_answer(before, [early], decision_starts={"ADR-007": date(2026, 3, 3)})
    assert out.abstained
    assert report.dropped == {"date_before_decision_start": 1}
    assert dates_in("on 2026-02-30 and 2026-03-07") == {
        date(2026, 3, 7)
    }  # impossible dates ignored


def test_l6_a_canary_not_in_the_visible_evidence_never_leaves() -> None:
    leak = answer((f"The note holds {HIDDEN} for rotation", [B]))
    out, report = verify_answer(leak, [B])
    assert out.abstained
    assert report.canary_hits == 1
    assert HIDDEN not in str(out.to_json())
    fine = answer((f"The note holds {VISIBLE} for rotation", [B]))
    assert not verify_answer(fine, [B])[0].abstained  # a canary the caller can read is allowed
    sneaky = AnswerBody(
        answer=f"Fine claim about rotation notes. {HIDDEN}",
        claims=(ClaimOut("Security note with token for rotation", (citation_id(B),)),),
        citations=(CitationInfo(citation_id(B), "Doc 2", "doc", "x", "q"),),
    )
    assert verify_answer(sneaky, [B])[0].abstained  # hidden token in the free-text answer too


def test_abstentions_pass_through_and_reports_hold_no_claim_text() -> None:
    nf = AnswerBody(answer=NOT_FOUND_TEXT, abstained=True)
    assert verify_answer(nf, [A])[0] is nf
    a = answer(("Invented statement with SECRET-TEXT-123 inside", [A]))
    out, _ = verify_answer(a, [A])
    assert "SECRET-TEXT-123" not in str(out.to_json())


def test_support_score_and_canary_pattern() -> None:
    assert support("moved from MongoDB to PostgreSQL", [A.text]) == 1.0
    assert support("quantum blockchain ledger synergy", [A.text]) == 0.0
    assert CANARY.findall(f"x {HIDDEN} y {VISIBLE}") == [HIDDEN, VISIBLE]
