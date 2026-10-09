"""GitHub Contents transport for the owner-private, allowlisted DR repository."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = "xtenrore/Plane-Alerts-Private-Repo"
PREFIX = "private-ai-ops/snapshots/"
API = "https://api.github.com"


class GitHubDR:
    def __init__(self, token: str, *, opener=urllib.request.urlopen):
        if not token:
            raise ValueError("GitHub DR token is required")
        self._token = token
        self._open = opener
        self._verified_private = False

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        req = urllib.request.Request(API + path, data=body, method=method,
            headers={"Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
        # Do not include exception text: it can echo a URL or sensitive header.
        try:
            with self._open(req, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError("GitHub DR path not found") from None
            if exc.code in (409, 422):
                raise RuntimeError("GitHub DR write conflict") from None
            raise ConnectionError("GitHub DR request failed") from None
        except Exception:
            raise ConnectionError("GitHub DR unavailable") from None

    def _verify_private(self) -> None:
        if self._verified_private:
            return
        repo = self._request("GET", "/repos/" + REPOSITORY)
        if repo.get("full_name") != REPOSITORY or repo.get("private") is not True:
            raise PermissionError("DR destination identity or privacy mismatch")
        self._verified_private = True

    @staticmethod
    def _path(path: str) -> str:
        if not path.startswith(PREFIX) or ".." in path or not path.endswith(".json"):
            raise ValueError("DR writes must stay inside the dedicated snapshot namespace")
        return "/repos/" + REPOSITORY + "/contents/" + urllib.parse.quote(path, safe="/")

    def get(self, path: str) -> bytes:
        endpoint = self._path(path)
        self._verify_private()
        response = self._request("GET", endpoint)
        if response.get("type") != "file" or response.get("encoding") != "base64":
            raise ValueError("unexpected GitHub DR content type")
        return base64.b64decode(response["content"], validate=False)

    def put(self, path: str, content: bytes) -> None:
        endpoint = self._path(path)
        self._verify_private()
        if len(content) > 2_000_000:
            raise ValueError("GitHub DR file exceeds size limit")
        try:
            existing = self.get(path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != content:
                raise RuntimeError("immutable GitHub DR snapshot conflict")
            return
        self._request("PUT", endpoint, {"message": "Private AI Ops sanitized DR snapshot",
            "content": base64.b64encode(content).decode(), "branch": "main"})
