"""Human-friendly, deterministic Plane Alerts v5.4 profile presets.

Presets only populate configuration fields that already exist in the profile
schema. They never alter prediction logic and never enable a provider or other
runtime service. Saved coordinates are preserved unless a caller explicitly
chooses otherwise, and every preset remains fully editable after application.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.aircraft.filtering import normalized_filter_config


@dataclass(frozen=True, slots=True)
class ProfilePreset:
    preset_id: str
    name: str
    description: str
    radius_km: float
    categories: tuple[str, ...] = ()
    all_aircraft: bool = False


PRESETS: tuple[ProfilePreset, ...] = (
    ProfilePreset(
        "casual",
        "Casual Observer",
        "A balanced nearby-aircraft setup for everyday spotting.",
        15.0,
        ("widebody", "narrowbody", "regional_jet", "turboprop"),
    ),
    ProfilePreset(
        "photo",
        "Aircraft Photographer",
        "Prioritizes visually interesting airliners, cargo, business jets and rarer types.",
        30.0,
        ("widebody", "cargo", "business_jet", "classic_rare"),
    ),
    ProfilePreset(
        "airport",
        "Airport-Adjacent",
        "A tighter radius for people spotting close to an airport.",
        10.0,
        all_aircraft=True,
    ),
    ProfilePreset(
        "rare",
        "Rare Aircraft Hunter",
        "Wider coverage focused on classic, rare, cargo and military aircraft.",
        60.0,
        ("classic_rare", "cargo", "military"),
    ),
    ProfilePreset(
        "military",
        "Military Watcher",
        "Wide-area monitoring focused on aircraft classified as military.",
        80.0,
        ("military",),
    ),
    ProfilePreset(
        "local_sdr",
        "Local SDR Mode",
        "Broad all-aircraft alerts suited to a self-hosted local ADS-B receiver deployment.",
        40.0,
        all_aircraft=True,
    ),
)

_BY_ID = {preset.preset_id: preset for preset in PRESETS}


def get_preset(preset_id: str) -> ProfilePreset | None:
    return _BY_ID.get(str(preset_id or "").strip())


def apply_preset(
    base_config: dict[str, Any] | None,
    preset_id: str,
    *,
    preserve_location: bool = True,
) -> dict[str, Any]:
    """Return an independent profile config with understandable preset defaults.

    Unrelated preferences (photography settings, 3D preference, etc.) are kept.
    Advanced filter overrides are reset because they would make a selected
    preset behave differently from its visible description.
    """
    preset = get_preset(preset_id)
    if preset is None:
        raise KeyError(f"unknown profile preset: {preset_id}")

    config = deepcopy(base_config or {})
    location = dict(config.get("location") or {}) if preserve_location else {}
    location["radius_km"] = float(preset.radius_km)
    config["location"] = location

    prefs = dict(config.get("preferences") or {})
    existing = normalized_filter_config(prefs)
    prefs["aircraft_filter"] = {
        "mode": "all" if preset.all_aircraft else "selected",
        "selected_categories": [] if preset.all_aircraft else list(preset.categories),
        "selected_types": [],
        "excluded_types": [],
    }
    # Do not carry hidden per-aircraft/category overrides into a human-readable
    # preset. Other unrelated preferences remain intact.
    prefs["filter_rules"] = {"profile": {}, "categories": {}, "aircraft": {}}
    # Remove legacy selector fields only when they conflict with the canonical
    # v4.3 filter representation. They are materialized from active profiles by
    # the existing profile engine when needed.
    if existing.get("mode") != prefs["aircraft_filter"]["mode"]:
        prefs.pop("disabled_types", None)
    config["preferences"] = prefs
    return config


def preset_summary(preset: ProfilePreset) -> str:
    if preset.all_aircraft:
        aircraft = "All aircraft"
    else:
        aircraft = ", ".join(part.replace("_", " ").title() for part in preset.categories)
    return f"{preset.radius_km:g} km · {aircraft}"
