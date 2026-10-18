from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # AI Extraction
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    ai_provider: Optional[Literal["openai", "anthropic"]] = None
    ai_model: Optional[str] = None

    max_upload_bytes: int = Field(20 * 1024 * 1024, ge=1)
    max_pdf_pages: int = Field(50, ge=1)
    max_heic_pixels: int = Field(40_000_000, ge=1, le=80_000_000)

    # Database
    database_url: Optional[str] = None

    # Private document objects. All access continues through authenticated APIs.
    storage_type: Literal["local", "s3", "r2"] = "local"
    local_storage_path: str = "./uploads"
    local_storage_id: str = "receipt-ai-local-v1"
    s3_bucket: Optional[str] = None
    s3_region: str = "us-east-1"
    s3_access_key: Optional[str] = Field(default=None, repr=False)
    s3_secret_key: Optional[str] = Field(default=None, repr=False)
    s3_endpoint: Optional[str] = None
    r2_bucket: Optional[str] = None
    r2_account_id: Optional[str] = None
    r2_access_key: Optional[str] = Field(default=None, repr=False)
    r2_secret_key: Optional[str] = Field(default=None, repr=False)

    # Browser sessions: enable Secure cookies when served over HTTPS.
    auth_cookie_secure: bool = False
    auth_session_hours: int = 12

    # Signed integration ingestion; exact public HTTPS hosts, no wildcards.
    webhook_download_hosts: str = ""
    email_enabled: bool = False
    email_provider: Literal["smtp", "sendgrid", "resend", "postmark"] = "smtp"
    email_from: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_security: Literal["starttls", "ssl"] = "starttls"
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = Field(default=None, repr=False)
    sendgrid_api_key: Optional[str] = Field(default=None, repr=False)
    resend_api_key: Optional[str] = Field(default=None, repr=False)
    postmark_api_key: Optional[str] = Field(default=None, repr=False)

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
        if (self.ai_provider or ("openai" if self.openai_api_key else "anthropic")) == "openai":
            return self.openai_api_key

        return self.anthropic_api_key


settings = Settings()
