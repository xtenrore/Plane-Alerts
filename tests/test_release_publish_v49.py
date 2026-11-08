from pathlib import Path


WORKFLOW = Path(".github/workflows/publish-release.yml")
DEPLOY_WORKFLOW = Path(".github/workflows/deploy-railway.yml")
TEST_WORKFLOW = Path(".github/workflows/tests.yml")
COMMUNITY_WORKFLOW = Path(".github/workflows/community-installer.yml")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _deploy_workflow_text() -> str:
    return DEPLOY_WORKFLOW.read_text(encoding="utf-8")


def _test_workflow_text() -> str:
    return TEST_WORKFLOW.read_text(encoding="utf-8")


def _community_workflow_text() -> str:
    return COMMUNITY_WORKFLOW.read_text(encoding="utf-8")


def test_community_release_publish_requires_explicit_weekly_tested_sha():
    workflow = _workflow_text()
    assert "workflow_dispatch:" in workflow
    assert "tested_sha:" in workflow
    assert "Exact main commit that passed the weekly community validation matrix" in workflow
    assert "workflow_run:" not in workflow
    assert "contents: write" in workflow


def test_community_release_publish_uses_exact_tested_sha_and_successful_matrix():
    workflow = _workflow_text()
    tested_sha = "${{ steps.candidate.outputs.sha }}"
    assert f"ref: {tested_sha}" in workflow
    assert f"TESTED_SHA: {tested_sha}" in workflow
    assert '[[ "$MAIN_SHA" == "$TESTED_SHA" ]]' in workflow
    assert "No successful exact-SHA community matrix run exists" in workflow
    assert "automation/community-matrix-" in workflow
    assert "community-validation-summary" in workflow
    assert "Verify exact release candidate checkout" in workflow
    assert "gh run download" in workflowdef test_release_publish_is_version_and_notes_driven():
    workflow = _workflow_text()
    assert "from app.version import VERSION" in workflow
    assert 'TAG="v$VERSION"' in workflow
    assert 'NOTES="docs/releases/$TAG.md"' in workflow
    assert "Canonical VERSION is not semantic version" in workflow
    assert '[[ -s "$NOTES" ]]' in workflow
    assert "requirements.lock" in workflow
    assert ".env.example" in workflow
    assert "docs/self-hosting.md" in workflow
    assert "weekly Community Release workflow" in workflowdef test_release_publish_never_moves_an_existing_tag():
    workflow = _workflow_text()
    assert 'git rev-parse --verify "refs/tags/$TAG^{commit}"' in workflow
    assert '[[ "$TAG_SHA" == "$TESTED_SHA" ]]' in workflow
    assert "Refusing to move existing $TAG" in workflow
    assert "--verify-tag" in workflow
    assert '--target "$TESTED_SHA"' in workflow
    assert 'gh release view "$TAG"' in workflow
    assert "sha256sum -c SHA256SUMS.txt" in workflow
    assert "cmp " in workflow
    assert "latest stable channel" in workflow
    assert "Potential embedded credentials in release assets" in workflowdef test_railway_test_workflow_does_not_run_long_community_platform_matrix():
    workflow = _test_workflow_text()
    assert "  self-hosting:" not in workflow
    assert "docker/setup-qemu-action" not in workflow
    assert "--platform linux/arm64" not in workflow
    assert "Run canonical route guard destination-path regressions" in workflow
    assert "Benchmark canonical non-blocking route guard" in workflow


def test_community_matrix_is_manual_and_automation-branch_only():
    workflow = _community_workflow_text()
    trigger_block = workflow.split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in trigger_block
    assert "tested_sha:" in trigger_block
    assert "push:" in trigger_block
    assert '"automation/community-matrix-*"' in trigger_block
    assert "schedule:" not in trigger_block
    assert "pull_request:" not in trigger_block
    assert "ordinary Railway pushes never trigger it" in trigger_block
    assert "resolve:" in workflow
    assert "windows-arm64:" in workflow
    assert "linux-arm64:" in workflow
    assert "raspberry-pi-arm64-emulation:" in workflow
    assert "mongodb-upgrade:" in workflow
    assert "community-validation-summary:" in workflow
    assert workflow.count("Verify exact release candidate checkout") == 7def test_railway_deploy_runs_only_after_trusted_green_main_push():
    workflow = _deploy_workflow_text()
    assert "workflow_run:" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow
    assert "pull_request_target" not in workflow
    assert "contents: read" in workflow


def test_railway_deploy_gate_runs_outside_railway_container_and_targets_only_main_service():
    workflow = _deploy_workflow_text()
    tested_sha = "${{ github.event.workflow_run.head_sha }}"
    gate_block, deploy_block = workflow.split("\n  deploy:\n", 1)
    assert "  gate:" in gate_block
    assert "runs-on: ubuntu-latest" in gate_block
    assert "container:" not in gate_block
    assert f"DEPLOY_SHA: {tested_sha}" in gate_block
    assert 'gh api "repos/${GITHUB_REPOSITORY}/commits/main" --jq' in gate_block
    assert 'if [[ "${CURRENT_MAIN_SHA}" != "${DEPLOY_SHA}" ]]' in gate_block
    assert 'echo "deploy=false" >> "$GITHUB_OUTPUT"' in gate_block
    assert "git fetch" not in gate_block
    assert "git rev-parse" not in gate_block

    assert "needs: gate" in deploy_block
    assert "if: needs.gate.outputs.deploy == 'true'" in deploy_block
    assert "container: ghcr.io/railwayapp/cli:latest" in deploy_block
    assert f"DEPLOY_SHA: {tested_sha}" in deploy_block
    assert f"ref: {tested_sha}" in deploy_block
    assert '--message "Plane Alerts main ${DEPLOY_SHA}"' in deploy_block
    assert "MAIN_SERVICE_ID" in deploy_block


def test_v490_release_notes_exist_for_automatic_publishing():
    notes = Path("docs/releases/v4.9.0.md")
    assert notes.exists()
    text = notes.read_text(encoding="utf-8")
    assert text.startswith("# Plane Alerts v4.9.0")
    assert "rollback" in text.lower()
