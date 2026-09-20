from pathlib import Path


WORKFLOW = Path(".github/workflows/publish-release.yml")
DEPLOY_WORKFLOW = Path(".github/workflows/deploy-railway.yml")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _deploy_workflow_text() -> str:
    return DEPLOY_WORKFLOW.read_text(encoding="utf-8")


def test_release_publish_runs_only_after_trusted_green_main_push():
    workflow = _workflow_text()
    assert "workflow_run:" in workflow
    assert "- tests" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow
    assert "pull_request_target" not in workflow
    assert "contents: write" in workflow


def test_release_publish_uses_exact_tested_sha_and_rejects_stale_main():
    workflow = _workflow_text()
    tested_sha = "${{ github.event.workflow_run.head_sha }}"
    assert f"ref: {tested_sha}" in workflow
    assert f"TESTED_SHA: {tested_sha}" in workflow
    assert 'gh api "repos/${GITHUB_REPOSITORY}/commits/main"' in workflow
    assert 'if [[ "${CURRENT_MAIN_SHA}" != "${TESTED_SHA}" ]]' in workflow
    assert 'echo "publish=false" >> "${GITHUB_OUTPUT}"' in workflow


def test_release_publish_is_version_and_notes_driven():
    workflow = _workflow_text()
    assert "from app.version import VERSION" in workflow
    assert 'TAG="v${VERSION}"' in workflow
    assert 'NOTES="docs/releases/${TAG}.md"' in workflow
    assert "Canonical VERSION is not a release semantic version" in workflow
    assert 'if [[ ! -s "${NOTES}" ]]' in workflow


def test_release_publish_never_moves_an_existing_tag():
    workflow = _workflow_text()
    assert 'git rev-parse -q --verify "refs/tags/${TAG}^{commit}"' in workflow
    assert 'if [[ "${TAG_SHA}" != "${TESTED_SHA}" ]]' in workflow
    assert "Refusing to move existing ${TAG}" in workflow
    assert "--verify-tag" in workflow
    assert '--target "${TESTED_SHA}"' in workflow
    assert 'gh release view "${TAG}"' in workflow


def test_railway_deploy_runs_only_after_trusted_green_main_push():
    workflow = _deploy_workflow_text()
    assert "workflow_run:" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow
    assert "pull_request_target" not in workflow
    assert "contents: read" in workflow


def test_railway_deploy_uses_exact_tested_sha_and_refuses_stale_main():
    workflow = _deploy_workflow_text()
    tested_sha = "${{ github.event.workflow_run.head_sha }}"
    assert f"DEPLOY_SHA: {tested_sha}" in workflow
    assert f"ref: {tested_sha}" in workflow
    assert "fetch-depth: 0" in workflow
    assert "git fetch --force --no-tags origin main" in workflow
    assert 'CURRENT_MAIN_SHA="$(git rev-parse refs/remotes/origin/main)"' in workflow
    assert 'if [ "${CURRENT_MAIN_SHA}" != "${DEPLOY_SHA}" ]' in workflow
    assert 'echo "deploy=false" >> "$GITHUB_OUTPUT"' in workflow
    assert workflow.count("if: steps.head.outputs.deploy == 'true'") >= 4
    assert '--message "Plane Alerts main ${DEPLOY_SHA}"' in workflow
    assert '--message "Plane Alerts AGY ${DEPLOY_SHA}"' in workflow


def test_v490_release_notes_exist_for_automatic_publishing():
    notes = Path("docs/releases/v4.9.0.md")
    assert notes.exists()
    text = notes.read_text(encoding="utf-8")
    assert text.startswith("# Plane Alerts v4.9.0")
    assert "rollback" in text.lower()
