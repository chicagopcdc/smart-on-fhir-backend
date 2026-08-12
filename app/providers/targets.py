"""Deciding whether a caller-supplied issuer is one we are willing to fetch.

Discovery normally runs against an issuer already checked against a provider's
allowlist (``app/api/auth.py``), so configuration fixes the set of hosts it can
reach. An endpoint that checks an arbitrary issuer has no such bound — whatever URL
arrives is one this process will connect to — which makes it a server-side request
forgery surface.

The guard lives here rather than in :class:`~app.providers.discovery.SMARTDiscovery`
because discovery also serves the authorization flow, whose allowlisted issuers
legitimately include a FHIR server on localhost during development.

The port is deliberately unrestricted: ONC's list carries 31 endpoints on
non-default ports, mostly 9443, and the oracle that allowing any port leaves —
whether something speaks HTTP on a public host — is one anyone can ask directly
and faster.

Not closed: the address checked here and the one httpx connects to come from two
separate resolutions, so a name that answers differently between them slips
through. Redirects are never followed, so a redirect into private space reads as an
unreachable server rather than a second, unchecked fetch.
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

    These messages reach whoever asked, so they state the rule and never quote the
    URL back: echoing caller input into a response is how a refusal becomes a way to
    get text of one's choosing out of this API.
    """


class UnresolvedTarget(ValueError):
    """The issuer is well-formed but its host does not resolve."""


async def ensure_fetchable(raw: str) -> str:
    """Return ``raw`` normalized, or raise if we should not connect to it.

    The two exceptions are kept apart because callers owe their users different
    answers: :class:`UnsafeTarget` is a bad request, :class:`UnresolvedTarget` an
    honest "gone".
    """
    candidate = raw.strip()
    try:
        parts = urlsplit(candidate)
        # Both raise on malformed input (an unbalanced IPv6 bracket, a port outside
        # 1-65535); reading them inside the try turns either into a refusal. The
        # port's value is unused — only its parsing.
        host = parts.hostname
        _ = parts.port
    except ValueError as exc:
        raise UnsafeTarget("An issuer must be a well-formed URL") from exc

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeTarget("An issuer must be an http or https URL")

    # Credentials would be sent to whatever host follows the @, which is also not the
    # host a reader skimming a log line would take the request to have gone to.
    if parts.username or parts.password:
        raise UnsafeTarget("An issuer must not carry credentials")

    if not host:
        raise UnsafeTarget("An issuer must name a host")

    # A FHIR base URL ends before any query or fragment, and the well-known path is
    # appended to this string: a query would swallow it, a fragment discard it, and
    # either would answer about a document that was never fetched.
    if "?" in candidate or "#" in candidate:
        raise UnsafeTarget("An issuer must not carry a query or fragment")

    for address in await _addresses(host):
        if not _is_public(address):
            raise UnsafeTarget("An issuer must resolve to a public address")

    return candidate.rstrip("/")


def _is_public(address: str) -> bool:
    """Whether ``address`` is one we are willing to open a connection to.

    Refused are the ranges that only mean something from inside whichever network
    this process runs in: loopback, RFC 1918, RFC 6598 carrier-grade NAT, and the
    link-local range that carries cloud instance metadata.

    Spelled out predicate by predicate rather than deferring to ``is_global``, a
    single flag whose membership has been corrected more than once. The two explicit
    cases are ranges the stock predicates miss: carrier-grade NAT is not
    ``is_private``, and an IPv4 address written in IPv6 form was not classified by
    its underlying address until a 3.10-era security fix, so ``[::ffff:10.0.0.1]``
    could otherwise read as public.
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
    except UnicodeError as exc:
        # A name the resolver will not even encode: the IDNA codec rejects a label
        # over 63 characters before any lookup happens. Caught here because it is
        # neither a lookup failure nor a subclass of one, and letting it out means
        # an unhandled error — which is the one way a response loses its CORS
        # headers, since nothing above this is in the middleware stack.
        raise UnsafeTarget("An issuer must name a host") from exc

    return [info[4][0] for info in infos]
