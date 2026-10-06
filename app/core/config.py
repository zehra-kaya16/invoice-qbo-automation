from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # OpenAI
    openai_api_key: Optional[str] = None

    # Database
    database_url: Optional[str] = None

    # Redis
    redis_url: Optional[str] = None

    # QuickBooks Online
    qbo_client_id: Optional[str] = None
    qbo_client_secret: Optional[str] = None
    qbo_redirect_uri: str = "http://localhost:8000/api/qbo/callback"
    qbo_environment: Literal["sandbox", "production"] = "sandbox"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()