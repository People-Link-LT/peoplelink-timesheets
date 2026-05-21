import logging
import httpx

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_GRAPH = "https://graph.microsoft.com/v1.0"


async def _get_token(tenant_id: str, client_id: str, client_secret: str, username: str, password: str) -> str:
    url = _TOKEN_URL.format(tenant_id=tenant_id)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
            "scope": "https://graph.microsoft.com/Files.ReadWrite offline_access",
        })
        resp.raise_for_status()
        return resp.json()["access_token"]


async def upload_file(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
    folder: str,
    filename: str,
    content: bytes,
) -> None:
    token = await _get_token(tenant_id, client_id, client_secret, username, password)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
    }
    upload_url = f"{_GRAPH}/me/drive/root:/{folder}/{filename}:/content"
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.put(upload_url, content=content, headers=headers)
        resp.raise_for_status()
        logger.info(f"OneDrive upload OK: {folder}/{filename} ({len(content):,} bytes)")
