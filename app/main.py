"""The application: middleware, lifespan, and the routers that make up the API."""

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


@asynccontextmanager
async def lifespan(app: FastAPI):
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
