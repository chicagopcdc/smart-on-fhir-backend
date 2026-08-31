"""What this application writes to its log, and what it will not write.

This service holds provider access tokens and clinical data, so a log line is a
place either can escape to. Two rules stand between them and the log, and only
the second is a guarantee.

**The rule the code follows.** A log line names *what* happened and *where*,
never *whose* it was. Safe: exception class names, provider keys, issuer hosts,
resource type names, HTTP statuses, OAuth error codes, counts, durations, and
our own ``pat_…`` record ids, which this application mints and no server has
ever seen. Not safe: access and refresh tokens, ``Authorization`` values,
session ids, an OAuth ``state`` or ``code``, a ``patient_fhir_id``, and resource
content.

The one that looks safe and is not is an **exception message**. Pydantic quotes
the value of the field that failed, which on a Patient is a name or a date of
birth; httpx quotes the URL it was fetching, which on a FHIR search carries the
provider's patient id. So a failure is logged as its type and the frames it came
through, and ``str(exc)`` is not written at all. That is why the redactor below
has so little to do: the rule is what keeps sensitive values out, and it is
enforced at every call site rather than here.

**The guarantee.** A rule is a habit, and this application does not author every
record its handler receives — httpx, SQLAlchemy and uvicorn write through the
same one. So every value on its way into a line is redacted first: values under a
sensitive key name are masked, a URL's query string is dropped whole
(``?patient=`` is the standard FHIR search parameter, so no enumeration of
parameter names could be trusted to stay complete), and the shapes credentials
actually take — a bearer prefix, a JWT's three segments, Fernet's ``gAAAAA`` —
are masked wherever they appear.

What it cannot do is recognise a secret that was rendered into a message and
looks like nothing in particular — an opaque token, spelled out. Structure is
what the key rule needs, and a sentence has none. That case belongs to the first
rule, which is why the first rule is the one that matters.

That happens in the **formatter** rather than in a filter. A filter added to the
root logger never runs for a record a child logger propagated up, which is every
record this application writes; a formatter runs on everything the handler
actually writes, and what is actually written is what the guarantee is about.
Value by value rather than over the finished line, for a reason ``_clean``
records: masking a rendered JSON object ate its delimiters.

Redaction is the wrong instrument for one thing, and ``configure_logging`` uses
the other one. An identifier in a URL *path*, or bound as a SQL parameter, is
indistinguishable from anything else in the same position — there is no pattern
to write. Those loggers are silenced by level instead.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import datetime, timezone

from app.core.config import Settings, get_settings

REDACTED = "[redacted]"

# The id assigned to the request being served, so every line written while
# handling it can be gathered afterwards. A ContextVar rather than a parameter
# threaded through: logging is a side channel, and a signature that carried a
# correlation id through the fetch layer would put it in the way of the code
# that does the work.
request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Structured keys whose value is never safe to write, matched as substrings so
# that access_token, refresh_token and id_token are all covered by one entry.
_UNSAFE_KEYS = (
    "token",
    "secret",
    "password",
    "authorization",
    "bearer",
    "credential",
    "code_verifier",
    "session_id",
    "patient_fhir_id",
    "state",
)

# Shapes a credential takes in free text, whoever wrote the line.
_CREDENTIAL_PATTERNS = (
    # A bearer token, however the line spelled the scheme.
    re.compile(r"(?i)\b(bearer\s+)\S+"),
    # A JWT: three base64url segments, the first of which decodes to "{"alg".
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),
    # Fernet ciphertext, which is what a stored token looks like at rest.
    re.compile(r"\bgAAAAA[A-Za-z0-9_\-=]{16,}"),
    # A credential named in a query string, for a URL that reached a log without
    # its query being stripped (a relative one, say).
    re.compile(
        r"(?i)([?&](?:access_token|refresh_token|id_token|token|code|state|patient)=)"
        r"[^&\s\"']+"
    ),
)

# A URL's query string, dropped whole rather than filtered. A FHIR search puts
# the provider's patient id in ?patient=, and no list of parameter names to strip
# could be trusted to stay complete as servers add their own.
_URL_QUERY = re.compile(r"(https?://[^\s\"'<>]*?)\?[^\s\"'<>]*")

# Anything that could end a line or drive a terminal. A newline in a logged value
# forges a whole record — one line becomes two, and the second is indistinguishable
# from something this application wrote. That is not hypothetical here: the reason
# on an exchange failure is the ``error`` code copied out of an authorization
# server's JSON body, so its content is the upstream server's to choose.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def redact(text: str) -> str:
    """Mask anything in ``text`` shaped like a credential or a patient identifier."""
    text = _URL_QUERY.sub(rf"\1?{REDACTED}", text)
    for pattern in _CREDENTIAL_PATTERNS:
        # Patterns with a group keep it (the "Bearer " or the "?code="); the rest
        # are replaced whole.
        text = pattern.sub(
            lambda match: (match.group(1) if match.groups() else "") + REDACTED, text
        )
    return text


def fields(**pairs) -> dict:
    """Structured keys for one call: ``logger.info("...", **fields(provider=key))``.

    Written as a helper rather than as a literal ``extra={"fields": {...}}`` at
    each call site, so every structured record carries its keys in the same place
    and the formatters have one thing to look for.
    """
    return {"extra": {"fields": pairs}}


def _is_unsafe(key: str) -> bool:
    lowered = key.lower()
    return any(unsafe in lowered for unsafe in _UNSAFE_KEYS)


def _clean(value):
    """One value on its way into a line, redacted.

    Applied per value rather than to the finished line, which is not a stylistic
    choice: the credential patterns match runs of non-whitespace, and a rendered
    JSON object puts a closing quote and a comma inside such a run. Masking after
    rendering ate them, and produced a line that no longer parsed.

    Numbers and booleans are left as they are. They cannot carry a secret, and
    coercing them to text would cost a JSON consumer the types it reads.

    A mapping is walked rather than rendered, so that the key rule reaches a
    nested ``access_token`` as surely as a top-level one. Rendered first, it
    would be one string, and a token that happens to look like nothing in
    particular — an opaque one, as Epic issues — would have no shape left to
    match on.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_unsafe(str(key)) else _clean(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(item) for item in value]
    text = redact(value if isinstance(value, str) else str(value))
    # One value is one value, whatever it arrived containing.
    return _CONTROL.sub(" ", text)


def _payload(record: logging.LogRecord) -> dict:
    """One record as the keys that will be written, redacted, before rendering."""
    payload = {
        "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="milliseconds"
        ),
        "level": record.levelname,
        "logger": _clean(record.name),
        "message": _clean(record.getMessage()),
    }

    current = request_id.get()
    if current:
        payload["request_id"] = current

    for key, value in (getattr(record, "fields", None) or {}).items():
        payload[key] = REDACTED if _is_unsafe(key) else _clean(value)

    if record.exc_info:
        exc_type, _, tb = record.exc_info
        payload["exception"] = getattr(exc_type, "__name__", str(exc_type))
        # The frames, and deliberately not the message: see the module docstring.
        #
        # Walked rather than extracted. ``traceback.extract_tb`` stats and reads
        # every source file the traceback names, to attach each line's text to
        # the frame — text nothing here asks for. That is tens of microseconds
        # and a syscall per file, spent on the one path least able to afford it:
        # the catch-all, which a stopped database puts every request through at
        # once.
        payload["traceback"] = [
            _clean(f"{frame.f_code.co_filename}:{lineno} in {frame.f_code.co_name}")
            for frame, lineno in traceback.walk_tb(tb)
        ]
    return payload


class JSONFormatter(logging.Formatter):
    """One JSON object per line, for a deployment shipping logs somewhere."""

    def format(self, record: logging.LogRecord) -> str:
        # The payload is already redacted value by value, so this only has to
        # render it. default=str is the backstop for a field type json does not
        # know, which _clean has already turned into text anyway.
        return json.dumps(_payload(record), default=str)


class TextFormatter(logging.Formatter):
    """The line a developer reads, with any structured keys appended.

    The same prefix this application has always written, so a log someone is used
    to reading still reads the same way.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = _payload(record)
        # Taken out of the payload rather than skipped by name, so that whatever
        # is left is by definition the structured keys. A second list of the ones
        # already rendered would be a thing to keep in step, and a key added to
        # ``_payload`` would start appearing twice the day someone forgot.
        when = payload.pop("time")
        level = payload.pop("level")
        name = payload.pop("logger")
        message = payload.pop("message")
        frames = payload.pop("traceback", None)

        line = f"{when} {level} [{name}] {message}"
        if payload:
            line += " " + " ".join(f"{key}={value}" for key, value in payload.items())
        if frames:
            line = "\n".join([line, *(f"    {frame}" for frame in frames)])
        return line


def build_handler(settings: Settings | None = None) -> logging.Handler:
    """A handler that writes this application's records and redacts them.

    Exposed so a test can attach the real thing to a buffer and assert over what
    it wrote. Asserting redaction against a handler built for the occasion would
    only prove the test's own formatter is safe.
    """
    settings = settings or get_settings()
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter() if settings.log_format == "json" else TextFormatter())
    return handler


def configure_logging(settings: Settings | None = None) -> None:
    """Give this application's own log records somewhere to go, and only ours.

    uvicorn configures three loggers of its own and leaves the root logger alone,
    so anything logged under ``app.*`` falls back to the WARNING default and every
    INFO record the application writes is dropped — including the one reporting
    how many connections a sweep retired, which exists precisely so that number is
    visible rather than inferred.

    Lowering the level on ``app`` rather than on the root is the difference
    between hearing this application and hearing every library it uses. httpx in
    particular logs a line per request at INFO, and a single read fans out over a
    dozen FHIR calls whose URLs carry the provider's own patient id — so a
    root-level floor would write exactly the identifier the rest of this code goes
    out of its way to keep out of logs. Records still reach the root handler once
    made; it is the level on the originating logger that decides whether they are
    made at all.

    Some libraries are floored outright rather than left to inherit, because what
    they write is not redactable in principle. A database driver logs the
    statement it ran *and the parameters bound to it*, which on this schema are
    session ids, the provider's patient id and the stored ciphertext, as one
    opaque tuple no pattern could reliably pick apart. httpx logs a line per
    request, and this application's requests carry a provider's patient id in the
    path — ``/Patient/{id}`` — where it is indistinguishable from any other path
    segment. Dropping a query string is possible; recognising an identifier by
    position in a URL is not.

    So the level is where that is settled. An operator turning the root logger up
    to debug a problem must not thereby turn the store and the FHIR client into a
    firehose of the data this module exists to keep out of logs — and the choice
    must not depend on the root floor happening to stay where it is.

    uvicorn's own loggers have to be taken over rather than left alone. It gives
    ``uvicorn`` and ``uvicorn.access`` a handler each and sets ``propagate``
    false, so their records never reach the root handler: they would go on being
    written in uvicorn's format, which makes ``LOG_FORMAT=json`` a stream that is
    only mostly JSON, and leaves the access line — request path, query string and
    all — as the one thing here written without passing through redaction.
    Clearing those handlers and letting the records propagate puts every line
    back under one formatter.

    A root logger that already has a handler keeps it, so a deployment that
    configures its own logging is not overruled. That deployment takes on the
    redaction rule with it, which is why the handler this function installs is the
    one the README describes.
    """
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(build_handler(settings))
    logging.getLogger("app").setLevel(logging.INFO)
    for noisy in ("httpx", "sqlalchemy.engine", "aiosqlite", "asyncpg"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for served in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        adopted = logging.getLogger(served)
        adopted.handlers.clear()
        adopted.propagate = True
