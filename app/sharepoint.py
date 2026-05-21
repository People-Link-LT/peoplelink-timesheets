import logging
import httpx

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_GRAPH = "https://graph.microsoft.com/v1.0"


async def _get_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    url = _TOKEN_URL.format(tenant_id=tenant_id)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        })
        if not resp.is_success:
            logger.error(f"Token request failed {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        return resp.json()["access_token"]


async def upload_file(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    site_hostname: str,
    site_path: str,
    folder: str,
    filename: str,
    content: bytes,
) -> None:
    token = await _get_token(tenant_id, client_id, client_secret)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        site_resp = await client.get(
            f"{_GRAPH}/sites/{site_hostname}:/{site_path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if not site_resp.is_success:
            logger.error(f"Site lookup failed {site_resp.status_code}: {site_resp.text}")
        site_resp.raise_for_status()
        site_id = site_resp.json()["id"]

        upload_url = f"{_GRAPH}/sites/{site_id}/drive/root:/{folder}/{filename}:/content"
        resp = await client.put(upload_url, content=content, headers=headers)
        if not resp.is_success:
            logger.error(f"Upload failed {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        logger.info(f"SharePoint upload OK: {folder}/{filename} ({len(content):,} bytes)")
