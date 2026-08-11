"""Deciding whether a caller-supplied issuer is one we are willing to fetch.

Discovery normally runs against an issuer that has already been checked against a
provider's allowlist (``app/api/auth.py``), so the set of hosts it can reach is
fixed by configuration. An endpoint that checks an arbitrary issuer on request has
no such bound: whatever URL arrives is a URL this process will connect to. That
makes it a server-side request forgery surface, and the guard below is what
narrows it back down.

The check lives here rather than inside :class:`~app.providers.discovery.SMARTDiscovery`
on purpose. Discovery is also used by the authorization flow, whose issuers come
from the allowlist and legitimately include a FHIR server on localhost during
development. Refusing private addresses there would break running the stack
locally while adding nothing, because the allowlist already answers the question.

What this does not close: the address checked here and the address httpx
eventually connects to come from two separate resolutions, so a name that answers
differently between them is not caught. Closing that needs a transport that
connects to an already-resolved address while still presenting the original host
for TLS, which is more machinery than this surface warrants. Redirects are never
followed, so a redirect into private space reads as an unreachable server rather
than becoming a second, unchecked fetch.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_CARRIER_GRADE_NAT = ipaddress.ip_network("100.64.0.0/10")


class UnsafeTarget(ValueError):
    """The issuer is one we refuse to fetch, whatever is behind it.

    These messages are shown to whoever asked, so they say what the rule is and
    never quote the URL back. Echoing caller input into a response body is how a
    refusal becomes a way to get text of one's choosing out of this API.
    """


class UnresolvedTarget(ValueError):
    """The issuer is well-formed but its host does not resolve."""


async def ensure_fetchable(raw: str) -> str:
    """Return ``raw`` normalized, or raise if we should not connect to it.

    Raises :class:`UnsafeTarget` for an issuer that is malformed or points
    somewhere off-limits, and :class:`UnresolvedTarget` when the host simply does
    not exist. Callers want those apart: the first is a bad request, the second is
    an honest answer about a server that has gone away.
    """
    candidate = raw.strip()
    try:
        parts = urlsplit(candidate)
        # hostname and port are both parsed on access and raise on malformed input
        # — an unbalanced IPv6 bracket, a port outside 1-65535. Reading them inside
        # the try is what turns either into a refusal rather than an unhandled error
        # somewhere further down. The port's value is not wanted, only its parsing.
        host = parts.hostname
        _ = parts.port
    except ValueError as exc:
        raise UnsafeTarget("An issuer must be a well-formed URL") from exc

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeTarget("An issuer must be an http or https URL")

    # Credentials in the URL would be sent to whatever host follows the @, and a
    # reader skimming a log line would attribute the request to the wrong one.
    if parts.username or parts.password:
        raise UnsafeTarget("An issuer must not carry credentials")

    if not host:
        raise UnsafeTarget("An issuer must name a host")

    for address in await _addresses(host):
        if not _is_public(address):
            raise UnsafeTarget("An issuer must resolve to a public address")

    return candidate.rstrip("/")


def _is_public(address: str) -> bool:
    """Whether ``address`` is one we are willing to open a connection to.

    Refused are the ranges that only mean something from inside whichever network
    this process happens to run in: loopback, the RFC 1918 blocks, the RFC 6598
    carrier-grade NAT space, and the link-local range that carries cloud instance
    metadata.

    Spelled out predicate by predicate rather than deferring to ``is_global``,
    which is a single flag whose membership has been corrected more than once; the
    predicates below name what is actually being refused and read the same on every
    version. The two explicit cases are both ranges the stock predicates have
    missed: carrier-grade NAT is not ``is_private``, and an IPv4 address written in
    IPv6 form was not classified by its underlying address until a 3.10-era
    security fix, so ``[::ffff:10.0.0.1]`` could otherwise read as public.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv4Address) and ip in _CARRIER_GRADE_NAT:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_public(str(ip.ipv4_mapped))

    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _addresses(host: str) -> list[str]:
    """Every address ``host`` stands for. A literal one needs no lookup."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return await _resolve(host)
    return [host]


async def _resolve(host: str) -> list[str]:
    """Resolve a name to its addresses.

    Its own function so a test can stand in for the name service. HTTP mocking
    intercepts the client, not the resolver, so without this seam every mocked test
    would need real DNS for whatever hostname its fixture uses.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnresolvedTarget(f"{host} does not resolve") from exc

    return [info[4][0] for info in infos]
