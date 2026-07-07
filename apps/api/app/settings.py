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

    # --- Phase 4 channel platform-app credentials (empty ⇒ channel disabled) ---
    # Slack: Events API request signing (v0=hmac_sha256(signing_secret, body)).
    slack_signing_secret: str = ""
    # Google OAuth2 (Gmail XOAUTH2 email + YouTube Data API). google_* is the
    # generic Google app; youtube_* may override for a dedicated YouTube app.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    youtube_oauth_client_id: str = ""
    youtube_oauth_client_secret: str = ""
    # Microsoft identity platform OAuth2 (Outlook/Office365 XOAUTH2 email).
    ms_oauth_client_id: str = ""
    ms_oauth_client_secret: str = ""
    # Zalo OA (OAuth v4 access/refresh token).
    zalo_app_id: str = ""
    zalo_app_secret: str = ""
    # VKontakte Callback API default confirmation string (per-community may set
    # its own in channel config; this is the platform-wide fallback).
    vk_confirmation_default: str = ""
    # TikTok Business Messaging.
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""

    # --- embeddings (P3): self-hosted bge-m3 sidecar (1024-dim, OpenAI-less) ---
    # sub2api relay has no embeddings endpoint; when set, services/embeddings
    # delegates to this HTTP server (POST {embed_base_url}/embed) for RAG.
    embed_base_url: str = ""
    embed_dim: int = 1024
    embed_timeout_s: float = 30.0

    # --- Stripe billing (P3). Empty secret key ⇒ billing disabled (no crash). ---
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_currency: str = "usd"
    # handling fee applied to the priced subtotal (ceil to cents). Reproduces the
    # observed backend example (Max 7d base $19.90 → fee $1.40) at 0.07.
    billing_handling_fee_pct: float = 0.07

    # --- broadcast / marketing pipeline (P3, plan B.3) ---
    broadcast_chunk_size: int = 500  # recipients materialised per fan-out chunk
    broadcast_min_interval_s: int = 3  # send-rate floor (3s generic / WhatsApp)
    broadcast_max_interval_s: int = 600  # send-rate ceiling
    broadcast_recycle_days: int = 30  # recycle-bin purge window
    broadcast_freq_cap_per_week: int = 0  # per-contact weekly cap (0 = disabled)
    broadcast_recent_months: int = 3  # list view only shows the recent N months

    access_token_ttl_min: int = 30
    refresh_token_ttl_days: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
