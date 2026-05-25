import logging
import time
import httpx

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_GRAPH = "https://graph.microsoft.com/v1.0"

# In-memory cache — avoids repeating token + site lookups on every page load
_cache: dict[str, dict] = {}


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.monotonic() < entry["exp"]:
        return entry["val"]
    return None


def _cache_set(key: str, value, ttl: int):
    _cache[key] = {"val": value, "exp": time.monotonic() + ttl}


async def _get_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    key = f"tok:{tenant_id}:{client_id}"
    cached = _cache_get(key)
    if cached:
        return cached

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _TOKEN_URL.format(tenant_id=tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        if not resp.is_success:
            logger.error(f"Token request failed {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        token = resp.json()["access_token"]

    _cache_set(key, token, 3300)  # tokens last 1h; cache for 55 min
    return token


async def _resolve_drive(
    client: httpx.AsyncClient,
    auth: dict,
    site_hostname: str,
    site_path: str,
    drive_name: str,
) -> str:
    """Returns drive_base URL. Cached for 24 h — site/drive IDs never change."""
    key = f"drive:{site_hostname}:{site_path}:{drive_name}"
    cached = _cache_get(key)
    if cached:
        return cached

    site_resp = await client.get(
        f"{_GRAPH}/sites/{site_hostname}:/{site_path}", headers=auth
    )
    if not site_resp.is_success:
        raise RuntimeError(f"Site lookup {site_resp.status_code}: {site_resp.text}")
    site_id = site_resp.json()["id"]

    if drive_name:
        drives_resp = await client.get(
            f"{_GRAPH}/sites/{site_id}/drives", headers=auth
        )
        drives_resp.raise_for_status()
        drive = next(
            (d for d in drives_resp.json()["value"] if d["name"] == drive_name), None
        )
        if not drive:
            raise RuntimeError(f"Drive '{drive_name}' not found on site {site_path}")
        drive_base = f"{_GRAPH}/drives/{drive['id']}"
    else:
        drive_base = f"{_GRAPH}/sites/{site_id}/drive"

    _cache_set(key, drive_base, 86400)
    return drive_base


async def upload_file(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    site_hostname: str,
    site_path: str,
    folder: str,
    filename: str,
    content: bytes,
    drive_name: str = "",
) -> None:
    token = await _get_token(tenant_id, client_id, client_secret)
    auth = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=120) as client:
        drive_base = await _resolve_drive(client, auth, site_hostname, site_path, drive_name)
        upload_url = f"{drive_base}/root:/{folder}/{filename}:/content"
        resp = await client.put(
            upload_url,
            content=content,
            headers={**auth, "Content-Type": "application/octet-stream"},
        )
        if not resp.is_success:
            logger.error(f"Upload failed {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        logger.info(f"SharePoint upload OK: {folder}/{filename} ({len(content):,} bytes)")


async def list_files(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    site_hostname: str,
    site_path: str,
    folder: str = "",
    drive_name: str = "",
) -> list[dict]:
    token = await _get_token(tenant_id, client_id, client_secret)
    auth = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        drive_base = await _resolve_drive(client, auth, site_hostname, site_path, drive_name)

        items_url = (
            f"{drive_base}/root:/{folder}:/children" if folder
            else f"{drive_base}/root/children"
        )
        items_resp = await client.get(
            items_url,
            headers=auth,
            params={
                "$select": "id,name,size,lastModifiedDateTime,folder,webUrl,file,@microsoft.graph.downloadUrl",
                "$top": "500",
            },
        )
        if not items_resp.is_success:
            raise RuntimeError(f"List files failed {items_resp.status_code}: {items_resp.text}")

        result = []
        for item in items_resp.json().get("value", []):
            result.append({
                "id": item["id"],
                "name": item["name"],
                "size": item.get("size", 0),
                "modified": item.get("lastModifiedDateTime", ""),
                "download_url": item.get("@microsoft.graph.downloadUrl", ""),
                "web_url": item.get("webUrl", ""),
                "is_folder": "folder" in item,
                "mime_type": item.get("file", {}).get("mimeType", "") if "file" in item else "",
            })

        return sorted(result, key=lambda x: (not x["is_folder"], x["name"].lower()))


async def get_file_stream(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    site_hostname: str,
    site_path: str,
    item_id: str,
    drive_name: str = "",
):
    """Returns (content_bytes, mime_type, filename) for streaming to the browser."""
    token = await _get_token(tenant_id, client_id, client_secret)
    auth = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=60) as client:
        drive_base = await _resolve_drive(client, auth, site_hostname, site_path, drive_name)

        meta_resp = await client.get(
            f"{drive_base}/items/{item_id}",
            headers=auth,
            params={"$select": "id,name,file,@microsoft.graph.downloadUrl"},
        )
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        filename = meta.get("name", "file")
        mime_type = meta.get("file", {}).get("mimeType", "application/octet-stream")
        download_url = meta.get("@microsoft.graph.downloadUrl", "")

        if not download_url:
            raise RuntimeError("No download URL for item")

        file_resp = await client.get(download_url, follow_redirects=True)
        file_resp.raise_for_status()
        return file_resp.content, mime_type, filename
