import logging
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_GRAPH = "https://graph.microsoft.com/v1.0"


async def _get_token() -> str:
    url = _TOKEN_URL.format(tenant_id=settings.sharepoint_tenant_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, data={
            "grant_type": "client_credentials",
            "client_id": settings.sharepoint_client_id,
            "client_secret": settings.sharepoint_client_secret,
            "scope": "https://graph.microsoft.com/.default",
        })
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _send_via_graph(to_email: str, subject: str, html: str, text: str) -> None:
    token = await _get_token()
    send_as = settings.smtp_from or settings.smtp_username
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": False,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_GRAPH}/users/{send_as}/sendMail",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if not resp.is_success:
            logger.error(f"Graph sendMail failed {resp.status_code}: {resp.text}")
        resp.raise_for_status()


def send_otp_email(to_email: str, full_name: str, code: str) -> None:
    from app.config import settings as s
    import asyncio

    if not all([s.sharepoint_tenant_id, s.sharepoint_client_id, s.sharepoint_client_secret, s.smtp_from or s.smtp_username]):
        logger.warning("Microsoft Graph email not configured — skipping OTP send.")
        return

    subject = f"{code} is your PeopleLink Timesheets login code"
    html = f"""
    <div style="font-family:sans-serif;max-width:420px;margin:0 auto;padding:24px">
      <p style="color:#1d4ed8;font-weight:700;font-size:18px;margin-bottom:4px">PeopleLink Timesheets</p>
      <p style="color:#374151">Hi {full_name},</p>
      <p style="color:#374151">Your login verification code is:</p>
      <div style="font-size:36px;font-weight:700;letter-spacing:10px;text-align:center;
                  padding:20px;background:#f3f4f6;border-radius:10px;margin:20px 0;color:#111827">
        {code}
      </div>
      <p style="color:#6b7280;font-size:13px">Expires in 10 minutes. Never share this code.</p>
    </div>
    """
    text = f"Hi {full_name},\n\nYour login code is: {code}\n\nExpires in 10 minutes.\n\nPeopleLink Timesheets"

    try:
        asyncio.run(_send_via_graph(to_email, subject, html, text))
        logger.info(f"OTP email sent to {to_email}")
    except Exception as e:
        logger.error(f"Failed to send OTP email to {to_email}: {e}")


def send_backup_alert_email(to_email: str, date_str: str, missing: list) -> None:
    import asyncio
    missing_list = "".join(f"<li>{f}</li>" for f in missing)
    subject = f"⚠️ PeopleLink Timesheets — backup missing for {date_str}"
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:24px">
      <p style="color:#1d4ed8;font-weight:700;font-size:18px;margin-bottom:4px">PeopleLink Timesheets</p>
      <p style="color:#374151">Daily backup check failed for <strong>{date_str}</strong>.</p>
      <p style="color:#374151">The following files were not found on SharePoint:</p>
      <ul style="color:#dc2626;font-family:monospace">{missing_list}</ul>
      <p style="color:#374151">Please check the Railway logs and run a manual backup from the admin panel.</p>
      <p style="color:#6b7280;font-size:12px">This alert was sent automatically at 17:00.</p>
    </div>
    """
    text = f"Backup missing for {date_str}:\n" + "\n".join(f"  - {f}" for f in missing)
    asyncio.run(_send_via_graph(to_email, subject, html, text))
