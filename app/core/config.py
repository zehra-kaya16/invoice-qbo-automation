from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # AI Extraction
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    ai_provider: Literal["openai", "anthropic"] = "openai"
    ai_model: str = "gpt-4o"

    # Database
    database_url: Optional[str] = None

    # Browser sessions: enable Secure cookies when served over HTTPS.
    auth_cookie_secure: bool = False
    auth_session_hours: int = 12

    # Redis
    redis_url: Optional[str] = None

    # QuickBooks Online
    qbo_client_id: Optional[str] = None
    qbo_client_secret: Optional[str] = None
    qbo_token_encryption_key: Optional[str] = None
    qbo_redirect_uri: str = "http://localhost:8000/api/qbo/callback"
    qbo_environment: Literal["sandbox", "production"] = "sandbox"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def ai_api_key(self) -> Optional[str]:
        if self.ai_provider == "openai":
            return self.openai_api_key

        return self.anthropic_api_key


settings = Settings()
