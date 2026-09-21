from app.version import PREDICTION_VERSION, VERSION


def test_v521_release_identity():
    major, minor, patch = (int(part) for part in VERSION.split("."))
    assert (major, minor, patch) >= (5, 2, 1)
    if (major, minor) >= (5, 3):
        assert PREDICTION_VERSION == "5.3-3d-proximity-age-aware"
    else:
        assert PREDICTION_VERSION == "5.1-3d-proximity"
