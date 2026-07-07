from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://smartchat:smartchat@localhost:5433/smartchat"
    redis_url: str = "redis://localhost:6380/0"
    secret_key: str = "dev-secret"
    credentials_master_key: str = ""
    public_base_url: str = "http://localhost:8000"
    assets_base_url: str = "http://localhost:8000"

    minio_endpoint: str = "localhost:9002"
    minio_root_user: str = "smartchat"
    minio_root_password: str = "smartchat-secret"
    minio_bucket: str = "smartchat"
    minio_secure: bool = False

    llm_provider: str = "anthropic"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model_fast: str = "claude-haiku-4-5-20251001"
    llm_model_smart: str = "claude-sonnet-5"
    llm_model_embed: str = "text-embedding-3-large"

    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_verify_token: str = ""

    access_token_ttl_min: int = 30
    refresh_token_ttl_days: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
