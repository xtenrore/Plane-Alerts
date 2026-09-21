"""Application configuration loaded from environment variables / .env file."""
from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All bot configuration, loaded from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    telegram_bot_token: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    mongo_uri: str = "mongodb://localhost:27017"
    database_name: str = "aircraft_bot"

    adsb_lol_base_url: str = "https://api.adsb.lol/v2"
    adsb_fi_base_url: str = "https://opendata.adsb.fi/api/v2"
    opensky_base_url: str = "https://opensky-network.org/api"
    airplanes_live_base_url: str = "https://api.airplanes.live/v2"
    adsb_one_base_url: str = "https://api.adsb.one/v2"
    opensky_token_url: str = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

    local_adsb_url: str = ""
    local_adsb_receiver_type: str = "auto"
    local_adsb_timeout_seconds: float = 0.8
    local_adsb_max_position_age_seconds: float = 12.0
    local_adsb_auth_header: str = ""

    opensky_1: str = ""
    opensky_2: str = ""
    opensky_3: str = ""
    opensky_4: str = ""
    opensky_5: str = ""
    opensky_credentials_json: str = ""
    api_keys_dir: str = "api"

    route_lookup_url: str = "https://api.adsb.lol/api/0/routeset"
    route_lookup_single_url: str = "https://api.adsb.lol/api/0/route"
    route_lookup_cache_seconds: int = 1200
    route_history_days: int = 3
    route_sample_interval_seconds: int = 30

    gemini_api_key: str = ""
    gemini_api_key_2: str = ""
    gemini_model_primary: str = "gemini-3.5-flash-lite"
    gemini_model_secondary: str = "gemini-3.5-flash"
    groq_key: str = ""
    groq_key_2: str = ""
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    gemini_photo_model: str = "gemini-3.8-flash"
    gemini_photo_fallback_model: str = "gemini-3.5-flash-lite"
    gemini_photo_timeout_seconds: float = 30.0
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_air_quality_url: str = "https://air-quality-api.open-meteo.com/v1/air-quality"
    photography_http_timeout_seconds: float = 12.0
    photography_conditions_cache_seconds: int = 120

    predictor_service_url: str = ""
    early_warning_buffer_km: float = 15.0
    poll_interval_seconds: int = 5
    default_radius_km: float = 15.0
    cooldown_minutes: int = 30
    learning_plane_threshold: int = 100
    relearn_plane_count: int = 25
    admin_telegram_id: int | None = None
    admin_password: str = ""
    agy_worker_url: str = ""
    agy_worker_token: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    @field_validator("admin_telegram_id", mode="before")
    @classmethod
    def _validate_optional_admin_telegram_id(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("local_adsb_receiver_type")
    @classmethod
    def _validate_local_receiver_type(cls, value: str) -> str:
        normalised = str(value or "auto").strip().lower()
        allowed = {"auto", "readsb", "dump1090", "dump1090-fa", "ultrafeeder"}
        if normalised not in allowed:
            raise ValueError("LOCAL_ADSB_RECEIVER_TYPE must be auto, readsb, dump1090, dump1090-fa, or ultrafeeder")
        return normalised

    @field_validator("local_adsb_url")
    @classmethod
    def _validate_local_adsb_url(cls, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LOCAL_ADSB_URL must be a valid http:// or https:// URL")
        if parsed.username or parsed.password:
            raise ValueError("Do not embed local ADS-B credentials in LOCAL_ADSB_URL; use LOCAL_ADSB_AUTH_HEADER instead")
        return raw.rstrip("/")

    @field_validator("local_adsb_timeout_seconds")
    @classmethod
    def _validate_local_timeout(cls, value: float) -> float:
        timeout = float(value)
        if not 0.2 <= timeout <= 1.5:
            raise ValueError("LOCAL_ADSB_TIMEOUT_SECONDS must be between 0.2 and 1.5")
        return timeout

    @field_validator("local_adsb_max_position_age_seconds")
    @classmethod
    def _validate_local_freshness(cls, value: float) -> float:
        freshness = float(value)
        if not 1.0 <= freshness <= 30.0:
            raise ValueError("LOCAL_ADSB_MAX_POSITION_AGE_SECONDS must be between 1 and 30")
        return freshness

    @field_validator("poll_interval_seconds")
    @classmethod
    def _validate_poll_interval(cls, value: int) -> int:
        interval = int(value)
        if not 1 <= interval <= 30:
            raise ValueError("POLL_INTERVAL_SECONDS must be between 1 and 30")
        return interval

    @field_validator("default_radius_km")
    @classmethod
    def _validate_default_radius(cls, value: float) -> float:
        radius = float(value)
        if not 0.5 <= radius <= 250.0:
            raise ValueError("DEFAULT_RADIUS_KM must be between 0.5 and 250")
        return radius

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int) -> int:
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        return port


settings = Settings()
