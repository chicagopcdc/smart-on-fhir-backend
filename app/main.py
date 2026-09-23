"""The application: middleware, lifespan, and the routers that make up the API."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import auth, deps, errors, health, login, patients, providers
from app.api.middleware import REQUEST_ID_HEADER, RequestContext
from app.core.config import get_settings
from app.core.db import engine
from app.core.logging import configure_logging
from app.fhir import normalize

DESCRIPTION = """
A SMART on FHIR backend for connecting to electronic health record systems and
serving what they hold about a patient in one consistent shape.

Authorization follows the [SMART App Launch](https://hl7.org/fhir/smart-app-launch/)
flow. Each server's endpoints and capabilities are read from its
`.well-known/smart-configuration` at runtime rather than hardcoded, so any
spec-compliant server works through the same path.

Responses are normalized against [US Core](https://hl7.org/fhir/us/core/), the
profile set a certified server must support, so a resource type has the same
shape whichever vendor it came from. The full resource travels alongside each
summary, so nothing the server sent is lost.
"""

TAGS = [
    {
        "name": "authentication",
        "description": "Sign a user in and out, and report who a session belongs to.",
    },
    {
        "name": "authorization",
        "description": "Connect a patient to an EHR and exchange the result for a session.",
    },
    {
        "name": "patients",
        "description": "Read a connected patient's normalized record.",
    },
    {
        "name": "providers",
        "description": "Find a server to connect to.",
    },
    {
        "name": "health",
        "description": "Whether the service and its database are up.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Build the FHIR model classes now so the first read is not the one that pays
    # for it on the event loop.
    normalize.warm_model_cache()
    yield
    await engine.dispose()


app = FastAPI(
    title="SMART on FHIR Backend",
    description=DESCRIPTION,
    version="0.1.0",
    openapi_tags=TAGS,
    lifespan=lifespan,
)

app.state.limiter = deps.limiter
app.add_exception_handler(RateLimitExceeded, deps.rate_limit_exceeded_handler)

# Every refusal answers the one envelope, whoever decided on it: a route, a
# dependency, the router itself for a path that does not exist, or the request
# validator. Registered rather than left to FastAPI because its defaults are two
# different shapes — ``{"detail": "<sentence>"}`` and ``{"detail": [<errors>]}``
# — and a client cannot read a body whose shape depends on which layer said no.
# RateLimitExceeded is an HTTPException too, and keeps the handler registered
# above because it needs a Retry-After header this one knows nothing about.
# Starlette resolves a handler by walking the exception's MRO, so the more
# specific registration wins whichever order they are added in; that handler
# renders through the same helper regardless.
app.add_exception_handler(StarletteHTTPException, errors.http_exception_handler)
app.add_exception_handler(RequestValidationError, errors.validation_exception_handler)

# Order matters here and is the opposite of how it reads. add_middleware puts
# each one outside the last, so CORSMiddleware being added second leaves it
# outermost — which is what lets the 500 RequestContext builds for an otherwise
# uncaught failure pick up its Access-Control-Allow-Origin on the way out. See
# app/api/middleware.py for why the alternatives do not.
app.add_middleware(RequestContext)

# Only the known frontend may call the API from a browser. Credentials are not
# used (the session travels as a bearer token, not a cookie), so a wildcard
# origin is neither needed nor safe to combine with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().resolved_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    # Response headers are not readable from JavaScript unless they are named
    # here, and an id a caller cannot read is one they cannot quote in a bug
    # report.
    expose_headers=[REQUEST_ID_HEADER],
)

app.include_router(login.router)
app.include_router(auth.router)
app.include_router(patients.router)
app.include_router(providers.router)
app.include_router(health.router)


def custom_openapi() -> dict:
    """The generated document, with the two error responses no route declares.

    Both are true of every endpoint and neither is visible to FastAPI's
    generator. The 422 it writes describes its own ``HTTPValidationError``, which
    this application no longer answers with; the 500 comes from the middleware in
    ``app/api/middleware.py``, which sits outside the routes entirely. Patching
    the document here beats restating either in every router's ``responses``,
    where the next endpoint added is the one that forgets.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        tags=app.openapi_tags,
        routes=app.routes,
    )

    # ``refusal`` builds a ``responses`` entry, which names its model rather than
    # referencing it. In the finished document a schema is a $ref, so the model
    # is swapped for one pointing at the component FastAPI already emitted for
    # ``ErrorResponse`` — it is the declared response of enough routes that it is
    # always there.
    def entry(source: dict) -> dict:
        content = source.get("content", {}).get("application/json", {})
        body = {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
        if "examples" in content:
            body["examples"] = dict(content["examples"])
        return {
            "description": source["description"],
            "content": {"application/json": body},
        }

    for path in schema.get("paths", {}).values():
        for operation in path.values():
            responses = operation.get("responses")
            if responses is None:
                continue
            # A fresh entry per operation rather than one shared object, so a
            # consumer editing the document in place cannot edit every endpoint.
            if "422" in responses:
                responses["422"] = entry(errors.VALIDATION_FAILED)
            responses.setdefault("500", entry(errors.UNEXPECTED_FAILURE))

    # Nothing answers with these any more, and a schema a caller can reach from
    # no response is a shape they may reasonably think they have to handle.
    for orphan in ("HTTPValidationError", "ValidationError"):
        schema.get("components", {}).get("schemas", {}).pop(orphan, None)

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi
