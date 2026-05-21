from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480  # 8 hours

    invenias_client_id: str
    invenias_client_secret: str
    invenias_username: str
    invenias_password: str

    # Email / SMTP (optional — email OTP skipped if not configured)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    # SharePoint backup (optional — backup job skips if empty)
    sharepoint_tenant_id: str = ""
    sharepoint_client_id: str = ""
    sharepoint_client_secret: str = ""
    sharepoint_site_hostname: str = ""   # e.g. peoplelink.sharepoint.com
    sharepoint_site_path: str = ""       # e.g. sites/IT
    sharepoint_backup_folder: str = "Timesheets/Backups"

    model_config = {"env_file": str(Path(__file__).parent.parent / ".env")}


settings = Settings()
