"""The deploy runbook (free-tier retarget, task 9.4; Requirement 17.13).

`docs/deploy.md` must name every user-owned step FU1 to FU9 and mark it "Needs user", cover the
sections of design §14, give the pinned wrangler command in the order Cloudflare requires (create
the project, set the secret, then deploy), name the volume the user removes, and hold no secret
value. Link resolution is `make docs-check`; this file adds the content rules.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from scripts.check_docs import check_links

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DEPLOY_DOC: Final = REPO_ROOT / "docs" / "deploy.md"
FU_IDS: Final = tuple(f"FU{n}" for n in range(1, 10))
NEEDS_USER: Final = "Needs user"
WRANGLER: Final = "pnpm dlx wrangler@4.156.2"
HEADING: Final = re.compile(r"^(#{2,6})\s+(.*)$")
HEX_LITERAL: Final = re.compile(r"\b[0-9a-fA-F]{32,}\b")
CONNECTION_STRING: Final = re.compile(r"postgres(?:ql)?://[^\s/:@]+:[^\s@]+@", re.IGNORECASE)


@pytest.fixture(scope="module")
def text() -> str:
    return DEPLOY_DOC.read_text(encoding="utf-8")


def _sections(text: str) -> dict[str, str]:
    """Map each heading (without the #s) to the text below it, up to the next heading of its level.

    Lines inside fenced code blocks are never headings.
    """
    lines = text.splitlines()
    heads: list[tuple[int, int, str]] = []  # (line index, level, title)
    in_fence = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        match = None if in_fence else HEADING.match(line)
        if match:
            heads.append((index, len(match.group(1)), match.group(2).strip()))
    result: dict[str, str] = {}
    for position, (index, level, title) in enumerate(heads):
        end = len(lines)
        for next_index, next_level, _ in heads[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        result[title] = "\n".join(lines[index:end])
    return result


def _fu_section(text: str, fu: str) -> str:
    for title, body in _sections(text).items():
        if title.startswith(f"{fu}:"):
            return body
    raise AssertionError(f"docs/deploy.md has no section headed '{fu}: ...'")


def test_runbook_exists() -> None:
    assert DEPLOY_DOC.is_file()


@pytest.mark.parametrize("fu", FU_IDS)
def test_each_fu_step_has_a_section_marked_needs_user(text: str, fu: str) -> None:
    body = _fu_section(text, fu)
    heading = body.splitlines()[0]
    assert NEEDS_USER in heading, f"{fu} heading does not say '{NEEDS_USER}'"
    assert f"**{NEEDS_USER}.**" in body, f"{fu} section lacks the bold '{NEEDS_USER}.' line"


def test_overview_table_lists_every_fu_step_as_needs_user(text: str) -> None:
    rows = [line for line in text.splitlines() if line.startswith("| FU")]
    assert [row.split("|")[1].strip() for row in rows] == list(FU_IDS)
    for row in rows:
        assert NEEDS_USER in row, f"overview row without '{NEEDS_USER}': {row[:40]}"


@pytest.mark.parametrize(
    "heading",
    [
        "Topology",
        "Limits and sources",
        "Prerequisites",
        "Sleep and wake",
        "Heartbeat gaps",
        "Neon compute hours",
        "Neuron budget",
        "Password rotation",
        "Teardown",
        "Troubleshooting",
        "End-to-end check",
    ],
)
def test_design_section_present(text: str, heading: str) -> None:
    assert heading in _sections(text), f"missing section '{heading}'"


def test_limits_section_cites_sources_for_each_platform(text: str) -> None:
    limits = _sections(text)["Limits and sources"]
    for needle in (
        "render.com/docs/free",
        "neon.com/faqs/free-plan-limits-and-quotas",
        "developers.cloudflare.com/pages",
        "developers.cloudflare.com/workers-ai/platform/pricing",
        "750",
        "100 CU-hours",
        "10,000 neurons",
    ):
        assert needle in limits, f"limits table lacks '{needle}'"


def test_neon_hours_section_states_the_polling_consequence(text: str) -> None:
    section = _sections(text)["Neon compute hours"]
    assert "while the Render service is awake, Neon is awake too" in section
    assert "100 CU-hours" in section
    assert "186" in section  # the always-awake case against the 100 CU-hour allowance


def test_wrangler_is_pinned_and_secret_is_set_before_deploy(text: str) -> None:
    section = _fu_section(text, "FU6")
    create = section.index(f"{WRANGLER} pages project create")
    secret = section.index(f"{WRANGLER} pages secret put CAIRN_PROXY_TOKEN")
    deploy = section.index(f"{WRANGLER} pages deploy dist")
    assert create < secret < deploy
    unpinned = [
        line.strip()
        for line in text.splitlines()
        if "pnpm dlx wrangler" in line and WRANGLER not in line
    ]
    assert not unpinned, f"unpinned wrangler command: {unpinned}"
    assert "make web-dist" in section


def test_old_volume_removal_is_a_user_step_with_its_own_command(text: str) -> None:
    body = _fu_section(text, "FU7")
    assert "docker volume rm cairn_pgdata\n" in body
    assert "cairn_pgdata-pgvector" in body  # says which volume must stay


def test_remote_targets_and_optional_live_check_are_named(text: str) -> None:
    assert "make env-neon" in _fu_section(text, "FU1")
    fu2 = _fu_section(text, "FU2")
    assert "make remote-init" in fu2
    assert "make remote-status" in fu2
    fu8 = _fu_section(text, "FU8")
    assert "make workers-ai-live-check" in fu8
    assert "Optional" in fu8


def test_render_step_says_keys_added_later_are_typed_by_hand(text: str) -> None:
    body = _fu_section(text, "FU4")
    assert "added to `render.yaml` after the Blueprint was first created" in body
    assert "by hand" in body


def test_render_step_keeps_owner_and_admin_credentials_out_of_render(text: str) -> None:
    body = _fu_section(text, "FU4")
    assert "Never enter `CAIRN_OWNER_DB_PASSWORD` or `CAIRN_DB_ADMIN_PASSWORD`" in body


def test_all_links_are_relative_and_resolve(text: str) -> None:
    assert "](/" not in text
    assert "file://" not in text
    assert "/Users/" not in text
    assert check_links(text, DEPLOY_DOC, REPO_ROOT) == []


def test_runbook_holds_no_secret_value(text: str) -> None:
    without_fences = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    assert not HEX_LITERAL.search(without_fences)
    assert not CONNECTION_STRING.search(text)
    for line in text.splitlines():
        match = re.match(r"\s*(CAIRN_\w*(?:PASSWORD|TOKEN)\w*|CLOUDFLARE_API_TOKEN)=(\S+)", line)
        assert match is None, f"assignment of a secret-like key: {match.group(1) if match else ''}"
