import httpx
import asyncio
from app.fhir import normalize
from app.providers import config, scopes



async def fetch_single_resource(client, url, params, headers):
    """Fetch one FHIR search or resource, as a uniform result dict.

    Returns ``{success, status_code, data, error}``. ``status_code`` is None only
    when nothing arrived at all (timeout, DNS, refused); a non-200 or a 200 that
    is not JSON keeps the status it came with and reports the body as ``error``.
    """
    def result(success, status_code, data=None, error=None):
        return {
            "success": success,
            "status_code": status_code,
            "data": data,
            "error": error,
        }

    try:
        response = await client.get(url, params=params, headers=headers)
    except Exception as e:
        # Nothing arrived at all: a timeout, a DNS failure, a refused connection.
        return result(False, status_code=None, error=str(e))

    if response.status_code != 200:
        return result(False, response.status_code, error=response.text)

    try:
        data = response.json()
    except ValueError:
        # A 200 whose body is not JSON, typically an error page from something
        # sitting in front of the EHR. It did arrive, so it keeps the status it
        # arrived with rather than being reported as unreachable.
        return result(False, response.status_code, error=response.text)

    return result(True, response.status_code, data=data)


async def fetch_and_normalize(client, url, params, headers, resource_type, fhir_type):
    """Fetch one resource type and normalize it, returning ``(key, envelope)``.

    Pairing the two means the parsing happens as each response lands rather than
    after every one of them has, so it overlaps the requests still in flight
    instead of being added to the end of the slowest.
    """
    response = await fetch_single_resource(client, url, params, headers)

    if response["success"]:
        return resource_type, normalize.normalize_response(
            response["data"], fhir_type=fhir_type, status_code=response["status_code"]
        )
    return resource_type, normalize.normalize_failure(
        fhir_type=fhir_type,
        status_code=response["status_code"],
        body=response["error"],
    )


def withheld_by_scope(entry: dict) -> dict:
    """The envelope for a type the grant does not cover, in place of a request.

    The shape a failed read already produces, so a consumer needs no new branch:
    this is a type that could not be read, and the reason happens to be one no
    request to the provider would change. ``statusCode`` is null because nothing
    was asked — a 403 here would claim the provider refused something it was
    never sent.
    """
    return normalize.failed_response(
        fhir_type=config.fhir_type_for(entry),
        error="This connection was not granted access to this resource type",
        status_code=None,
    )


async def fetch_fhir_resources(
    access_token, base_url, fhir_patient_id, resource_types, scope=None
):
    """Read the named fetch config rows for one patient at one server.

    ``resource_types`` is already resolved, from a tier or from an explicit list,
    so which slice of the record to read stays a decision the caller makes and
    this stays the code that reads it.

    ``scope`` is what this connection was granted, and a row it does not cover is
    answered without being asked for. That belongs here rather than in the
    caller: a request the grant forbids buys a 403 and nothing else, so declining
    to send it is a property of reading rather than a policy each route
    reimplements — and every caller gets it, including the deprecated one that
    would otherwise go on collecting a refusal per type. None means no
    restriction we can act on, which is what a server granting what was asked for
    looks like.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/fhir+json"
    }
    readable, withheld = scopes.partition(resource_types, scope)

    # All types are requested at once, which for the widest read is the whole
    # config. Cap connections so that does not hit one EHR as a throttle-worthy
    # burst, and set a timeout so one slow endpoint cannot hold the request open.
    limits = httpx.Limits(max_connections=10)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        tasks = []

        for resource_type, endpoint_config in readable.items():
            url_path = endpoint_config["url_template"].format(patient_id=fhir_patient_id)

            params = {}
            if endpoint_config.get("needs_patient_param", False):
                params["patient"] = fhir_patient_id

            if "extra_params" in endpoint_config:
                params.update(endpoint_config["extra_params"])

            tasks.append(
                fetch_and_normalize(
                    client,
                    base_url + url_path,
                    params,
                    headers,
                    resource_type,
                    config.fhir_type_for(endpoint_config),
                )
            )

        read = dict(await asyncio.gather(*tasks))

    # What leaves this module is our shape, keyed by fetch config row, rather
    # than the provider's — and every row the caller asked for is in it, in the
    # order they asked, whether it was read or withheld. A caller should not have
    # to know which happened to find out what a type came back as.
    return {
        name: read[name] if name in read else withheld_by_scope(entry)
        for name, entry in resource_types.items()
    }

