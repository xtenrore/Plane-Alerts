from pathlib import Path

from app.version import PREDICTION_VERSION, VERSION


def test_v47_has_one_canonical_runtime_version_and_prediction_model():
    assert VERSION == "4.7.0"
    assert PREDICTION_VERSION == "4.7-airport-terminal"
    main = Path("app/main.py").read_text(encoding="utf-8")
    worker = Path("app/worker/v36.py").read_text(encoding="utf-8")
    assert "from app.version import VERSION, COMMIT" in main
    assert "from app.version import COMMIT, VERSION" in worker
    assert '"plane_version": VERSION' in worker
    assert '"plane_commit": COMMIT' in worker


def test_railway_workflow_embeds_exact_tested_commit_and_has_no_stale_release_label():
    workflow = Path(".github/workflows/deploy-railway.yml").read_text(encoding="utf-8")
    assert 'DEPLOY_SHA: ${{ github.event.workflow_run.head_sha }}' in workflow
    assert 'printf \'%s\\n\' "$DEPLOY_SHA" > app/build_commit.txt' in workflow
    assert "v4.2.2" not in workflow
    assert '--message "Plane Alerts main ${DEPLOY_SHA}"' in workflow
    assert '--message "Plane Alerts AGY ${DEPLOY_SHA}"' in workflow


def test_runtime_commit_has_cli_deployment_fallback():
    source = Path("app/version.py").read_text(encoding="utf-8")
    assert 'Path(__file__).with_name("build_commit.txt")' in source
    assert "RAILWAY_GIT_COMMIT_SHA" in source
    assert "SOURCE_COMMIT" in source
