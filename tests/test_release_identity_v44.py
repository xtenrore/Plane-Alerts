from pathlib import Path

from app.version import PREDICTION_VERSION, VERSION


def test_v490_or_later_has_one_canonical_runtime_and_prediction_identity():
    assert tuple(int(part) for part in VERSION.split(".")) >= (4, 9, 0)
    assert isinstance(PREDICTION_VERSION, str) and PREDICTION_VERSION.strip()
    main = Path("app/main.py").read_text(encoding="utf-8")
    worker = Path("app/worker/v36.py").read_text(encoding="utf-8")
    assert "from app.version import VERSION, COMMIT" in main
    assert "from app.version import COMMIT, VERSION" in worker
    assert '"plane_version": VERSION' in worker
    assert '"plane_commit": COMMIT' in worker


def test_railway_workflow_embeds_exact_tested_commit_and_keeps_agy_out_of_normal_releases():
    workflow = Path(".github/workflows/deploy-railway.yml").read_text(encoding="utf-8")
    assert 'DEPLOY_SHA: ${{ github.event.workflow_run.head_sha }}' in workflow
    assert 'printf \'%s\\n\' "$DEPLOY_SHA" > app/build_commit.txt' in workflow
    assert "v4.2.2" not in workflow
    assert '--message "Plane Alerts main ${DEPLOY_SHA}"' in workflow
    assert '--message "Plane Alerts AGY ${DEPLOY_SHA}"' not in workflow
    assert "AGY_SERVICE_ID" not in workflow


def test_runtime_commit_has_cli_deployment_fallback():
    source = Path("app/version.py").read_text(encoding="utf-8")
    assert 'Path(__file__).with_name("build_commit.txt")' in source
    assert "RAILWAY_GIT_COMMIT_SHA" in source
    assert "SOURCE_COMMIT" in source
