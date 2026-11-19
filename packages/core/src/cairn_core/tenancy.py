"""Which organisation the current request belongs to.

The API sets this once per request, after it has authenticated the caller and checked that they
belong to the organisation. Everything that opens a transaction for a principal reads it and
writes ``cairn.org_id`` into the transaction, where the restrictive row-level policy on every
table reads it (migration 0013). When nothing is set the setting stays empty and no tenant row is
visible: forgetting to set an organisation fails closed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_ORG: ContextVar[int | None] = ContextVar("cairn_org", default=None)
_ACCOUNT: ContextVar[str | None] = ContextVar("cairn_account", default=None)


def current_org() -> int | None:
    return _ORG.get()


def current_account() -> str | None:
    return _ACCOUNT.get()


def set_request_scope(org_id: int | None, account_id: str | None = None) -> None:
    """Bind the organisation (and account) for the rest of this request's task."""
    _ORG.set(org_id)
    _ACCOUNT.set(account_id)


@contextmanager
def org_scope(org_id: int | None, account_id: str | None = None) -> Iterator[None]:
    """Bind an organisation for a block (workers, tests), restoring the previous one after."""
    t_org, t_acct = _ORG.set(org_id), _ACCOUNT.set(account_id)
    try:
        yield
    finally:
        _ORG.reset(t_org)
        _ACCOUNT.reset(t_acct)
