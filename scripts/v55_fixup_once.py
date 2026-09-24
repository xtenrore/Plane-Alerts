from pathlib import Path


def replace_exact(path, old, new):
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"missing expected text in {path}: {old[:80]!r}")
    p.write_text(text.replace(old, new), encoding="utf-8")


for path in ("tests/test_general_audit_fixes_v542.py", "tests/test_user_experience_v54.py"):
    replace_exact(path, 'assert VERSION == "5.4.3"', 'assert VERSION == "5.5.0"')

p = Path("tests/test_shadow_evaluation_v52.py")
text = p.read_text(encoding="utf-8")
start = text.index("@pytest.mark.asyncio\nasync def test_prediction_lab_marks_pass_scoreable_but_cancellation_unresolved")
end = text.index("\n\ndef test_database_declares_bounded_shadow_indexes", start)
replacement = '''@pytest.mark.asyncio
async def test_prediction_lab_marks_pass_scoreable_but_cancellation_unresolved(monkeypatch, tmp_path):
    written = []

    async def _append(doc, **_kwargs):
        written.append(dict(doc))
        return tmp_path / "evidence.json"

    monkeypatch.setattr(prediction_lab_audit, "append_evidence", _append)
    monkeypatch.setattr(prediction_lab_audit, "migration_verified", lambda: True)
    aircraft = SimpleNamespace(icao24="abc123", callsign="TEST1", aircraft_type="A320")
    prediction = SimpleNamespace(
        projected_closest_km=4.0,
        projected_closest_3d_km=4.2,
        projected_closest_3d_lower_bound_km=4.0,
        time_to_cpa_s=0.0,
        time_to_3d_cpa_s=0.0,
        state="Passed",
        confidence="High",
        altitude_relevance_applied=False,
    )
    await prediction_lab_audit.record_prediction_outcome(user_id=1, aircraft=aircraft, outcome="passed", observed_closest_km=4.1, final_prediction=prediction)
    passed = next(doc for doc in written if doc.get("kind") == "outcome")
    assert passed["scoreable"] is True
    assert passed["outcome_basis"] == "observed_in_radius_pass"

    written.clear()
    await prediction_lab_audit.record_prediction_outcome(user_id=1, aircraft=aircraft, outcome="cancelled", observed_closest_km=12.0, final_prediction=prediction)
    cancelled = next(doc for doc in written if doc.get("kind") == "outcome")
    assert cancelled["scoreable"] is False
    assert cancelled["outcome_basis"] == "lifecycle_transition_only"
'''
text = text[:start] + replacement + text[end:]
old_index = '''def test_database_declares_bounded_shadow_indexes():
    source = Path("app/database.py").read_text(encoding="utf-8")
    assert 'db["prediction_shadow_evaluations"].create_index("evaluation_id", unique=True)' in source
    assert 'db["prediction_shadow_evaluations"].create_index("expires_at", expireAfterSeconds=0)' in source'''
new_index = '''def test_database_retires_high_volume_prediction_lab_indexes():
    source = Path("app/database.py").read_text(encoding="utf-8")
    assert 'db["prediction_lab_audit"].create_index' not in source
    assert 'db["prediction_shadow_evaluations"].create_index' not in source
    assert 'db["prediction_sentinel_routes"].create_index' not in source
    assert 'db["users"].create_index("user_id", unique=True)' in source'''
if old_index not in text:
    raise SystemExit("old shadow-index test not found")
p.write_text(text.replace(old_index, new_index), encoding="utf-8")

p = Path("app/sentinel_network.py")
text = p.read_text(encoding="utf-8")
import_anchor = "from app.intelligence.route_history import normalize_flight_key\n"
if "from app.next60_outcomes_v55 import resolve_next60_outcomes\n" not in text:
    if import_anchor not in text:
        raise SystemExit("sentinel import anchor not found")
    text = text.replace(import_anchor, import_anchor + "from app.next60_outcomes_v55 import resolve_next60_outcomes\n")
text = text.replace("last_migration_attempt = time.monotonic(); last_spool_cleanup = 0.0", "last_migration_attempt = time.monotonic(); last_spool_cleanup = 0.0; last_next60_resolution = 0.0")
anchor = "        dynamic = await _refresh_admin_regions(started)\n"
block = '''        if started - last_next60_resolution >= 60.0:
            try:
                outcome_counts = await resolve_next60_outcomes(get_db())
                if outcome_counts.get("resolved"):
                    logger.info("prediction_next60_outcomes %s", outcome_counts)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("prediction_next60_outcome_resolution_failed", exc_info=True)
            last_next60_resolution = started
'''
if "prediction_next60_outcome_resolution_failed" not in text:
    if anchor not in text:
        raise SystemExit("sentinel loop anchor not found")
    text = text.replace(anchor, block + anchor)
p.write_text(text, encoding="utf-8")
