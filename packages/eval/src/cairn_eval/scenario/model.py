"""Typed models for the Kestrel Pay scenario file, schema version 1 (design §11, Data Models).

The models are the single source of truth for the scenario format:

* ``scenario-v1.schema.json`` is generated from them (``cairn_eval.scenario.schema``).
* The validator (task 12.3) loads the YAML with the timestamp-free loader, checks the
  JSON Schema, builds a :class:`Scenario` and then runs the semantic rules.

Conventions

* Every model is frozen and rejects unknown keys (``extra="forbid"``), so a typo in the
  YAML is a schema error instead of silently ignored data.
* Dates are ``YYYY-MM-DD`` strings in the file and ``datetime.date`` in the models.
  Timestamps are RFC 3339 strings with a zone and become aware ``datetime`` objects.
* Python-keyword field names use an alias: ``Membership.from_`` is ``from`` in the file.
  ``TimelineEvent.on`` is spelled ``on`` in both places (the loader keeps it a string key).
* Identifiers that *define* an entity (``Person.id``, ``Source.id`` and so on) carry the
  patterns of design §11. Fields that *refer* to an entity use :data:`Ref`, a plain
  non-empty string, so a dangling reference is reported by the validator as REF-1 with a
  JSON pointer instead of being hidden behind a pattern error.
* Some "exactly one of" and "kind implies field" constraints are enforced by model
  validators only (the JSON Schema cannot express them without ``oneOf`` noise). The
  validator reports such failures as SCH.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Final, Literal, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic.json_schema import WithJsonSchema

# --------------------------------------------------------------------------------------
# Scalar types
# --------------------------------------------------------------------------------------

_DATE_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIMESTAMP_RE: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)

DATE_PATTERN: Final = r"^\d{4}-\d{2}-\d{2}$"
TIMESTAMP_PATTERN: Final = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"


def parse_iso_date(value: object) -> object:
    """Accept ``YYYY-MM-DD`` strings (and plain ``date`` objects from Python callers).

    Integers, datetimes and other date spellings are rejected, so a YAML file that was
    loaded with a stock loader (which turns dates into objects) fails loudly in tests.
    """
    if isinstance(value, dt.datetime):
        raise ValueError("expected a date (YYYY-MM-DD), not a datetime")
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and _DATE_RE.fullmatch(value):
        return dt.date.fromisoformat(value)
    raise ValueError("expected a date string in the form YYYY-MM-DD")


def parse_iso_timestamp(value: object) -> object:
    """Accept RFC 3339 timestamps with a zone, such as ``2026-02-18T09:12:00Z``."""
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and _TIMESTAMP_RE.fullmatch(value):
        return dt.datetime.fromisoformat(value)
    raise ValueError("expected an RFC 3339 timestamp with a zone, e.g. 2026-02-18T09:12:00Z")


Date = Annotated[
    dt.date,
    BeforeValidator(parse_iso_date),
    WithJsonSchema({"type": "string", "format": "date", "pattern": DATE_PATTERN}),
]
Timestamp = Annotated[
    AwareDatetime,
    BeforeValidator(parse_iso_timestamp),
    WithJsonSchema({"type": "string", "format": "date-time", "pattern": TIMESTAMP_PATTERN}),
]

# Defining ids (design "Data Models": id formats enforced by the JSON Schema).
SLUG_PATTERN: Final = r"^[a-z][a-z0-9_-]*$"
SOURCE_ID_PATTERN: Final = r"^[a-z][a-z0-9-]*$"
DECISION_ID_PATTERN: Final = r"^ADR-\d{3}$"
TWIST_ID_PATTERN: Final = r"^TW-[A-Z0-9-]+$"
CANARY_ID_PATTERN: Final = r"^CAN-(HR|PARTNER|SEC|INC)-\d{2}$"
QUESTION_ID_PATTERN: Final = r"^Q\d{3}$"
CLAIM_ID_PATTERN: Final = r"^C\d+$"
CONFLICT_ID_PATTERN: Final = r"^K\d+$"
SCOPE_PATTERN: Final = r"^(repo|channel|meeting):[a-z0-9][a-z0-9_.-]*$"
ASKER_PATTERN: Final = r"^(agent:)?[a-z][a-z0-9_]*$"
PR_REF_PATTERN: Final = r"^[a-z][a-z0-9-]*#\d+$"

Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]  # people, teams, repos, channels
SourceId = Annotated[str, StringConstraints(pattern=SOURCE_ID_PATTERN)]
DecisionId = Annotated[str, StringConstraints(pattern=DECISION_ID_PATTERN)]
TwistId = Annotated[str, StringConstraints(pattern=TWIST_ID_PATTERN)]
CanaryId = Annotated[str, StringConstraints(pattern=CANARY_ID_PATTERN)]
QuestionId = Annotated[str, StringConstraints(pattern=QUESTION_ID_PATTERN)]
ClaimId = Annotated[str, StringConstraints(pattern=CLAIM_ID_PATTERN)]
ConflictId = Annotated[str, StringConstraints(pattern=CONFLICT_ID_PATTERN)]
ScopeRef = Annotated[str, StringConstraints(pattern=SCOPE_PATTERN)]
Asker = Annotated[str, StringConstraints(pattern=ASKER_PATTERN)]
PrRef = Annotated[str, StringConstraints(pattern=PR_REF_PATTERN)]
Ref = Annotated[str, StringConstraints(min_length=1)]  # a reference; resolved by REF-1

# --------------------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------------------

LifecycleEvent = Literal[
    "proposed",
    "accepted",
    "rejected",
    "withdrawn",
    "implemented",
    "superseded",
    "deprecated",
    "reverted",
    "reopened",
]
AnnotationEvent = Literal["discussed", "amended", "partially_reverted"]
EventName = LifecycleEvent | AnnotationEvent
# A decision's state as the as-of oracle reports it ("reopened" leads back to "proposed").
LifecycleState = Literal[
    "proposed",
    "accepted",
    "rejected",
    "withdrawn",
    "implemented",
    "superseded",
    "deprecated",
    "reverted",
    "absent",
]
TwistKind = Literal[
    "cross_source_archaeology",
    "private_rationale",
    "partial_revert",
    "split_rationale",
    "supersession",
    "deleted_source",
    "backdated_valid_time",
    "scheduled_change",
    "conflicting_dates",
    "drift",
    "restricted_decision",
    "temporary_access",
    "stale_doc",
    "hr_restricted",
    "team_move",
    "injected_instruction",
]
Tier = Literal["T1", "T2", "T3", "T4", "T5"]  # T6 (AI inference) is never evidence
SourceKind = Literal[
    "doc", "adr_file", "wiki", "meeting_note", "chat_thread", "pull_request", "issue"
]
CanaryFamily = Literal["hr", "partner", "security", "incident"]
CanaryKind = Literal["token", "phrase"]
QuestionCategory = Literal[
    "why",
    "when_who",
    "as_of",
    "conflict",
    "supersession",
    "not_found",
    "permission",
    "agent_brief",
]

#: Runtime views of the vocabularies, for validators and tests.
LIFECYCLE_EVENTS: Final[tuple[str, ...]] = get_args(LifecycleEvent)
ANNOTATION_EVENTS: Final[tuple[str, ...]] = get_args(AnnotationEvent)
LIFECYCLE_STATES: Final[tuple[str, ...]] = get_args(LifecycleState)
TWIST_KINDS: Final[tuple[str, ...]] = get_args(TwistKind)
TIERS: Final[tuple[str, ...]] = get_args(Tier)
SOURCE_KINDS: Final[tuple[str, ...]] = get_args(SourceKind)
CANARY_KINDS: Final[tuple[str, ...]] = get_args(CanaryKind)
QUESTION_CATEGORIES: Final[tuple[str, ...]] = get_args(QuestionCategory)


class Model(BaseModel):
    """Base for every scenario model: frozen, no unknown keys, aliases usable by name."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_by_name=True)


# --------------------------------------------------------------------------------------
# Organisation: meta, people, teams, memberships
# --------------------------------------------------------------------------------------


class Meta(Model):
    id: Slug
    name: str
    reference_date: Date  # "now" for the scenario
    canary_seed: str = Field(min_length=1)
    github_org: str | None = None


class Person(Model):
    id: Slug
    name: str  # first name only
    title: str
    external: bool = False


class Team(Model):
    id: Slug
    name: str | None = None


class Membership(Model):
    """A person's membership of a team over ``[from, to)``; ``to`` absent means open."""

    person: Ref
    team: Ref
    from_: Date = Field(alias="from")
    to: Date | None = None
    note: str | None = None


class PrincipalRef(Model):
    """Reference to exactly one team or one person: ``{team: payments}`` or ``{person: dev}``."""

    team: Ref | None = None
    person: Ref | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> PrincipalRef:
        if (self.team is None) == (self.person is None):
            raise ValueError("exactly one of 'team' and 'person' is required")
        return self


class AccessGrant(PrincipalRef):
    permission: Literal["read", "write", "admin"]


class Repo(Model):
    id: Slug
    visibility: Literal["private", "public"]
    access: list[AccessGrant] = []


class Channel(Model):
    id: Slug
    private: bool
    members: list[PrincipalRef] = []  # private channels only; public means every employee


# --------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------


class Meeting(Model):
    attendees: list[Ref]
    share_with: list[PrincipalRef] = []


class Location(Model):
    """Where a source lives: a repo (with a path), a channel, or a meeting."""

    repo: Ref | None = None
    path: str | None = None
    channel: Ref | None = None
    meeting: Meeting | None = None


class Message(Model):
    author: Ref
    at: Timestamp
    text: str


class PullRequest(Model):
    number: int = Field(ge=1)
    state: Literal["open", "merged", "closed"]
    merged_at: Timestamp | None = None
    labels: list[str] = []


class Issue(Model):
    number: int = Field(ge=1)
    state: Literal["open", "closed"] = "open"
    labels: list[str] = []


class Source(Model):
    """One piece of evidence the renderers turn into a doc, thread, note, PR or issue.

    Text fields (``title``, ``body``, ``messages[].text``) may hold ``{{canary:ID}}``
    placeholders for token canaries.
    """

    id: SourceId
    kind: SourceKind
    date: Date
    available: Date | None = None  # imported later than ``date``
    last_edited: Date | None = None
    deleted: Date | None = None
    location: Location
    author: Ref | None = None
    title: str | None = None
    body: str | None = None
    messages: list[Message] = []
    pr: PullRequest | None = None
    issue: Issue | None = None

    @property
    def available_or_date(self) -> dt.date:
        """When Cairn could first know this source (recorded time lower bound)."""
        return self.available if self.available is not None else self.date

    @property
    def scope(self) -> str | None:
        """The scope string (``meeting:<source id>``, ``channel:<id>``, ``repo:<id>``) or None."""
        if self.location.meeting is not None:
            return f"meeting:{self.id}"
        if self.location.channel is not None:
            return f"channel:{self.location.channel}"
        if self.location.repo is not None:
            return f"repo:{self.location.repo}"
        return None


# --------------------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------------------


class DecisionOption(Model):
    name: str
    verdict: Literal["chosen", "rejected", "considered"]


class Anchors(Model):
    repos: list[Ref] = []
    paths: list[str] = []
    prs: list[PrRef] = []  # "payment-service#123"


class TimelineEvent(Model):
    """One dated event: ``on`` is when it happened, ``effective`` when it holds in reality."""

    event: EventName
    on: Date
    effective: Date | None = None
    source: Ref
    by: Ref | None = None  # superseding decision for "superseded"
    note: str | None = None


class Claim(Model):
    id: ClaimId
    text: str
    tier: Tier
    support_sets: list[list[Ref]]  # each inner list is one support set of source ids


class ConflictSide(Model):
    value: str
    source: Ref
    tier: Tier


class Conflict(Model):
    id: ConflictId
    subject: str
    sides: list[ConflictSide]


class Decision(Model):
    id: DecisionId
    title: str
    question: str | None = None
    outcome: str | None = None
    options: list[DecisionOption] = []
    decision_makers: list[Ref] = []
    approvers: list[Ref] = []
    consulted: list[PrincipalRef] = []
    informed: list[PrincipalRef] = []
    anchors: Anchors | None = None
    confirmation: str | None = None
    timeline: list[TimelineEvent]
    claims: list[Claim] = []
    conflicts: list[Conflict] = []
    supersedes: list[Ref] = []
    superseded_by: list[Ref] = []


# --------------------------------------------------------------------------------------
# Twists, canaries, questions
# --------------------------------------------------------------------------------------


class Twist(Model):
    """A named hard case. Which of ``decisions``, ``people`` and ``sources`` must be filled
    depends on ``kind`` (rule TW-2)."""

    id: TwistId
    kind: TwistKind
    decisions: list[Ref] = []
    people: list[Ref] = []
    sources: list[Ref] = []
    description: str | None = None


class Canary(Model):
    """A planted string. ``token`` canaries are generated (``canary_token``) and referenced
    as ``{{canary:ID}}``; ``phrase`` canaries are literal text."""

    id: CanaryId
    family: CanaryFamily
    kind: CanaryKind
    phrase: str | None = Field(default=None, min_length=1)
    allowed_scopes: list[ScopeRef]

    @model_validator(mode="after")
    def _phrase_matches_kind(self) -> Canary:
        if self.kind == "phrase" and self.phrase is None:
            raise ValueError("a phrase canary needs a 'phrase'")
        if self.kind == "token" and self.phrase is not None:
            raise ValueError("a token canary must not have a 'phrase'")
        return self


class AsOf(Model):
    valid: Date
    recorded: Date


class Expected(Model):
    decisions: list[Ref] = []
    evidence: list[Ref] = []
    lifecycle: dict[str, LifecycleState] = {}  # decision id -> state at the as-of dates
    abstain: bool = False
    must_not_contain: list[Ref] = []  # canary ids
    must_not_cite: list[Ref] = []  # source ids


class Question(Model):
    id: QuestionId
    split: Literal["dev", "test"]
    category: QuestionCategory
    asker: Asker  # a person id, or "agent:<person id>"
    text: str
    as_of: AsOf | None = None  # None means the reference date
    twists: list[Ref] = []
    expected: Expected


# --------------------------------------------------------------------------------------
# Root
# --------------------------------------------------------------------------------------


class Scenario(Model):
    schema_version: Literal[1]
    meta: Meta
    people: list[Person]
    teams: list[Team]
    memberships: list[Membership]
    repos: list[Repo]
    channels: list[Channel]
    sources: list[Source]
    decisions: list[Decision]
    twists: list[Twist]
    canaries: list[Canary]
    questions: list[Question]

    def source(self, source_id: str) -> Source:
        """The source with this id; raises ``KeyError`` if there is none."""
        for src in self.sources:
            if src.id == source_id:
                return src
        raise KeyError(source_id)
