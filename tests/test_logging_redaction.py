"""Nothing a patient or a token owns reaches the log, proved by driving the app.

The rule this checks is in ``app/core/logging.py``: a log line may name what
happened and where, never whose it was. Reading the code is how that rule gets
believed; this is how it gets known. A whole record is authorized, read,
summarized and disconnected against captured responses, with the real handler
attached to a buffer, and then the buffer is searched for every identifier that
went through the flow.

Two things make it a real check rather than a reassuring one.

It listens on the **root logger at DEBUG**, which is louder than any deployment:
httpx logs a line per FHIR request at INFO and the application deliberately does
not raise the root floor to hear it, so a run at the shipped levels would prove
nothing about the records that floor is there to suppress. If those records leak,
they leak here.

And it asserts the buffer is not empty, then proves the search would notice. A
test that greps silence passes for the wrong reason, so ``test_the_search_would
_catch_a_leak`` writes each secret through a logger this application does not own
and requires the same assertions to fail.
"""

from __future__ import annotations

import json
import logging

import pytest
import respx

from app.core.logging import REDACTED, fields, redact
from tests.app_harness import (
    SMART_LAUNCHER as LAUNCHER,
    app_db,
    bearer,
    client,
    connect_and_serve,
    load_fixture,
)

# What must never appear, taken from the fixture the flow is driven against
# rather than restated, so a re-captured record cannot leave this checking for
# strings nobody serves any more.
_RECORD = load_fixture(LAUNCHER["record"])
_PATIENT = _RECORD["responses"]["Patient"]["body"]
_DEMOGRAPHICS = _PATIENT["entry"][0]["resource"] if "entry" in _PATIENT else _PATIENT

FHIR_PATIENT_ID = _RECORD["patientId"]
FAMILY_NAME = _DEMOGRAPHICS["name"][0]["family"]
GIVEN_NAME = _DEMOGRAPHICS["name"][0]["given"][0]
BIRTH_DATE = _DEMOGRAPHICS["birthDate"]
ACCESS_TOKEN = f"access-for-{FHIR_PATIENT_ID}"


@pytest.fixture
def written(log_capture):
    """Everything written while a flow runs, listened for harder than in production.

    DEBUG on the root is louder than any deployment, and deliberately so: it is
    what makes httpx write a line per FHIR request, URLs and their ``?patient=``
    and all. Those are the records the shipped levels are arranged to suppress,
    and therefore the ones a test at those levels would never see leak.
    """
    return log_capture(logging.DEBUG)


@pytest.fixture
def served_by_uvicorn(written):
    """The logging state a process actually serves in: uvicorn's, then ours.

    ``uvicorn.config.LOGGING_CONFIG`` is what a served process applies at
    startup, before the application's lifespan runs. Applying it here is what
    makes the test that follows a real check rather than an observation about
    pytest's defaults.
    """
    import logging.config

    import uvicorn.config

    from app.core.logging import configure_logging

    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    configure_logging()
    # log_capture puts uvicorn's loggers back as they were, so this need not.
    return written


def _forbidden(logged: str, *, session_id: str) -> None:
    """Everything this flow handled that a log line must not carry."""
    assert ACCESS_TOKEN not in logged, "an access token reached the log"
    assert f"refresh-for-{FHIR_PATIENT_ID}" not in logged, "a refresh token reached the log"
    assert session_id not in logged, "a session bearer reached the log"
    assert FHIR_PATIENT_ID not in logged, "a provider-issued patient id reached the log"
    assert FAMILY_NAME not in logged, "the patient's name reached the log"
    assert GIVEN_NAME not in logged, "the patient's name reached the log"
    assert BIRTH_DATE not in logged, "the patient's date of birth reached the log"


@respx.mock
async def test_a_whole_record_goes_through_without_reaching_the_log(written, tmp_path):
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'redaction.db'}"):
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER)
            session_id = connected["sessionId"]
            patient_id = connected["patientId"]
            auth = bearer(session_id)

            reads = await http.get(f"/patients/{patient_id}/resources", headers=auth)
            summary = await http.get(f"/patients/{patient_id}/summary", headers=auth)
            ended = await http.delete(
                f"/patients/{patient_id}/connections/{LAUNCHER['provider']}",
                headers=auth,
            )

    # The flow really ran: assertions over an empty buffer would pass whatever
    # the redaction did.
    assert reads.status_code == 200, reads.text
    assert summary.status_code == 200, summary.text
    assert ended.status_code == 200, ended.text
    assert FHIR_PATIENT_ID in reads.text, "the read did not carry the record it should"

    logged = written.getvalue()
    assert logged.strip(), "nothing was logged, so this asserts nothing"
    _forbidden(logged, session_id=session_id)


@respx.mock
async def test_the_search_would_catch_a_leak(written, tmp_path):
    """The same assertions, over a log that really does leak, must fail.

    Without this the test above is indistinguishable from one whose fixtures no
    longer match what the flow handles. Each secret is written through a logger
    outside ``app.*``, which is both the harder case — the application's own
    discipline does not apply to it — and the one the shipped configuration is
    quietest about.
    """
    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'leak.db'}"):
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER)

    stray = logging.getLogger("some.vendor.library")
    for secret in (ACCESS_TOKEN, FHIR_PATIENT_ID, FAMILY_NAME, GIVEN_NAME, BIRTH_DATE):
        stray.debug("a library wrote %s straight into a message", secret)

    with pytest.raises(AssertionError):
        _forbidden(written.getvalue(), session_id=connected["sessionId"])


@respx.mock
async def test_a_failed_read_names_the_provider_and_our_own_record_id(
    written, tmp_path, monkeypatch
):
    """What survives redaction still has to be enough to act on.

    A log that gives nothing away and also says nothing is no win, so this drives
    the one branch whose whole purpose is to leave a trace and reads what the real
    handler wrote. The failure is injected past the fetch layer because that is
    the only place it can come from: ``fetch_single_resource`` turns everything
    the HTTP call raises into a result dict, so what reaches ``_read_all`` is a
    normalization error — and pydantic quotes the value of the field that failed,
    which on a Patient is a name.
    """
    from app.api import patients

    async def unmodelled(access_token, base_url, fhir_patient_id, resource_types):
        raise ValueError(f"choked on {FAMILY_NAME}")

    async with app_db(f"sqlite+aiosqlite:///{tmp_path / 'named.db'}"):
        async with client() as http:
            connected, _ = await connect_and_serve(http, LAUNCHER)
            monkeypatch.setattr(patients.service, "fetch_fhir_resources", unmodelled)

            response = await http.get(
                f"/patients/{connected['patientId']}/resources",
                headers=bearer(connected["sessionId"]),
            )

    assert response.status_code == 200
    logged = written.getvalue()
    assert LAUNCHER["provider"] in logged, "the log does not say which connection failed"
    assert connected["patientId"] in logged, "the log does not say which record it was on"
    _forbidden(logged, session_id=connected["sessionId"])


# --- the redactor itself, over the shapes a credential actually takes ----------


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
        "authorization=bearer opaque-token-value",
        "id_token=eyJhbGciOiJSUzI1NiJ9.eyJwYXRpZW50IjoiOTk5In0.abc",
        "stored gAAAAABmZm90aGVyLWNpcGhlcnRleHQtZ29lcy1oZXJl",
        "GET https://fhir.example.org/Observation?patient=abc123&category=vital-signs",
        "POST /token?code=4/0Adeu5B&state=Yl8kQq",
    ],
)
def test_the_redactor_masks_a_credential_however_it_is_written(line):
    cleaned = redact(line)
    assert REDACTED in cleaned
    for secret in (
        "c2lnbmF0dXJl",
        "opaque-token-value",
        "eyJwYXRpZW50IjoiOTk5In0",
        "aGVyLWNpcGhlcnRleHQtZ29lcy1oZXJl",
        "abc123",
        "4/0Adeu5B",
    ):
        assert secret not in cleaned, f"{secret} survived redaction of {line!r}"


def test_a_url_keeps_the_part_worth_reading():
    """Dropping the query must not cost the line the endpoint it was talking to."""
    cleaned = redact("GET https://fhir.example.org/Condition?patient=abc123 -> 403")
    assert "https://fhir.example.org/Condition" in cleaned
    assert "403" in cleaned
    assert "abc123" not in cleaned


def test_a_structured_field_is_masked_by_its_name(written):
    """A key whose name says it is sensitive is masked whatever its value looks like."""
    logging.getLogger("app.test").error(
        "storing", **fields(provider="EPIC_SANDBOX", refresh_token="plain-looking-value")
    )

    logged = written.getvalue()
    assert "EPIC_SANDBOX" in logged
    assert "plain-looking-value" not in logged
    assert REDACTED in logged


def test_json_format_writes_one_object_per_line(json_log_capture):
    """The shipped JSON output has to parse, redaction and all."""
    written = json_log_capture()

    logging.getLogger("app.test.json").error(
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
        **fields(event="probe", provider="EPIC_SANDBOX"),
    )

    [line] = [line for line in written.getvalue().splitlines() if line.strip()]
    record = json.loads(line)
    assert record["event"] == "probe"
    assert record["provider"] == "EPIC_SANDBOX"
    assert record["level"] == "ERROR"
    assert "sig" not in record["message"]
    assert REDACTED in record["message"]


# --- one value is one line, whoever supplied the value -------------------------


def test_a_value_carrying_a_newline_cannot_forge_a_record(written):
    """A logged value is data, and a log line is a record. Keep them apart.

    The reachable vector is the reason on a failed exchange, which is the
    ``error`` code copied verbatim out of an authorization server's JSON body —
    so its content is the upstream server's to choose, not ours. A newline in it
    turns one record into two, the second indistinguishable from something this
    application wrote. The middleware already validates an inbound request id for
    exactly this reason; every other value needs the same treatment.
    """
    forged = "invalid_grant\n2026-01-01T00:00:00.000+00:00 ERROR [app.core.db] rows dropped"
    logging.getLogger("app.test.injection").warning(
        "upstream refused", **fields(event="auth.token_exchange.failed", reason=forged)
    )

    written_lines = [line for line in written.getvalue().splitlines() if line.strip()]
    assert len(written_lines) == 1, "a supplied value wrote a log record of its own"
    # Still legible: the value is kept, only its ability to end the line is not.
    assert "invalid_grant" in written_lines[0]
    assert "rows dropped" in written_lines[0]


def test_the_json_stream_survives_a_value_that_would_break_it(json_log_capture):
    """Every line has to parse, including the one an upstream server wrote into."""
    written = json_log_capture()

    logging.getLogger("app.test.jsoninjection").error(
        "upstream refused",
        **fields(reason='not_json"}\n{"level": "INFO", "message": "all clear'),
    )

    lines = [line for line in written.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "upstream refused"


def test_uvicorns_own_records_are_written_by_this_handler_too(served_by_uvicorn):
    """Otherwise the log is two formats interleaved, and half of it unredacted.

    uvicorn gives its loggers a handler each and turns propagation off, so
    without being taken over they write in their own format — which makes
    LOG_FORMAT=json a stream that is only mostly JSON, and leaves the access
    line's query string as the one thing written without passing through
    redaction.

    The fixture applies uvicorn's own logging configuration before the
    application's, because nothing else in this suite does: under pytest those
    loggers are untouched, so a check written against the default state would
    pass whether or not anything took them over. What is asserted here only
    means something because uvicorn has already claimed them.
    """
    written = served_by_uvicorn
    access = logging.getLogger("uvicorn.access")
    assert access.propagate, "uvicorn.access still bypasses the application's handler"
    assert not access.handlers, "uvicorn.access still writes through a handler of its own"

    # The shape uvicorn logs an access line in, with a credential in the query.
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "10.0.1.7:52344",
        "GET",
        "/auth/callback?code=4-0AdeuREAL&state=Yl8kQq",
        "1.1",
        307,
    )

    logged = written.getvalue()
    assert "10.0.1.7:52344" in logged, "the access line did not reach this handler"
    assert "4-0AdeuREAL" not in logged, "an authorization code reached the log"
    assert "Yl8kQq" not in logged, "an OAuth state reached the log"
