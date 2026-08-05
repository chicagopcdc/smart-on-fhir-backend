import httpx
import asyncio
from app.fhir import normalize
from app.providers import config



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


async def fetch_fhir_resources(access_token, base_url, fhir_patient_id, resource_types):
    """Read the named fetch config rows for one patient at one server.

    ``resource_types`` is already resolved, from a tier or from an explicit list,
    so which slice of the record to read stays a decision the caller makes and
    this stays the code that reads it.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/fhir+json"
    }

    # All types are requested at once, which for the widest read is the whole
    # config. Cap connections so that does not hit one EHR as a throttle-worthy
    # burst, and set a timeout so one slow endpoint cannot hold the request open.
    limits = httpx.Limits(max_connections=10)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        tasks = []

        for resource_type, endpoint_config in resource_types.items():
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

        # What leaves this module is our shape, keyed by fetch config row, rather
        # than the provider's.
        return dict(await asyncio.gather(*tasks))

