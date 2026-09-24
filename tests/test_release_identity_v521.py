from app.version import PREDICTION_VERSION, VERSION


def test_v521_release_identity():
    parts = tuple(int(part) for part in VERSION.split("."))
    assert len(parts) in (2, 3)
    major, minor = parts[:2]
    patch = parts[2] if len(parts) == 3 else 0
    assert (major, minor, patch) >= (5, 2, 1)
    if (major, minor) >= (5, 3):
        assert PREDICTION_VERSION == "5.3-3d-proximity-age-aware"
    else:
        assert PREDICTION_VERSION == "5.1-3d-proximity"
