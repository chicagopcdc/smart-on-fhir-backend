import httpx
import asyncio
import config



async def fetch_single_resource(client, url, params, headers, resource_type):
    """
    Fetch a single FHIR resource type for the patient.

    Args:
        client (httpx.AsyncClient): The async HTTP client to use.
        url (str): The relative URL path to query.
        params (dict): Query parameters (e.g., patient ID, extra filters).
        headers (dict): Headers including Authorization.
        resource_type (str): The resource type name (for logging).

    Returns:
        dict: {
            "resource_type": str,
            "success": bool,
            "status_code": int,
            "data": dict or None,
            "error": str or None
        }
    """
    try:
        response = await client.get(url, params=params, headers=headers)
        
        if response.status_code == 200:
            return {
                "resource_type": resource_type,
                "success": True,
                "status_code": response.status_code,
                "data": response.json(),
                "error": None
            }
        else:
            return {
                "resource_type": resource_type,
                "success": False,
                "status_code": response.status_code,
                "data": None,
                "error": response.text
            }
    except Exception as e:
        return {
            "resource_type": resource_type,
            "success": False,
            "status_code": None,
            "data": None,
            "error": str(e)
        }


async def fetch_fhir_resources(access_token, base_url, fhir_patient_id):
    epic_fhir_base_url = base_url

    resource_types = config.RESOURCE_FETCH_CONFIG

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/fhir+json"
    }

    resources = {}

    async with httpx.AsyncClient() as client:
        tasks = []

        for resource_type, endpoint_config in resource_types.items():
            url_path = endpoint_config["url_template"].format(patient_id=fhir_patient_id)

            params = {}
            if endpoint_config.get("needs_patient_param", False):
                params["patient"] = fhir_patient_id

            if "extra_params" in endpoint_config:
                params.update(endpoint_config["extra_params"])

            tasks.append(fetch_single_resource(client, epic_fhir_base_url + url_path, params, headers, resource_type))

        responses = await asyncio.gather(*tasks)

        for resource, response in zip(resource_types, responses):
            print(f"--- {resource} ---")
            print(f"Status: {response['status_code']}")
            print(f"Error: {response['error']}\n")

            if response["success"]:
                resources[resource] = response["data"]
            else:
                # resources[resource] = None
                resources[resource] = {
                    "error": response["error"],
                    "status_code": response["status_code"]
                }

    return resources

