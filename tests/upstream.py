"""Telling an upstream server that changed from one that is merely down.

A live test fails for two reasons that call for opposite reactions. If a server
changed what it publishes, the fixture standing in for it is now fiction and
someone has to look. If the server is simply not answering, there is nothing to
do but run it again later. Reported the same way, the first is lost in the noise
of the second, and a suite nobody trusts is a suite nobody runs.

So the live tests route their failures through here, and the two arrive as
different pytest outcomes: a skip naming the server for an outage, a failure
naming it for a change. Read them with ``pytest -m live -rs``, which prints the
skip reasons. That flag is part of the documented command rather than a nicety,
because a skip is quiet: a test that skips on every run is a server that has gone
away for good, and only the reasons make that visible.

Anything that answers at all has not gone away, so a 404 is counted as a change
rather than an outage. That is not a technicality. It is exactly how the Lantern
REST source broke.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import NoReturn

import httpx
import pytest

from app.providers.discovery import SMARTDiscoveryError

# 408 and 429 are the server asking to be left alone; 5xx is it admitting it
# cannot answer. Everything else it can say is an answer, and therefore news.
_ASKED_TO_WAIT = frozenset({408, 429})

# For a caller that formats the status into its message and chains nothing.
_STATUS_IN_MESSAGE = re.compile(r"returned (\d{3})")


def is_outage(status: int) -> bool:
    """Whether ``status`` means "ask again later" rather than "this is different"."""
    return status >= 500 or status in _ASKED_TO_WAIT


def status_reported_by(exc: BaseException) -> int | None:
    """The HTTP status an exception names in its own message, if it names one.

    A last resort, for a raiser that formats the status into text and chains
    nothing, leaving no structured cause to read. Prefer ``reaching`` wherever
    the exception carries what caused it.
    """
    found = _STATUS_IN_MESSAGE.search(str(exc))
    return int(found.group(1)) if found else None


def outage(name: str, reason: str) -> NoReturn:
    """Skip, because ``name`` never really answered.

    One sentence in one shape, so an outage stays recognizable at a glance in a
    run that also has real failures in it.
    """
    pytest.skip(
        f"{name} is not answering right now ({reason}). An outage, not a change."
    )


def served(name: str, response: httpx.Response) -> httpx.Response:
    """The response, unless the server was too unwell to give a real one.

    Returns a refusal (401, 403) rather than skipping on it. Something answered,
    so whether that refusal is news is the caller's to judge: a vendor this
    backend cannot authorize against is free to turn anonymous readers away,
    while an issuer it holds credentials for is not.
    """
    if is_outage(response.status_code):
        outage(name, f"HTTP {response.status_code}")
    return response


@contextmanager
def reaching(name: str) -> Iterator[None]:
    """Turn a failure to reach ``name`` into a skip, and let everything else fail.

    Covers the raw httpx errors and the typed ones the discovery service raises.
    Those chain the httpx failure that caused them, so the cause still says
    whether anything was ever received, which is the whole question here.

    Letting the rest through is the interesting half. A 404 leaves untouched, and
    so does the parse error a server earns by publishing something new and wrong,
    because in both cases something came back and it was not what we read before.
    A refusal leaves too, which is the one place this differs from ``served``.
    Only an exception carrying a status reaches that branch, and in this suite
    that means one the discovery service raised about an issuer this backend
    authorizes against, where being turned away is a change worth failing over.
    A caller holding a raw response judges its own refusals through ``served``.
    """
    try:
        yield
    except (SMARTDiscoveryError, httpx.HTTPError) as exc:
        cause = exc if isinstance(exc, httpx.HTTPError) else exc.__cause__
        if isinstance(cause, httpx.HTTPStatusError):
            served(name, cause.response)
        elif isinstance(cause, httpx.RequestError):
            outage(name, f"{type(cause).__name__}: {cause}")
        raise
