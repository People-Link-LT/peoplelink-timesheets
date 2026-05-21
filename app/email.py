import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import settings

logger = logging.getLogger(__name__)


def send_otp_email(to_email: str, full_name: str, code: str) -> None:
    if not settings.smtp_host:
        logger.warning("SMTP not configured — cannot send OTP email.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{code} is your PeopleLink Timesheets login code"
    msg["From"] = settings.smtp_from or settings.smtp_username
    msg["To"] = to_email

    text = (
        f"Hi {full_name},\n\n"
        f"Your login code is: {code}\n\n"
        f"This code expires in 10 minutes. Do not share it.\n\n"
        f"PeopleLink Timesheets"
    )
    html = f"""
    <div style="font-family:sans-serif;max-width:420px;margin:0 auto;padding:24px">
      <p style="color:#1d4ed8;font-weight:700;font-size:18px;margin-bottom:4px">PeopleLink Timesheets</p>
      <p style="color:#374151">Hi {full_name},</p>
      <p style="color:#374151">Your login verification code is:</p>
      <div style="font-size:36px;font-weight:700;letter-spacing:10px;text-align:center;
                  padding:20px;background:#f3f4f6;border-radius:10px;margin:20px 0;color:#111827">
        {code}
      </div>
      <p style="color:#6b7280;font-size:13px">Expires in 10 minutes. Never share this code with anyone.</p>
    </div>
    """
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)

    logger.info(f"OTP email sent to {to_email}")
