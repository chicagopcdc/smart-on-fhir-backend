"""Request and response models for the API.

These are what FastAPI reads to generate the OpenAPI document, so they are the
public contract: a field that is not here is not documented, and a route that
returns something a model does not describe fails loudly rather than shipping an
undocumented shape.

Field names are camelCase, matching FHIR itself and the envelope
``app/fhir/normalize.py`` already produces. ``populate_by_name`` means the models
still accept the Python spelling, so a route can build one from keyword
arguments and let serialization apply the alias.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    """Base for every model in the API: camelCase out, either spelling in."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ErrorResponse(BaseModel):
    """The body of a failed request.

    FastAPI's ``HTTPException`` renders as ``{"detail": ...}``, so declaring it
    here documents what a caller already receives rather than inventing a second
    error shape for them to handle.
    """

    model_config = ConfigDict(json_schema_extra={"examples": [{"detail": "Invalid or expired session"}]})

    detail: str
