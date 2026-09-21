from app.version import PREDICTION_VERSION, VERSION


def test_v521_release_identity():
    assert VERSION == "5.2.1"
    assert PREDICTION_VERSION == "5.1-3d-proximity"
