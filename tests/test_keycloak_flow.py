"""Signing in against Keycloak, which is an OIDC provider like any other.

There is no Keycloak adapter and these tests exist to keep it that way. The
login layer reads whatever the issuer publishes, so pointing it at a realm ought
to be configuration and nothing else — but "ought to" is the part worth
checking, because Keycloak differs from Okta in three places that reach this
code, and two of them fail silently:

  * **The issuer is a realm, not a host.** One Keycloak serves many, so
    ``/realms/<realm>`` is load-bearing: two realms on one server are two
    issuers, and a discovery document from the wrong one has the right host.
  * **Its key set carries an encryption key beside the signing one**, listed
    first, with an ``alg`` no signature check can use. Anything that took the
    first RSA key it found, or gave up on a set holding a key it could not
    build, would fail here and nowhere else.
  * **An account with no first or last name has no ``name`` claim.** Only
    ``preferred_username``, which is why the display name falls back to it.

The realm driven here is the one in ``keycloak/realm-smartfhir.json`` and the
document it publishes is a capture of what Keycloak 26.7 really served for it,
so what these assert against is that server's shape rather than a guess at it.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import respx
from sqlalchemy import select

from app.auth import oidc
from app.auth.models import LoginState, User
from app.core.config import get_settings
from tests import idp
from tests.app_harness import app_db, bearer, client

DB = "sqlite+aiosqlite:///{path}/db.sqlite"


def refusal_message(response):
    """The sentence out of a refused response, under either error envelope.

    This file arrives alongside the standard error envelope, which replaces
    FastAPI's ``{"detail": ...}`` with ``{"error": {"code": ..., "message": ...}}``.
    The two changes touch different files, so git merges them without reporting a
    conflict and the result fails right here — the shape moved under assertions
    that had named the old one.

    Reading whichever is present is what lets both land in either order, with no
    fixup commit on whichever goes second. It is deliberately narrow: it tolerates
    two known shapes and raises on anything else, so a response that is neither
    still fails rather than quietly returning None.

    Delete it once the envelope has landed. The ``detail`` arm is dead at that
    point, and an assertion on ``error["code"]`` is the better test anyway —
    the code is the half that is not free to be reworded.
    """
    body = response.json()
    error = body.get("error")
    if isinstance(error, dict) and "message" in error:
        return error["message"]
    return body["detail"]


@pytest.fixture
def keycloak(monkeypatch):
    """Point the application at the Keycloak realm for the length of one test.

    The suite is configured for the Okta-shaped provider in ``tests/idp.py``;
    this swaps the three settings a deployment would swap and nothing else,
    which is the whole claim these tests are making.
    """
    monkeypatch.setenv("OIDC_ISSUER", idp.KEYCLOAK_ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", idp.KEYCLOAK_CLIENT_ID)
    monkeypatch.setenv("OIDC_CLIENT_SECRET", idp.KEYCLOAK_CLIENT_SECRET)
    get_settings.cache_clear()
    oidc.reset_client()
    try:
        yield
    finally:
        get_settings.cache_clear()
        oidc.reset_client()


async def sign_in(http, factory, monkeypatch, **token_kwargs) -> dict:
    """Run a whole sign-in against the realm and return the callback body.

    The counterpart of ``test_login_flow.sign_in``, differing only in which
    provider answers — which is the comparison the module is here to make.
    """
    idp.mock_keycloak_discovery()
    started = await http.post("/auth/login")
    assert started.status_code == 200, started.text
    state = started.json()["state"]

    async with factory() as session:
        row = await session.get(LoginState, state)
        assert row is not None
        nonce = row.nonce

    idp.mock_keycloak_token_endpoint(
        idp.token_response(idp.keycloak_id_token(nonce=nonce, **token_kwargs))
    )
    # The realm's key set, not the single-key one the other provider publishes.
    with idp.serving_keys(monkeypatch, idp.keycloak_jwks()):
        response = await http.post(
            "/auth/login/callback", json={"code": "test-code", "state": state}
        )
    return response


# --- the realm as the issuer --------------------------------------------------


@respx.mock
async def test_the_authorization_url_is_built_from_what_the_realm_publishes(
    tmp_path, keycloak
):
    """Every endpoint comes off the discovery document, including the realm path."""
    async with app_db(DB.format(path=tmp_path)):
        async with client() as http:
            idp.mock_keycloak_discovery()
            response = await http.post("/auth/login")

    assert response.status_code == 200, response.text
    url = urlparse(response.json()["authorizationUrl"])
    query = parse_qs(url.query)

    published = idp.keycloak_discovery_document()
    assert f"{url.scheme}://{url.netloc}{url.path}" == published["authorization_endpoint"]
    assert url.path == "/realms/smartfhir/protocol/openid-connect/auth"
    assert query["client_id"] == [idp.KEYCLOAK_CLIENT_ID]
    assert query["redirect_uri"] == ["http://localhost:3000/auth/login/callback"]
    # The realm registers the client with a required S256 challenge method, so a
    # request without one is refused outright rather than downgraded.
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"]


@respx.mock
async def test_a_document_from_another_realm_on_the_same_server_is_refused(
    tmp_path, keycloak
):
    """Same host, different realm, different issuer — and not this one.

    Worth its own case rather than leaning on the general issuer check: with
    Okta the wrong document comes from a visibly wrong host, while two Keycloak
    realms differ by one path segment on a server this deployment does trust.
    """
    other_realm = idp.KEYCLOAK_ISSUER.replace("/smartfhir", "/staff")
    idp.mock_keycloak_discovery(
        idp.keycloak_discovery_document(issuer=other_realm)
    )

    async with app_db(DB.format(path=tmp_path)):
        async with client() as http:
            response = await http.post("/auth/login")

    assert response.status_code == 502
    assert "identity provider" in refusal_message(response)


# --- completing a sign-in -----------------------------------------------------


@respx.mock
async def test_a_realm_signs_a_user_in_and_the_account_is_keyed_to_it(
    tmp_path, monkeypatch, keycloak
):
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            response = await sign_in(http, factory, monkeypatch)

        async with factory() as session:
            users = (await session.execute(select(User))).scalars().all()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == "avery@example.org"
    assert body["user"]["displayName"] == "Avery Diaz"
    assert body["user"]["patientId"] is None

    # Identity is (issuer, subject), and for Keycloak the issuer is the realm.
    # An account made by the staff realm on the same server is a different
    # account, which is only true if the realm path is part of what is stored.
    (user,) = users
    assert user.issuer == idp.KEYCLOAK_ISSUER
    assert user.subject == "f4bed77d-e6d4-47f4-9b08-39c1621ee421"


@respx.mock
async def test_the_signing_key_is_found_past_the_encryption_key_beside_it(
    tmp_path, monkeypatch, keycloak
):
    """A key set whose first entry cannot verify anything is still usable.

    Keycloak publishes an RSA-OAEP encryption key at the same ``jwks_uri`` as
    the signing key and lists it first. It is an RSA key, so it looks like a
    candidate, and it is not one — the sign-in below only completes because the
    set is searched by ``kid`` and the unusable entry is skipped.
    """
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            response = await sign_in(http, factory, monkeypatch)

    key_set = idp.keycloak_jwks()
    assert key_set["keys"][0]["alg"] == "RSA-OAEP"
    assert response.status_code == 200, response.text


@respx.mock
async def test_skipping_that_key_does_not_soften_what_the_set_is_for(
    tmp_path, monkeypatch, keycloak
):
    """A token signed by a key the realm does not publish is still refused.

    The other half of the case above: tolerating an entry that cannot verify
    must not become tolerating a token nothing verified.
    """
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            response = await sign_in(
                http,
                factory,
                monkeypatch,
                key=idp.impostor_key(),
                kid=idp.IMPOSTOR_KEY_ID,
            )

        async with factory() as session:
            users = (await session.execute(select(User))).scalars().all()

    assert response.status_code == 400
    assert refusal_message(response) == "Sign-in could not be verified"
    assert users == []


@respx.mock
async def test_an_account_with_no_name_on_it_is_shown_its_username(
    tmp_path, monkeypatch, keycloak
):
    """Keycloak omits ``name`` where the account has no first or last name.

    Its user profile can be configured not to require either, and an account
    arriving from LDAP or a brokered identity often has neither, so this is an
    ordinary account rather than a broken one. ``preferred_username`` is always
    there, and a name in the corner of the page beats a blank.
    """
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            response = await sign_in(http, factory, monkeypatch, name=None)

    assert response.status_code == 200, response.text
    assert response.json()["user"]["displayName"] == "avery"


@respx.mock
async def test_an_account_with_neither_is_named_by_nothing_rather_than_by_a_guess(
    tmp_path, monkeypatch, keycloak
):
    """No name and no username is null, not an email and not an empty string."""
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            response = await sign_in(
                http, factory, monkeypatch, name=None, preferred_username=None
            )

    assert response.status_code == 200, response.text
    assert response.json()["user"]["displayName"] is None
    assert response.json()["user"]["email"] == "avery@example.org"


# --- signing out --------------------------------------------------------------


@respx.mock
async def test_sign_out_names_the_client_keycloak_will_not_redirect_without(
    tmp_path, monkeypatch, keycloak
):
    """Keycloak refuses a post-logout redirect it cannot attribute to a client.

    Since Keycloak 18 the end-session endpoint takes ``post_logout_redirect_uri``
    only alongside an ``id_token_hint`` or a ``client_id``, and answers "Invalid
    redirect uri" with neither. Nothing here keeps an ID token — it is read once
    for the subject inside it and discarded — so ``client_id`` is what must be
    sent, every time.
    """
    async with app_db(DB.format(path=tmp_path)) as factory:
        async with client() as http:
            session_id = (await sign_in(http, factory, monkeypatch)).json()["sessionId"]
            response = await http.post("/auth/logout", headers=bearer(session_id))

    assert response.status_code == 200, response.text
    logout = urlparse(response.json()["logoutUrl"])
    query = parse_qs(logout.query)

    published = idp.keycloak_discovery_document()["end_session_endpoint"]
    assert f"{logout.scheme}://{logout.netloc}{logout.path}" == published
    assert query["client_id"] == [idp.KEYCLOAK_CLIENT_ID]
    # The realm has to register this separately: Keycloak defaults a client's
    # post-logout redirects to its sign-in ones, which this is not.
    assert query["post_logout_redirect_uri"] == ["http://localhost:3000"]
