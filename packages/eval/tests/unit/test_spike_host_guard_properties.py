"""Property test for the spike host guard (free-tier retarget task 3.4, FT-28).

Feature: cairn-free-tier-retarget, Property 4: Spike commands accept only local database hosts

**Validates: Requirements 3.8**

For any host string (IPv4, IPv6, bracketed IPv6, DNS names, compose service names, empty)
and either value of ``--allow-remote``, the spike host guard accepts the host if and only if
the flag is given, the host is a loopback address (``127.0.0.0/8``, ``::1``, ``localhost``),
or the host is one of the compose service names ``db`` and ``ft-db``.

Each generator below draws hosts whose expected verdict is known by construction, so the
oracle never calls the code under test. ``cairn-s2 run`` is driven with ``load_config`` and
``connect_admin`` replaced: no ``.env`` is read and no connection is ever opened.

Not generated, because the requirement does not decide them: IPv4-mapped IPv6 addresses
(``::ffff:127.0.0.1``), IPv6 zone identifiers (``::1%lo0``) and bracketed IPv4. The guard
accepts the loopback ones, which are local, so accepting them is on the safe side.
"""

from __future__ import annotations

import contextlib
import io
import ipaddress
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

import cairn_core.testing
from cairn_eval.spikes.host_guard import host_allowed, is_local_host, refusal
from cairn_eval.spikes.s2_rls_explain.cli import main as s2_main

LOCAL_NAMES = ("localhost", "db", "ft-db")
LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
LOOPBACK_V6 = ipaddress.IPv6Address(1)
EXIT_USAGE = 2

flags = st.booleans()


def any_case(text: str) -> st.SearchStrategy[str]:
    """``text`` with each letter independently upper- or lower-case."""
    return st.tuples(*(st.sampled_from(sorted({c.lower(), c.upper()})) for c in text)).map("".join)


def maybe_bracketed(text: str) -> st.SearchStrategy[str]:
    return st.sampled_from([text, f"[{text}]"])


# --- hosts the guard must accept without the flag -------------------------------------------

local_names = st.sampled_from(LOCAL_NAMES).flatmap(any_case)

loopback_v4 = st.tuples(st.integers(0, 255), st.integers(0, 255), st.integers(0, 255)).map(
    lambda t: f"127.{t[0]}.{t[1]}.{t[2]}"
)


@st.composite
def loopback_v6_full(draw: st.DrawFn) -> str:
    """``::1`` written out as eight groups, each zero-padded to a random width."""
    groups = [draw(st.sampled_from(["0", "00", "000", "0000"])) for _ in range(7)]
    groups.append(draw(st.sampled_from(["1", "01", "001", "0001"])))
    return ":".join(groups)


loopback_v6 = st.one_of(
    st.sampled_from(["::1", "0::1", "::0:1", "::0001", "0:0::1"]), loopback_v6_full()
).flatmap(maybe_bracketed)

local_hosts = st.one_of(local_names, loopback_v4, loopback_v6)

# --- hosts the guard must refuse without the flag -------------------------------------------

remote_v4 = st.ip_addresses(v=4).filter(lambda a: a not in LOOPBACK_V4).map(str)


def is_remote_v6(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Not ``::1`` and not IPv4-mapped (the requirement does not decide mapped addresses)."""
    return (
        isinstance(address, ipaddress.IPv6Address)
        and address != LOOPBACK_V6
        and address.ipv4_mapped is None
    )


remote_v6 = (
    st.ip_addresses(v=6)
    .filter(is_remote_v6)
    .flatmap(lambda a: st.sampled_from([a.compressed, a.exploded, a.compressed.upper()]))
    .flatmap(maybe_bracketed)
)

dns_label = st.from_regex(r"[a-z0-9]([a-z0-9-]{0,14}[a-z0-9])?", fullmatch=True)
dns_names = (
    st.lists(dns_label, min_size=1, max_size=4)
    .map(".".join)
    .filter(lambda n: n not in LOCAL_NAMES)
    .flatmap(any_case)
)

socket_dirs = st.from_regex(r"/[a-z0-9_/.-]{0,30}", fullmatch=True)


@st.composite
def near_misses(draw: st.DrawFn) -> str:
    """A local host with something added: padding, a trailing dot, a suffix or prefix, a
    second host in a comma list, or an unbalanced bracket."""
    base = draw(local_hosts)
    remote = draw(st.one_of(dns_names, remote_v4))
    return draw(
        st.sampled_from(
            [
                f" {base}",
                f"{base} ",
                f"{base}.",
                # "-2", not "2": "127.0.0.1" + "2" is another loopback address.
                f"{base}-2",
                f"{base}.example.com",
                f"x{base}",
                f"{base},{remote}",
                f"{remote},{base}",
                f"[{base}",
                f"{base}]",
            ]
        )
    )


remote_hosts = st.one_of(st.just(""), remote_v4, remote_v6, dns_names, socket_dirs, near_misses())

# A host with its expected verdict without the flag.
labelled_hosts = st.one_of(
    local_hosts.map(lambda h: (h, True)), remote_hosts.map(lambda h: (h, False))
)


@given(labelled_hosts, flags)
def test_guard_accepts_exactly_the_flag_loopback_and_compose_names(
    labelled: tuple[str, bool], allow_remote: bool
) -> None:
    host, local = labelled
    assert is_local_host(host) is local
    assert host_allowed(host, allow_remote=allow_remote) is (allow_remote or local)


def spec_local(host: str) -> bool:
    """Requirement 3.8 read literally: a local name (case-insensitive) or a loopback IP
    literal, optionally in brackets."""
    if host.lower() in LOCAL_NAMES:
        return True
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(literal).is_loopback
    except ValueError:
        return False


@given(st.text(), flags)
def test_guard_on_arbitrary_text_never_raises_and_accepts_only_local_hosts(
    host: str, allow_remote: bool
) -> None:
    allowed = host_allowed(host, allow_remote=allow_remote)
    if allow_remote:
        assert allowed is True
    else:
        assert allowed is spec_local(host)


@given(st.one_of(remote_hosts, st.text()), st.sampled_from(["cairn-s1", "cairn-s2"]))
def test_refusal_is_one_line_naming_the_command_host_and_flag(host: str, command: str) -> None:
    message = refusal(command, host)
    assert message.startswith(f"{command}: refusing database host ")
    assert repr(host) in message
    assert "--allow-remote" in message
    assert "db" in message
    assert "ft-db" in message
    assert "\n" not in message
    assert "\r" not in message


class ConnectRecorder:
    """Stands in for ``cairn_core.testing.connect_admin``: counts the call and stops the
    command there, as an unreachable database would (exit 2)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise cairn_core.testing.DatabaseUnavailable("stopped by the test before connecting")


@given(labelled_hosts, flags)
def test_s2_run_exits_2_before_connecting_exactly_when_the_guard_refuses(
    labelled: tuple[str, bool], allow_remote: bool
) -> None:
    host, local = labelled
    refused = not (allow_remote or local)
    recorder = ConnectRecorder()
    config = SimpleNamespace(target=SimpleNamespace(host=host), settings=None)
    stderr = io.StringIO()
    argv_flag = ["--allow-remote"] if allow_remote else []
    with (
        tempfile.TemporaryDirectory() as tmp,
        pytest.MonkeyPatch.context() as mp,
        contextlib.redirect_stderr(stderr),
    ):
        mp.chdir(tmp)
        mp.setattr(cairn_core.testing, "load_config", lambda *_a, **_k: config)
        mp.setattr(cairn_core.testing, "connect_admin", recorder)
        out = Path(tmp) / "out"
        code = s2_main(["run", "--out", str(out), *argv_flag])
        assert code == EXIT_USAGE
        assert recorder.calls == (0 if refused else 1)
        assert ("refusing database host" in stderr.getvalue()) is refused
        if refused:
            assert not out.exists()
