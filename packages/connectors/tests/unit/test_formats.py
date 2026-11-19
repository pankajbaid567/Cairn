"""Properties 4 to 6 (design): the three file formats round-trip."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cairn_connectors.documents import (
    AclRef,
    DirectorySnapshot,
    DirGroup,
    DirMembership,
    DirUser,
    FormatError,
)
from cairn_connectors.formats.directory import parse_directory, print_directory
from cairn_connectors.formats.frontmatter import parse_front_matter, print_front_matter
from cairn_connectors.formats.slack_export import (
    SlackChannel,
    SlackExport,
    SlackMessage,
    SlackUser,
    parse_export,
    print_export,
    ts_to_datetime,
)


def test_front_matter_round_trip_keeps_dates_as_strings() -> None:
    meta = {"cairn_format": "doc-v1", "date": "2026-03-12", "read": ["team:engineering"], "n": 3}
    body = "# Title\n\nText\n"
    parsed, rest = parse_front_matter(print_front_matter(meta, body))
    assert parsed == meta
    assert isinstance(parsed["date"], str)
    assert rest == body


def test_front_matter_absent_and_invalid() -> None:
    assert parse_front_matter("just text") == ({}, "just text")
    with pytest.raises(FormatError, match="mapping"):
        parse_front_matter("---\n- a\n---\nbody")
    with pytest.raises(FormatError, match="YAML"):
        parse_front_matter("---\n: : :\n  - [\n---\nbody", source="f.md")


def _snapshot() -> DirectorySnapshot:
    return DirectorySnapshot(
        date(2026, 10, 27),
        (DirUser("ben", "Ben"), DirUser("rook", "Rook", True)),
        (DirGroup("team:payments", "Payments"), DirGroup("group:employees", "Employees")),
        (
            DirMembership("ben", "team:payments", date(2026, 9, 1)),
            DirMembership("ben", "group:employees", date(2026, 3, 2), date(2026, 9, 1)),
        ),
    )


def test_directory_round_trip_and_determinism() -> None:
    snap = _snapshot()
    text = print_directory(snap)
    assert text == print_directory(parse_directory(text))
    parsed = parse_directory(text)
    assert set(parsed.users) == set(snap.users)
    assert set(parsed.memberships) == set(snap.memberships)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="nope"),
        lambda d: d["memberships"].append(
            {"user": "ghost", "group": "team:payments", "from": "2026-01-01"}
        ),
        lambda d: d["memberships"].append(
            {"user": "ben", "group": "team:none", "from": "2026-01-01"}
        ),
        lambda d: d["memberships"].append(
            {"user": "ben", "group": "team:payments", "from": "2026-02-02", "to": "2026-01-01"}
        ),
        lambda d: d["users"][0].pop("name"),
    ],
)
def test_directory_rejects_bad_input(mutate: object) -> None:
    import json

    doc = json.loads(print_directory(_snapshot()))
    mutate(doc)  # type: ignore[operator]
    with pytest.raises(FormatError):
        parse_directory(json.dumps(doc))


def test_slack_export_round_trip(tmp_path: Path) -> None:
    export = SlackExport(
        users=(SlackUser("U1", "ben", "Ben"), SlackUser("U2", "asha", "Asha")),
        channels=(
            SlackChannel("C1", "general", 1_700_000_000, ("U1", "U2"), False),
            SlackChannel("G1", "hr-private", 1_700_000_100, ("U2",), True),
        ),
        messages={
            "general": (
                SlackMessage("U1", "hello", "1770000000.000000", "1770000000.000000"),
                SlackMessage("U2", "hi there", "1770000060.000000", "1770000000.000000"),
            ),
            "hr-private": (SlackMessage("U2", "secret", "1770100000.000000"),),
        },
    )
    print_export(export, tmp_path)
    again = parse_export(tmp_path)
    assert again == export
    print_export(again, tmp_path / "second")
    assert (tmp_path / "users.json").read_text() == (tmp_path / "second" / "users.json").read_text()
    assert ts_to_datetime("1770000000.500000").microsecond == 500000


def test_slack_export_errors_name_the_file(tmp_path: Path) -> None:
    (tmp_path / "users.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(FormatError, match=r"users\.json"):
        parse_export(tmp_path)


def test_acl_ref_parse() -> None:
    assert AclRef.parse("team:payments") == AclRef("team", "payments")
    assert AclRef("user", "ben").external_id == "user:ben"
    for bad in ("payments", "robot:x", "team:", "team:Bad Case"):
        with pytest.raises(FormatError):
            AclRef.parse(bad)
