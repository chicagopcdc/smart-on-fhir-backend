"""Keeping a stored connection's access token usable.

An access token from a SMART server lasts about an hour. The refresh token
stored beside it lasts days, and everything here exists to turn the second into
the first without a caller having to notice, so a connection authorized on
Monday still reads on Thursday.

A refresh is only ever attempted where the row already holds a refresh token,
which is to say where the server granted ``offline_access``. A connection
without one lapses with its access token and there is nothing to be done for it
but authorize again.

References: SMART App Launch (refreshing an access token), RFC 6749 §6 and §5.2.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from weakref import WeakValueDictionary

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ProviderToken, expiry_from, utcnow
from app.core import db
from app.core.config import get_settings
from app.providers import registry
from app.providers.discovery import SMARTDiscoveryError
from app.providers.generic import SMARTProviderError, TokenExchangeError
from app.providers.models import TokenSet

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveToken:
    """An access token that is good to send, and what it is good for.

    Returned rather than written back onto the caller's row: a refresh commits
    in a session of its own, so the instance a request is holding still shows
    the token that has just been replaced.

    ``scope`` travels with it for that same reason. A refresh may come back
    granting less than the one before it, and a caller deciding what to read from
    the scope on its own instance would be reading the wider one that has just
    been superseded — asking for types this token no longer covers, and reporting
    the refusals as the provider's fault.
    """

    access_token: str
    expires_at: datetime | None
    scope: str | None = None

    @classmethod
    def of(cls, connection: ProviderToken) -> "LiveToken":
        """What a connection currently holds, read out while its row is loaded."""
        return cls(connection.access_token, connection.expires_at, connection.scope)


class TokenRefreshError(Exception):
    """A stored token has lapsed and could not be replaced."""


class ReauthorizationRequired(TokenRefreshError):
    """The authorization behind this connection is finished.

    The patient revoked it, or the refresh token outlived its own lifetime.
    Nothing on this side recovers it; only authorizing the provider again does.
    """


class RefreshUnavailable(TokenRefreshError):
    """The refresh could not be carried out, for a reason that may well pass.

    The token endpoint was unreachable or answered an error that says nothing
    about the grant, discovery failed, or this deployment no longer holds a
    registration for the provider. The authorization is very likely intact — we
    could not ask.
    """


# Coalesces concurrent refreshes of one connection, so two reads arriving on the
# same expired token spend its refresh token once between them rather than each.
# The map is weak so an entry lives only while someone is inside a refresh,
# which is what keeps it from accumulating a lock per connection ever seen.
#
# Within one process only, like the rate limiter. Two workers can still each
# decide a token is due and both present it, and while the compare-and-set below
# keeps the row coherent afterwards, nothing here stops the provider seeing the
# same refresh token twice — which a server that treats reuse as theft answers
# by revoking the grant. A deployment running more than one worker wants this on
# a lock the workers share, such as a Postgres advisory lock on the connection id.
_refreshes: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def _lock_for(connection_id: int) -> asyncio.Lock:
    # setdefault rather than get-then-set: neither can be interrupted by the
    # event loop as written, but only setdefault stays correct if a suspension
    # point is ever introduced between the two halves.
    return _refreshes.setdefault(connection_id, asyncio.Lock())


def _is_due(expires_at: datetime | None, leeway: int) -> bool:
    """Whether a read should replace this token before sending it.

    A token with no stated expiry is left alone. The server never said when it
    stops working, and refreshing on every read to find out would spend the
    refresh token far faster than the access token is actually being used up.
    """
    if expires_at is None:
        return False
    return expires_at - timedelta(seconds=leeway) <= utcnow()


async def live_token(connection: ProviderToken) -> LiveToken:
    """The access token to read this connection with, refreshed where it is due.

    Raises ``ReauthorizationRequired`` where the provider has said the grant is
    finished, and ``RefreshUnavailable`` where a token that has genuinely lapsed
    could not be replaced. A token merely inside the leeway window whose refresh
    did not come off comes back unchanged: it still works, and a head start that
    failed is no reason to fail a read that would have succeeded.
    """
    stored = LiveToken.of(connection)
    leeway = get_settings().token_refresh_leeway_seconds

    if not _refresh_due(connection, leeway):
        return stored

    async with _lock_for(connection.id):
        try:
            return await _refresh(connection.id, leeway)
        except RefreshUnavailable:
            if _is_due(connection.expires_at, leeway=0):
                raise
            return stored


async def _refresh(connection_id: int, leeway: int) -> LiveToken:
    """Replace one connection's token set, in sessions of its own.

    Separate from the request's session for two reasons. A rotated refresh token
    is spent the instant the provider answers, so the replacement has to be
    committed on its own rather than ride out a request that may still fail —
    losing it would leave the connection holding a token the provider has already
    retired. And the connections of one record are read concurrently, which a
    single session cannot serve.
    """
    async with db.SessionFactory() as session:
        connection = await session.get(ProviderToken, connection_id)
        if connection is None:
            raise RefreshUnavailable("the connection no longer exists")
        # Whoever held the lock before us may already have done this.
        if not _refresh_due(connection, leeway):
            return LiveToken.of(connection)

        spent = connection.refresh_token
        provider = connection.provider
        iss = connection.iss
        patient_fhir_id = connection.patient_fhir_id

    try:
        adapter = registry.for_connection(provider, iss)
        config = await adapter.discover(iss)
        token_set = await adapter.refresh_token(config, spent)
    except TokenExchangeError as exc:
        if not exc.grant_is_gone:
            raise RefreshUnavailable(provider) from exc
        superseded = await _forget_refresh_token(connection_id, spent)
        if superseded is not None:
            # The token this refusal was about is no longer the connection's, so
            # the refusal says nothing about the connection.
            return superseded
        logger.info("Refresh refused by %s; the connection needs re-authorizing", provider)
        raise ReauthorizationRequired(provider) from exc
    except (
        registry.ProviderNotConfigured,
        SMARTDiscoveryError,
        SMARTProviderError,
        httpx.HTTPError,
    ) as exc:
        raise RefreshUnavailable(provider) from exc

    # A refresh must never repoint a connection at a different chart. No server
    # does this, which is precisely why one that did would go unnoticed.
    if token_set.patient and token_set.patient != patient_fhir_id:
        logger.error("Refresh by %s answered with a different patient context", provider)
        raise ReauthorizationRequired(provider)

    return await _store(connection_id, spent, token_set)


def _refresh_due(connection: ProviderToken, leeway: int) -> bool:
    """Whether this connection needs renewing and has the means to be renewed.

    Presence is asked of the column, not the property: reading the property would
    decrypt a refresh token only to compare it against None, and would fail a
    connection whose access token is perfectly usable because the refresh token
    beside it happens to predate a key rotation.
    """
    return (
        _is_due(connection.expires_at, leeway)
        and connection.encrypted_refresh_token is not None
    )


def _replaced_since(connection: ProviderToken, spent: str) -> bool:
    """Whether another refresh has already put a different token on this row.

    Only a different token that is actually *there* counts. An empty column is
    the absence of a replacement rather than evidence of one — a sibling the
    provider refused clears it — and the two mean opposite things to whoever
    reads them next, so the distinction lives here rather than at each caller.

    Presence is read off the column and the comparison off the value: Fernet
    ciphertext carries a nonce, so the same token encrypts differently every time
    and two ciphertexts can never be compared to each other.
    """
    if connection.encrypted_refresh_token is None:
        return False
    return connection.refresh_token != spent


async def _store(connection_id: int, spent: str, token_set: TokenSet) -> LiveToken:
    """Save the new token set, unless another refresh reached the row first."""
    async with db.SessionFactory() as session:
        connection = await _held(session, connection_id)
        if connection is None:
            raise RefreshUnavailable("the connection no longer exists")
        if _replaced_since(connection, spent):
            # Something has already rotated this connection since we read it.
            # Ours is the older answer, so theirs is the one that survives.
            return LiveToken.of(connection)

        connection.access_token = token_set.access_token
        # A server may omit either of these on a refresh, meaning "unchanged" —
        # scope may only ever narrow, never widen, so what came back stands.
        if token_set.refresh_token is not None:
            connection.refresh_token = token_set.refresh_token
        if token_set.scope is not None:
            connection.scope = token_set.scope
        connection.expires_at = expiry_from(token_set.expires_in)

        # Read out before committing rather than after. A session that expires
        # its instances on commit would turn the attribute access into a lazy
        # load, which in async code raises rather than quietly re-querying.
        renewed = LiveToken.of(connection)
        await session.commit()
        return renewed


async def _forget_refresh_token(connection_id: int, spent: str) -> LiveToken | None:
    """Drop a refresh token the provider has just refused.

    Returns whatever another refresh stored while ours was in flight, and None
    where no replacement had arrived — including where the column was already
    empty, which means a sibling was refused over the same token rather than
    that anything succeeded. A refusal speaks for the token that was presented
    and for nothing else, so one that has since been replaced must neither
    overwrite the replacement nor retire the connection.

    The connection itself stays either way. A live session may still hold this
    record, and the caller has to be able to authorize back into it; removing
    the row here would take the whole record with it wherever this was its only
    connection. So it remains, visibly needing re-authorization, holding nothing
    that could be spent again.
    """
    async with db.SessionFactory() as session:
        connection = await _held(session, connection_id)
        if connection is None:
            return None
        if _replaced_since(connection, spent):
            return LiveToken.of(connection)
        if connection.encrypted_refresh_token is not None:
            connection.refresh_token = None
            await session.commit()
        return None


async def _held(session: AsyncSession, connection_id: int) -> ProviderToken | None:
    """The connection, held against a concurrent write for this transaction.

    ``FOR UPDATE`` is what makes comparing the stored refresh token against the
    one we spent, and then writing, a single decision. It has to be compared in
    Python rather than in the ``WHERE`` clause because the column is encrypted
    and Fernet ciphertext is not deterministic, so the same token enciphers
    differently every time and no equality test in SQL could ever match.

    SQLite renders no row lock at all and serializes writers wholesale instead,
    which is why the offline suite still behaves.
    """
    result = await session.execute(
        select(ProviderToken).where(ProviderToken.id == connection_id).with_for_update()
    )
    return result.scalar_one_or_none()
