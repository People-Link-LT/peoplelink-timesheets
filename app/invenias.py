import httpx
from app.config import settings

SUBDOMAIN = "peoplelink"
BASE_URL = f"https://{SUBDOMAIN}.invenias.com/api"
TOKEN_URL = f"https://{SUBDOMAIN}.invenias.com/identity/connect/token"


async def get_token() -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "password",
                "username": settings.invenias_username,
                "password": settings.invenias_password,
                "scope": "api",
            },
            auth=(settings.invenias_client_id, settings.invenias_client_secret),
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def fetch_active_assignments() -> list[dict]:
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "Select": ["AssignmentReferenceNumber", "CompanyDisplayName", "FileAs", "Status_lookup"],
        "PageSize": 500,
        "PageIndex": 0,
        "Sort": [{"Selector": "AssignmentReferenceNumber", "Desc": False}],
        "Filter": [["Status_lookup", "=", "Active"]],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{BASE_URL}/v1/assignments/list", headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json().get("Items", [])
