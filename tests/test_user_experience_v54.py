"""Plane Alerts v5.4 user-experience release gates."""
from __future__ import annotations

import inspect

from app.aircraft.registry import CATEGORY_LABELS
from app.bot import next60_web, profile_handlers
from app.bot import profile_experience_v54 as ux
from app.profile_presets_v54 import PRESETS, apply_preset, get_preset
from app.version import PREDICTION_VERSION, VERSION


EXPECTED_PRESETS = {
    "casual": "Casual Observer",
    "photo": "Aircraft Photographer",
    "airport": "Airport-Adjacent",
    "rare": "Rare Aircraft Hunter",
    "military": "Military Watcher",
    "local_sdr": "Local SDR Mode",
}


def _base() -> dict:
    return {
        "location": {"latitude": 41.0123, "longitude": 28.9876, "geohash": "sxk9", "radius_km": 12.0},
        "preferences": {
            "proximity_3d": {"altitude_relevance": True},
            "camera_body": "Canon R7",
            "aircraft_filter": {"mode": "all", "selected_categories": [], "selected_types": [], "excluded_types": []},
            "filter_rules": {"profile": {"radius_km": 9}, "categories": {"cargo": {"enabled": False}}, "aircraft": {}},
        },
    }


def test_release_identity_and_physical_predictor_are_pinned():
    assert VERSION == "5.4.0"
    assert PREDICTION_VERSION == "5.3-3d-proximity-age-aware"


def test_all_six_roadmap_presets_exist_with_supported_categories():
    assert {p.preset_id: p.name for p in PRESETS} == EXPECTED_PRESETS
    for preset in PRESETS:
        assert 1 <= preset.radius_km <= 150
        assert preset.description
        assert preset.all_aircraft or preset.categories
        assert set(preset.categories) <= set(CATEGORY_LABELS)


def test_presets_preserve_coordinates_and_unrelated_preferences():
    for preset in PRESETS:
        config = apply_preset(_base(), preset.preset_id)
        assert config["location"]["latitude"] == 41.0123
        assert config["location"]["longitude"] == 28.9876
        assert config["location"]["geohash"] == "sxk9"
        assert config["location"]["radius_km"] == preset.radius_km
        assert config["preferences"]["proximity_3d"] == {"altitude_relevance": True}
        assert config["preferences"]["camera_body"] == "Canon R7"
        assert config["preferences"]["filter_rules"] == {"profile": {}, "categories": {}, "aircraft": {}}


def test_presets_are_independent_editable_copies():
    first = apply_preset(_base(), "photo")
    second = apply_preset(_base(), "photo")
    first["location"]["radius_km"] = 1
    first["preferences"]["aircraft_filter"]["selected_categories"].append("military")
    assert second["location"]["radius_km"] == get_preset("photo").radius_km
    assert "military" not in second["preferences"]["aircraft_filter"]["selected_categories"]


def test_local_sdr_preset_does_not_claim_to_enable_a_provider():
    config = apply_preset(_base(), "local_sdr")
    flattened = repr(config).casefold()
    assert "local_adsb_url" not in flattened
    assert "provider" not in config["preferences"]
    assert config["preferences"]["aircraft_filter"]["mode"] == "all"


def test_guided_profile_layer_is_installed_before_legacy_handlers():
    assert getattr(profile_handlers, "_v54_installed", False) is True
    source = inspect.getsource(ux.register_v54_handlers)
    assert "group = -40" in source
    assert 'CommandHandler("profiles"' in source
    assert 'CommandHandler("preferences"' in source


def test_profile_navigation_has_quick_custom_back_cancel_close_and_status():
    source = inspect.getsource(ux)
    for label in ("Quick Preset", "Custom Setup", "Back", "Cancel", "Close", "Status", "Save Profile"):
        assert label in source
    assert "One thing needs attention" in source
    assert "Set Location" in source
    assert "Set Radius" in source
    assert "Choose Aircraft" in source


def test_preferences_command_opens_active_profile_editor_not_aircraft_picker():
    source = inspect.getsource(ux.cmd_preferences_v54)
    assert "_start_edit" in source
    assert "_render_aircraft_menu" not in source


def test_restart_commands_recover_profile_state_without_dead_end():
    profiles = inspect.getsource(ux.cmd_profiles_v54)
    preferences = inspect.getsource(ux.cmd_preferences_v54)
    assert "_clear_state" in profiles
    assert "_clear_state" in preferences
    assert "ApplicationHandlerStop" in profiles
    assert "ApplicationHandlerStop" in preferences


def test_new_callback_data_and_button_labels_fit_telegram():
    for preset in PRESETS:
        assert len(f"ux54:picknew:{preset.preset_id}".encode()) <= 64
        assert len(f"ux54:confirm:ffffffffff:{preset.preset_id}".encode()) <= 64
        assert len(preset.name) <= 24


def test_next60_mini_app_uses_plane_alerts_branding():
    assert "Plane Alerts · Next 60" in next60_web.NEXT60_HTML
    assert "PLANE ALERTS · FORECAST" in next60_web.NEXT60_HTML
    assert "Plane? Telegram bot" not in next60_web.NEXT60_HTML


def test_preset_module_has_no_prediction_or_network_dependency():
    source = inspect.getsource(__import__("app.profile_presets_v54", fromlist=["*"]))
    assert "http" not in source.casefold()
    assert "predict_trajectory" not in source
    assert "provider_manager" not in source
