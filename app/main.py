"""The application: middleware, lifespan, and the routers that make up the API."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from app.api import auth, deps, patients, providers
from app.core.config import get_settings
from app.core.db import engine
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
]


def configure_logging() -> None:
    """Give this application's own log records somewhere to go, and only ours.

    uvicorn configures three loggers of its own and leaves the root logger
    alone, so anything logged under ``app.*`` falls back to the WARNING default
    and every INFO record the application writes is dropped — including the one
    reporting how many connections a sweep retired, which exists precisely so
    that number is visible rather than inferred.

    Lowering the level on ``app`` rather than on the root is the difference
    between hearing this application and hearing every library it uses. httpx in
    particular logs a line per request at INFO, and a single read fans out over
    a dozen FHIR calls whose URLs carry the provider's own patient id — so a
    root-level floor would write exactly the identifier the rest of this code
    goes out of its way to keep out of logs. Records still reach the root
    handler once made; it is the level on the originating logger that decides
    whether they are made at all.

    ``basicConfig`` is a no-op once the root logger has handlers, so a
    deployment that configures its own logging keeps it, and this only supplies
    a destination for one that does not.
    """
    logging.basicConfig(format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logging.getLogger("app").setLevel(logging.INFO)


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

# Only the known frontend may call the API from a browser. Credentials are not
# used (the session travels as a bearer token, not a cookie), so a wildcard
# origin is neither needed nor safe to combine with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().resolved_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(patients.router)
app.include_router(providers.router)
