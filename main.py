from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.datastructures import URL

from pydantic import BaseModel

import secrets
import httpx
import base64

import config
import service


app = FastAPI()

origins = ["http://localhost:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

state_store = {}  # replace with secure store later

class CallbackData(BaseModel):
    code: str
    state: str


# http://127.0.0.1:8000/auth/start?provider=EPIC_SANDBOX&iss=https://fhir.epic.com/interconnect-fhir-oauth
@app.get("/auth/start")
async def start_auth(provider: str, iss: str):
    ehr = config.EHR_CONFIGS.get(provider)
    if not ehr:
        return JSONResponse({"error": "Unknown or unsupported issuer (iss)"}, status_code=400)

    state = secrets.token_urlsafe(16)
    state_store[state] = {"iss": iss, "provider": provider}

    # scope and aud can be omitted
    auth_url = (
        f"{iss}{ehr['authorize_url']}?"
        f"response_type=code&"
        f"client_id={ehr['client_id']}&"
        f"redirect_uri={ehr['redirect_uri']}&"
        f"scope={ehr['scopes']}&"
        f"state={state}&"
        f"aud=https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
    )
    # return JSONResponse(content={"auth_url": auth_url})
    return RedirectResponse(auth_url)

# f"aud={iss + ehr['fhir_server_url']}"


@app.post("/auth/callback")
async def handle_callback(callback_data: CallbackData):
    code = callback_data.code
    state = callback_data.state

    print(state_store)
    if state not in state_store:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    iss = state_store[state]["iss"]
    provider = state_store[state]["provider"]
    ehr = config.EHR_CONFIGS.get(provider)

    credentials = f"{ehr['client_id']}:{ehr['client_secret']}".encode("utf-8")
    basic_auth_header = base64.b64encode(credentials).decode("utf-8")

    headers = {
        "Authorization": f"Basic {basic_auth_header}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ehr["redirect_uri"]
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(iss + ehr["token_url"], data=data, headers=headers)
        token_response = response.json()

    # print(token_response) TODO persist the refresh token information in the DB
    # {
    #     "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJ1cm46b2lkOjEuMi44NDAuMTE0MzUwLjEuMTMuMC4xLjcuMy42ODg4ODQuMTAwIiwiY2xpZW50X2lkIjoiMjk5MDVkN2YtNmMyYi00ODhiLTkzMTItY2M3NWQ0MWIxZTg0IiwiZXBpYy5lY2kiOiJ1cm46ZXBpYzpPcGVuLkVwaWMtY3VycmVudCIsImVwaWMubWV0YWRhdGEiOiJNOFFHLWxNZmZCUWR0Nm1IR3RIWVJlLWZIWlNVbVN5cklmS0FjR2syRW15MWpuNFE3VUN0a0RZdGdfTzFzMjJkaVhLZVZ1T3RtNkY5b1lQaThwbjI2eGFVMlN2MU5hYnRiUklueUQ4OUFqYkpDSGwxX2NocjE1OFdkQTQtUTZFSiIsImVwaWMudG9rZW50eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzQ1NjE4MzI3LCJpYXQiOjE3NDU2MTQ3MjcsImlzcyI6InVybjpvaWQ6MS4yLjg0MC4xMTQzNTAuMS4xMy4wLjEuNy4zLjY4ODg4NC4xMDAiLCJqdGkiOiJjY2ZkNzBlMy1jOGQ2LTQzOTUtYmEzMy00ODM5NWRmZjhmNGMiLCJuYmYiOjE3NDU2MTQ3MjcsInN1YiI6ImViNEdpYTdGeWlqdFBtWGtydGpScFB3MyJ9.iMoSaniowx6ltfsogPZlPiFfuCst0WOMDQHvayRga37TuTZR8EzWHScDaX6jHuBhn5cGeSkLzR5qAoOt6mAp9yK0CC2RaXC7J0fBL79yMUdRsx9LxFxW-L7DAkztbPbdror5SKCVtesAafWP_Xbup818Mt06C2m-9rL4JE_6B310dDyE46kGnKYZ-MSZBvq4xaRY0ySGrVhvGBF-7qhQ0TyzuYzCTjS3yPi7itCi_qHENcteg5OzNZVE0OtI75YqR-T8s1U-cOa2XlixRsnQi54mK7LFV5zcJq29T6c-talIp6xx-RiM0VbkXYo2W4_UZBTQwEZYMqsnPxpTe3Yy8A",
    #     "token_type": "Bearer",
    #     "expires_in": 3600,
    #     "scope": "patient/Binary.read patient/DocumentReference.read patient/Medication.read patient/MedicationRequest.read patient/Observation.read patient/Patient.read launch/patient offline_access openid profile",
    #     "state": "D3qwA4oGb_h1Gm3cbvOFpQ",
    #     "refresh_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJ1cm46b2lkOjEuMi44NDAuMTE0MzUwLjEuMTMuMC4xLjcuMy42ODg4ODQuMTAwIiwiY2xpZW50X2lkIjoiMjk5MDVkN2YtNmMyYi00ODhiLTkzMTItY2M3NWQ0MWIxZTg0IiwiZXBpYy5lY2kiOiJ1cm46ZXBpYzpPcGVuLkVwaWMtY3VycmVudCIsImVwaWMubWV0YWRhdGEiOiJDOVVGS3lsZnlKLURla0J3OGcwazRRUGFLZ3RvYndReW1mN0s0SHZVQ1RRS2JpWmZramdLNjR2TjEzcVVUU3NoR0RrdHNVS0JLV3kyWUNGaHZ2ODJoaW9MN3BZTDlPdFBFVGwyQy1PeVlnajhzVG01aWNvV2RxcUh1ZEZ2LXdfWSIsImVwaWMudG9rZW50eXBlIjoicmVmcmVzaCIsImlhdCI6MTc0NTYxNDcyNywiaXNzIjoidXJuOm9pZDoxLjIuODQwLjExNDM1MC4xLjEzLjAuMS43LjMuNjg4ODg0LjEwMCIsImp0aSI6IjNhNjJjOGI3LThmMDQtNDc5ZS04OWYwLTgxM2M2YThlYmE3NSIsIm5iZiI6MTc0NTYxNDcyNywic3ViIjoiZWI0R2lhN0Z5aWp0UG1Ya3J0alJwUHczIn0.pAyh0vzZbeqo73jIxRM3kFSBEDmSz-T5FI4t610SWGrLwWwzyd6UNn8tPOZeBXXT0SG3jpZnN0sr2_YpJ04uRf1CxpiIJkQ2XGlFX74RQEJL-6MFy_k-DbMJo8z89LCbFWCiwKi13S7ROU4tLjRI_duOtc2qfxOIJj1_uoWssYUjFDmY69xqdsWY4kEaXkBssRqyQclCddhpCmyRgzFVab-tBE4EELlC-NBW9OTpuc7qHP0xpsHSaiBWi-dEtWP2pt0V8Gv6t4h_AAxHeu98H53ToKwS7s2OoxJSthS7NotB1zyfcm2SnotEzgDjDIavEAr2iylEF-7E0SF5f9Y8EA",
    #     "__epic.dstu2.patient": "TnOZ.elPXC6zcBNFMcFA7A5KZbYxo2.4T-LylRk4GoW4B",
    #     "id_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6InZDTW9mQzZQTTZFZlF1NHBQQ0FIVUxLZUQrWVJpMXVuWStrNVRLNmtqcm89IiwidHlwIjoiSldUIn0.eyJhdWQiOiIyOTkwNWQ3Zi02YzJiLTQ4OGItOTMxMi1jYzc1ZDQxYjFlODQiLCJleHAiOjE3NDU2MTUwMjcsImlhdCI6MTc0NTYxNDcyNywiaXNzIjoiaHR0cHM6Ly9maGlyLmVwaWMuY29tL2ludGVyY29ubmVjdC1maGlyLW9hdXRoL29hdXRoMiIsInN1YiI6ImVyWHVGWVVmdWNCWmFyeVZrc1lFY01nMyJ9.eTnQ11gaaD9daWhdv6NkKI_FgiyDRzRMKoM3_esddLd0bPjXRunGHnPt7hs3aWa9MqPU1ABlSf71dTpw2XZ8KDHskbU37Qh98Txh7inJAegIs9ESQ_edGoxnCz7AKrTBH1xLvdy-Wrz5Xhud_cSj9EULuVH4jaqHq4FIJDxUj16EE3-6Qutm9-irFUoooladuLBziurLbvQ_7Ej6f0ZNSoD_TTvwFt4oPzQQod6EsJXSHV4vUjt8qfIgDVf5Sm-qX4zfXUvRTAK9DyjdSff_GJSVwDbo8Qgpr8GWjLeyLLmrSDYSnj_HlFftoPIwxurkDzZmOziRcOxut1B01buecQ",
    #     "patient": "erXuFYUfucBZaryVksYEcMg3"
    # }
    # return JSONResponse(token_response)

    return JSONResponse(content={"success": True}, status_code=200)


@app.get("/fhir_resources")
async def get_all_resource(access_token: str, fhir_patient_id: str, state: str):
    if not access_token or not fhir_patient_id or not state:
        return JSONResponse({"error": "Missing or unsupported parameters"}, status_code=400)

    iss = state_store[state]["iss"]
    provider = state_store[state]["provider"]
    ehr = config.EHR_CONFIGS.get(provider)

    resources = await service.fetch_fhir_resources(access_token, iss + ehr["fhir_server_url"], fhir_patient_id)
    return JSONResponse(resources)




# LANTERN_URL = "https://lantern.healthit.gov/api/endpoint-manager/endpoints"
# # https://lantern.healthit.gov/api/daily/download
# @app.get("/fhir-endpoints")
# async def get_fhir_endpoints():
#     async with httpx.AsyncClient() as client:
#         res = await client.get(LANTERN_URL)
#         res.raise_for_status()
#         data = res.json()

#     # Filter and transform
#     options = [
#         {
#             "label": f"{item.get('organizationName', 'Unknown')} ({item.get('url')})",
#             "value": item["url"]
#         }
#         for item in data
#         if item.get("url") and item.get("organizationName")
#     ]

#     return options





LANTERN_CSV_URL = "https://lantern.healthit.gov/api/daily/download"

@app.get("/lantern-csv")
async def get_lantern_csv():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(LANTERN_CSV_URL)
            response.raise_for_status()
        return Response(content=response.text, media_type="text/csv")
    except httpx.HTTPError as e:
        return Response(content=f"Error fetching CSV: {str(e)}", status_code=500)



