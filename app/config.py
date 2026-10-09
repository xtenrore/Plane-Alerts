"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

from typing import Any
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict



class Settings(BaseSettings):
    """All bot configuration, loaded from environment variables or a .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Telegram ────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    webhook_url: str = ""  # Public HTTPS URL for Telegram Webhook (blank = long polling mode)
    webhook_secret: str = ""  # Optional secret token for Webhook verification

    # ── MongoDB ─────────────────────────────────────────────────────────
    mongo_uri: str = "mongodb://localhost:27017"
    database_name: str = "aircraft_bot"

    # ── Aircraft Data Providers ─────────────────────────────────────────
    adsb_lol_base_url: str = "https://api.adsb.lol/v2"
    adsb_fi_base_url: str = "https://opendata.adsb.fi/api/v2"
    opensky_base_url: str = "https://opensky-network.org/api"
    airplanes_live_base_url: str = "https://api.airplanes.live/v2"
    adsb_one_base_url: str = "https://api.adsb.one/v2"

    # OpenSky OAuth2 token endpoint
    opensky_token_url: str = (
        "https://auth.opensky-network.org/auth/realms/"
        "opensky-network/protocol/openid-connect/token"
    )

    # OpenSky API keys directory (contains .json credential files)
    api_keys_dir: str = "api"

    # ── AI Providers ───────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model_primary: str = "gemini-2.0-flash-lite"
    gemini_model_secondary: str = "gemini-2.0-flash-lite"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # ── Trajectory Prediction ───────────────────────────────────────────
    # Kinematic trajectory calculation runs natively in-process.
    predictor_service_url: str = ""  # Optional remote override if desired

    # ── Monitoring ──────────────────────────────────────────────────────
    poll_interval_seconds: int = 5
    default_radius_km: float = 15.0
    cooldown_minutes: int = 30

    # ── Learning ────────────────────────────────────────────────────────
    learning_plane_threshold: int = 100  # planes to observe before selecting providers
    relearn_plane_count: int = 25  # extra planes on dislike feedback

    # ── Admin ───────────────────────────────────────────────────────────
    admin_telegram_id: int | None = None
    admin_password: str = ""  # Optional password for admin panel

    @field_validator("admin_telegram_id", mode="before")
    @classmethod
    def parse_empty_admin_id(cls, v: Any) -> Any:
        if v == "" or v is None:
            return None
        return v

    # ── Server ──────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"


# Singleton – import this everywhere
settings = Settings()
