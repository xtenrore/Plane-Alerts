"""Pydantic models for canonical normalized aircraft observations."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class NormalizedAircraft(BaseModel):
    """Unified aircraft observation with source and field provenance.

    ``timestamp`` remains as a compatibility alias for the position observation
    time used by the existing trajectory pipeline. New code should prefer
    ``observed_at`` explicitly.
    """

    icao24: str = Field(..., description="ICAO 24-bit address")
    callsign: str = ""
    origin_country: str = ""
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = Field(default=None, description="Barometric altitude in metres")
    velocity: float | None = Field(default=None, description="Ground speed in m/s")
    heading: float | None = Field(default=None, description="True track/heading degrees")
    turn_rate: float = 0.0
    vertical_rate_mps: float | None = None
    position_age_s: float | None = None
    source_freshness_s: float | None = None
    data_quality: str = "unknown"
    aircraft_type: str = "UNKNOWN"
    observed_at: float | None = None
    received_at: float | None = None
    source: str = ""
    field_provenance: dict[str, str] = Field(default_factory=dict)
    source_candidates: list[str] = Field(default_factory=list)
    merge_notes: list[str] = Field(default_factory=list)
    timestamp: int | float | None = None

    @field_validator("aircraft_type", mode="before")
    @classmethod
    def _normalise_aircraft_type(cls, value):
        text = str(value or "").strip()
        return text.upper() if text else "UNKNOWN"

    @field_validator("icao24", mode="before")
    @classmethod
    def _normalise_icao24(cls, value):
        return str(value or "").lower().strip()

    @field_validator("source", mode="before")
    @classmethod
    def _normalise_source(cls, value):
        return str(value or "").strip().lower()

    @model_validator(mode="after")
    def _synchronise_observation_timestamp(self):
        if self.observed_at is None and self.timestamp is not None:
            self.observed_at = float(self.timestamp)
        elif self.timestamp is None and self.observed_at is not None:
            self.timestamp = self.observed_at
        if self.source and not self.source_candidates:
            self.source_candidates = [self.source]
        return self

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def display_type(self) -> str:
        return "Unknown" if self.aircraft_type == "UNKNOWN" else self.aircraft_type

    @property
    def ground_speed(self) -> float | None:
        return None if self.velocity is None else self.velocity * 1.9438444924406

    @property
    def speed(self) -> float | None:
        return self.ground_speed

    @property
    def track(self) -> float | None:
        return self.heading
