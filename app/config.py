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

    model_config = {"env_file": str(Path(__file__).parent.parent / ".env")}


settings = Settings()
