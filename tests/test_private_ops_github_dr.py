import io
import json
import urllib.error

import pytest

from app.private_ops.github_dr import GitHubDR


def test_transport_refuses_public_destination_and_unrelated_paths():
    def opener(req, timeout):
        return io.BytesIO(json.dumps({"full_name": "xtenrore/Plane-Alerts-Private-Repo", "private": False}).encode())

    transport = GitHubDR("test-only", opener=opener)
    with pytest.raises(PermissionError, match="privacy"):
        transport.put("private-ai-ops/snapshots/digest.json", b"{}")
    with pytest.raises(ValueError, match="namespace"):
        transport.put("README.md", b"{}")


def test_transport_refuses_existing_immutable_snapshot_conflict():
    def opener(req, timeout):
        if req.full_url.endswith("/repos/xtenrore/Plane-Alerts-Private-Repo"):
            return io.BytesIO(json.dumps({"full_name": "xtenrore/Plane-Alerts-Private-Repo", "private": True}).encode())
        if req.get_method() == "GET":
            return io.BytesIO(json.dumps({"type": "file", "encoding": "base64", "content": "e30="}).encode())
        raise AssertionError("must not overwrite remote snapshot")

    with pytest.raises(RuntimeError, match="immutable"):
        GitHubDR("test-only", opener=opener).put("private-ai-ops/snapshots/digest.json", b'{"changed":true}')
