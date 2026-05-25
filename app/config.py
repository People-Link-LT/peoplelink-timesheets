from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 120  # 2 hours

    invenias_client_id: str
    invenias_client_secret: str
    invenias_username: str
    invenias_password: str

    # Email — sent via Microsoft Graph using the SharePoint app credentials
    smtp_from: str = ""      # M365 address to send from, e.g. matas@peoplelink.lt
    smtp_username: str = ""  # fallback alias for smtp_from

    setup_token: str = ""  # required to call /setup/create-admin

    # Ask PL — RAG assistant
    openai_api_key: str = ""       # for text-embedding-3-small
    anthropic_api_key: str = ""    # for Claude Haiku streaming

    # SharePoint backup (optional — backup job skips if empty)
    sharepoint_tenant_id: str = ""
    sharepoint_client_id: str = ""
    sharepoint_client_secret: str = ""
    sharepoint_site_hostname: str = ""   # e.g. peoplelink.sharepoint.com
    sharepoint_site_path: str = ""       # e.g. sites/IT
    sharepoint_backup_folder: str = "Timesheets/Backups"
    sharepoint_drive_name: str = ""        # named document library, e.g. "Kiti dokumentai"
    sharepoint_documents_folder: str = ""  # default folder for Documents browse/upload, e.g. "Documents"
    sharepoint_index_drives: str = ""      # comma-separated drives to index for Ask PL, e.g. "Kiti dokumentai,Aktualūs dokumentai"

    model_config = {"env_file": str(Path(__file__).parent.parent / ".env")}


settings = Settings()
