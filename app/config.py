"""Application configuration loaded from environment / .env file.

All runtime knobs live here. Secrets come from the environment (see .env.example);
detection tuning (weights, thresholds, label lists) will move into the database
with an admin UI in later milestones — this file only holds infrastructure config.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General ---
    app_env: str = "development"  # development | production
    app_version: str = "0.1.0"
    secret_key: str = "change-me-in-production"

    # --- Datastores (defaults target the docker-compose network) ---
    database_url: str = "postgresql+asyncpg://trackguard:trackguard@postgres:5432/trackguard"
    redis_url: str = "redis://redis:6379/0"

    # --- Telegram (optional in M0; the bot idles gracefully without a token) ---
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None  # without leading @, for deep-links
    admin_tg_ids: str = ""  # comma-separated Telegram user IDs

    # --- Spotify (client-credentials; optional until the user creates an app) ---
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    # Throttling: dev-mode quota is small and measured over a rolling 30s window.
    # Requests are spaced by this interval; 429s are always honored via Retry-After.
    spotify_min_interval_seconds: float = 1.0
    spotify_albums_page_limit: int = 10  # Spotify api max limit is 10 for some accounts
    spotify_max_retries: int = 4

    # --- YouTube ---
    youtube_api_key: str | None = None
    youtube_search_quota_daily: int = 90
    youtube_min_interval_seconds: float = 1.0

    # --- AI Judge / Anthropic ---
    anthropic_api_key: str | None = None
    ai_judge_monthly_budget_usd: float = 10.0

    # --- Scheduler & Decay ---
    scheduler_interval_minutes: int = 15
    hot_track_decay_days: int = 60
    hot_track_max_clean_scans: int = 10

    # --- Web / sessions ---
    base_url: str = "http://localhost:8080"  # public URL, used to build links
    session_cookie: str = "tg_session"
    session_max_age: int = 60 * 60 * 24 * 14  # 14 days
    login_nonce_ttl_seconds: int = 300  # deep-link login handshake window

    # --- Storage (inside the container; backed by the 'appdata' volume) ---
    data_dir: str = "/data"

    # --- Health / heartbeats ---
    heartbeat_ttl_seconds: int = 180

    # --- Ops (M7, PLAN.md §12) ---
    # External dead-man switch: https://healthchecks.io free check "Ping URL".
    # If the whole VPS dies, nothing running on it can report that — this external
    # watchdog notices the missing ping and alerts instead.
    healthchecks_ping_url: str | None = None
    dead_man_ping_interval_seconds: int = 300
    # yt-dlp canary: a known-good, stable video (e.g. one of the artist's own
    # uploads) to test-extract weekly. Empty = canary skipped (nothing configured).
    ytdlp_canary_url: str | None = None

    @property
    def admin_ids(self) -> list[int]:
        return [int(x) for x in self.admin_tg_ids.split(",") if x.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def audio_dir(self) -> str:
        return f"{self.data_dir}/audio"

    @property
    def cover_dir(self) -> str:
        return f"{self.data_dir}/covers"

    @property
    def evidence_dir(self) -> str:
        # Separate from cover_dir: a frozen copy that survives the live cover being
        # overwritten by a later rescan or deleted along with the pirate release.
        return f"{self.data_dir}/evidence"

    @property
    def takedown_followup_days(self) -> int:
        # No response 14 days after sending a complaint -> remind the owner (PLAN §11).
        return 14


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
