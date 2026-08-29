"""What a grant actually allows, when it came back narrower than we asked.

An authorization server is free to grant less than was requested — RFC 6749 §3.3
says so outright, and a consent screen that lets a patient tick resource types is
the ordinary way it happens. The granted scope has been stored on every
connection since tokens were first persisted, and nothing has ever read it.

Unread, a narrowing surfaces as one 403 per withheld resource type on every read,
forever, with nothing anywhere saying why. Read, it is a connection that works
for part of the record, which is a thing this API can already describe.

Two scope grammars are in play and both are live. SMART v1 spells the access
``patient/Condition.read``; v2 spells it ``patient/Condition.rs`` — a subset of
``cruds`` — and may hang a search filter off it. A server picks one, so this
understands both rather than assuming the one our own requests happen to use.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.providers.config import fhir_type_for

# One SMART scope: a context, a resource type (or *), and what may be done with
# it. Only patient- and user-level scopes reach a patient's record; launch/…,
# openid, profile and offline_access are about the session rather than the data,
# and simply do not match.
_SCOPE = re.compile(r"\A(?:patient|user)/(?P<type>[A-Za-z]+|\*)\.(?P<access>[A-Za-z*]+)\Z")

# v1 spells the whole verb out. "write" is why the v2 letters cannot simply be
# searched for a literal "r": it contains one.
_V1_READS = {"read", "*"}
_V1_ACCESS = {"read", "write", "*"}


def _reads(access: str) -> bool:
    """Whether this access string allows reading, in either scope grammar."""
    if access in _V1_ACCESS:
        return access in _V1_READS
    return "r" in access or "s" in access


def granted_types(scope: str | None) -> frozenset[str] | None:
    """The resource types this grant allows reading, or None for "no restriction".

    None means the grant places no limit we can act on, and covers three cases
    that all have to behave identically to how this application behaved before it
    read scopes at all:

    * the server returned no scope, which RFC 6749 §5.1 defines as having granted
      what was asked for;
    * the grant includes a readable wildcard, so every type is allowed;
    * the grant names no resource scope at all, which is a server describing the
      session rather than the data, not one that withheld the whole record.

    That last case is the important one. Treating "nothing recognizable" as
    "nothing permitted" would turn one unfamiliar spelling into a record that
    reads as empty, which is the worst possible way to be wrong here.
    """
    if not scope:
        return None

    types = set()
    for entry in scope.split():
        # v2 permits a search filter on the scope; the type and access are the
        # part before it.
        match = _SCOPE.match(entry.split("?", 1)[0])
        if not match or not _reads(match["access"]):
            continue
        if match["type"] == "*":
            return None
        types.add(match["type"])

    return frozenset(types) or None


def unreadable(fhir_types: Iterable[str], scope: str | None) -> list[str]:
    """Which of these FHIR types the grant does not allow reading, sorted."""
    granted = granted_types(scope)
    if granted is None:
        return []
    return sorted(set(fhir_types) - granted)


def partition(resource_types: dict, scope: str | None) -> tuple[dict, dict]:
    """Split fetch config rows into the ones this grant covers and the ones it does not.

    Split by FHIR type rather than by row name: the Observation searches are one
    type divided by category on our side, and a single ``Observation.read`` covers
    both of them.
    """
    granted = granted_types(scope)
    if granted is None:
        return resource_types, {}

    readable, withheld = {}, {}
    for name, entry in resource_types.items():
        target = readable if fhir_type_for(entry) in granted else withheld
        target[name] = entry
    return readable, withheld
