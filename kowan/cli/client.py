"""A small typed REST client for the Kowan control plane.

It speaks the same JSON contracts the server exposes under /api/v1,
authenticating with a bearer JWT and/or an admin-managed API key ("pk_...").
It accepts self-signed TLS certificates so the CLI works
against a server using an auto-generated certificate.
"""

from __future__ import annotations

from typing import Any, BinaryIO

import httpx


class APIError(Exception):
    """An error built from a non-2xx server response.

    It prefers the server's JSON
    ``{"error": ...}`` envelope and renders ``METHOD path: message (status)``.
    """


class Client:
    """A typed HTTP client for the Kowan server REST API."""

    def __init__(self, server: str, token: str = "", api_key: str = "") -> None:
        # Normalise the server URL by trimming trailing slashes / whitespace.
        self.server = server.strip().rstrip("/")
        self.token = token or ""
        self.api_key = (api_key or "").strip()
        # verify=False accepts self-signed server certs.
        self._http = httpx.Client(verify=False, timeout=60.0)

    # -- context-manager sugar so callers can `with Client(...) as c:` --
    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- internals --

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def _status_error(self, method: str, path: str, resp: httpx.Response) -> APIError:
        data = resp.content[: 1 << 20]
        msg = ""
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("error"), str):
                msg = body["error"]
        except Exception:
            msg = ""
        if not msg:
            msg = data.decode("utf-8", "replace").strip()
        if not msg:
            msg = resp.reason_phrase or ""
        return APIError(f"{method} {path}: {msg} ({resp.status_code})")

    def _do(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        decode: bool = True,
    ) -> Any:
        """Perform an HTTP request. On non-2xx raise APIError; else decode JSON."""
        headers = self._auth_headers()
        headers["Accept"] = "application/json"
        json_body = None
        if body is not None:
            json_body = body
            headers["Content-Type"] = "application/json"

        try:
            resp = self._http.request(
                method, self.server + path, json=json_body, headers=headers
            )
        except httpx.HTTPError as e:
            raise APIError(f"{method} {path}: {e}") from e

        if not (200 <= resp.status_code < 300):
            raise self._status_error(method, path, resp)

        if not decode:
            return None
        if not resp.content:
            return None
        try:
            return resp.json()
        except Exception as e:
            raise APIError(f"{method} {path}: decode response: {e}") from e

    # --- Auth ---

    def login(self, username: str, password: str) -> str:
        """Authenticate with username/password and return a signed JWT."""
        out = self._do(
            "POST",
            "/api/v1/auth/login",
            {"username": username, "password": password},
        )
        return (out or {}).get("token", "")

    def me(self) -> dict[str, Any]:
        """Return the currently authenticated user."""
        return self._do("GET", "/api/v1/me") or {}

    # --- Hosts ---

    def list_hosts(self) -> list[dict[str, Any]]:
        return self._do("GET", "/api/v1/hosts") or []

    def rename_host(self, id_: str, name: str) -> dict[str, Any]:
        return self._do("PATCH", f"/api/v1/hosts/{id_}", {"name": name}) or {}

    # --- Users ---

    def list_users(self) -> list[dict[str, Any]]:
        return self._do("GET", "/api/v1/users") or []

    # --- Containers ---

    def list_containers(self, host_id: str, all_: bool) -> list[dict[str, Any]]:
        path = f"/api/v1/hosts/{host_id}/containers"
        if all_:
            path += "?all=true"
        return self._do("GET", path) or []

    def container_action(self, host_id: str, id_: str, action: str) -> None:
        path = f"/api/v1/hosts/{host_id}/containers/{id_}/{action}"
        self._do("POST", path, decode=False)

    def container_logs_ws_url(self, host_id: str, id_: str) -> str:
        """The ws(s) URL for a container's log stream."""
        url = self.server + f"/api/v1/hosts/{host_id}/containers/{id_}/logs"
        if url.startswith("https://"):
            return "wss://" + url[len("https://") :]
        if url.startswith("http://"):
            return "ws://" + url[len("http://") :]
        return url

    # --- Images ---

    def list_images(self, host_id: str) -> list[dict[str, Any]]:
        return self._do("GET", f"/api/v1/hosts/{host_id}/images") or []

    # --- Volumes ---

    def list_volumes(self, host_id: str) -> list[dict[str, Any]]:
        return self._do("GET", f"/api/v1/hosts/{host_id}/volumes") or []

    # --- Networks ---

    def list_networks(self, host_id: str) -> list[dict[str, Any]]:
        return self._do("GET", f"/api/v1/hosts/{host_id}/networks") or []

    # --- System ---

    def system_info(self, host_id: str) -> dict[str, Any]:
        return self._do("GET", f"/api/v1/hosts/{host_id}/system") or {}

    # --- Deployments ---

    def list_deployments(self) -> list[dict[str, Any]]:
        return self._do("GET", "/api/v1/deployments") or []

    def get_deployment(self, id_: str) -> dict[str, Any]:
        return self._do("GET", f"/api/v1/deployments/{id_}") or {}

    @staticmethod
    def _deployment_request(dep: dict[str, Any]) -> dict[str, Any]:
        """Project a Deployment onto the create/update request body.

        The server decodes with DisallowUnknownFields, so only these mutable
        fields may be sent — never server-managed ones (id/status/timestamps).
        """
        return {
            "name": dep.get("name", ""),
            "type": dep.get("type", ""),
            "strategy": dep.get("strategy", ""),
            "targets": dep.get("targets") or [],
            "replicas": dep.get("replicas", 0),
            "spec": dep.get("spec"),
            "compose": dep.get("compose", ""),
            # Without it a git deployment would turn into an inline one on update.
            "git": dep.get("git"),
        }

    def create_deployment(self, dep: dict[str, Any]) -> dict[str, Any]:
        return (
            self._do("POST", "/api/v1/deployments", self._deployment_request(dep))
            or {}
        )

    def update_deployment(self, id_: str, dep: dict[str, Any]) -> dict[str, Any]:
        return (
            self._do(
                "PUT",
                f"/api/v1/deployments/{id_}",
                self._deployment_request(dep),
            )
            or {}
        )

    def delete_deployment(self, id_: str) -> None:
        self._do("DELETE", f"/api/v1/deployments/{id_}", decode=False)

    def deployment_revisions(self, id_: str) -> list[dict[str, Any]]:
        return self._do("GET", f"/api/v1/deployments/{id_}/revisions") or []

    def deployment_revision_diff(self, id_: str, n: int, against: int = 0) -> dict[str, Any]:
        q = f"?against={against}" if against else ""
        return self._do("GET", f"/api/v1/deployments/{id_}/revisions/{n}/diff{q}") or {}

    def rollback_deployment(self, id_: str, n: int, deploy: bool = True) -> dict[str, Any]:
        return self._do("POST", f"/api/v1/deployments/{id_}/revisions/{n}/rollback", {"deploy": deploy}) or {}

    def deployment_previews(self, id_: str) -> list[dict[str, Any]]:
        return self._do("GET", f"/api/v1/deployments/{id_}/previews") or []

    def create_preview(self, id_: str, branch: str) -> dict[str, Any]:
        return self._do("POST", f"/api/v1/deployments/{id_}/previews", {"branch": branch}) or {}

    def promotion_plan(self, id_: str, to: str = "") -> dict[str, Any]:
        q = f"?to={to}" if to else ""
        return self._do("GET", f"/api/v1/deployments/{id_}/promote{q}") or {}

    def promote(self, id_: str, to: str = "", deploy: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {"deploy": deploy}
        if to:
            body["to"] = to
        return self._do("POST", f"/api/v1/deployments/{id_}/promote", body) or {}

    def _lifecycle(self, action: str, id_: str) -> dict[str, Any]:
        """Perform a deployment lifecycle action and decode {deployment, error}.

        The server returns this envelope on success and failure; when it
        reports an operation error the deployment record is still returned
        alongside the raised APIError so callers may show its (error) status.
        """
        path = f"/api/v1/deployments/{id_}/{action}"
        out = self._do("POST", path) or {}
        dep = out.get("deployment") or {}
        err = out.get("error", "")
        if err:
            raise APIError(f"{action} deployment: {err}")
        return dep

    def deploy_deployment(self, id_: str) -> dict[str, Any]:
        return self._lifecycle("deploy", id_)

    def redeploy_deployment(self, id_: str) -> dict[str, Any]:
        return self._lifecycle("redeploy", id_)

    def stop_deployment(self, id_: str) -> dict[str, Any]:
        return self._lifecycle("stop", id_)

    def start_deployment(self, id_: str) -> dict[str, Any]:
        return self._lifecycle("start", id_)

    def destroy_deployment(self, id_: str) -> dict[str, Any]:
        return self._lifecycle("destroy", id_)

    def deployment_status(self, id_: str) -> list[dict[str, Any]]:
        return self._do("GET", f"/api/v1/deployments/{id_}/status") or []

    # --- Backup / Restore ---

    def download_backup(self, w: BinaryIO) -> None:
        """Stream a gzip+tar backup from GET /backup and copy it to w."""
        headers = self._auth_headers()
        headers["Accept"] = "application/gzip"
        url = self.server + "/api/v1/backup"
        try:
            with self._http.stream("GET", url, headers=headers) as resp:
                if not (200 <= resp.status_code < 300):
                    # Materialise the body so the error envelope can be read.
                    resp.read()
                    raise self._status_error("GET", "/api/v1/backup", resp)
                for chunk in resp.iter_bytes():
                    w.write(chunk)
        except httpx.HTTPError as e:
            raise APIError(f"GET /api/v1/backup: {e}") from e

    def restore(self, r: BinaryIO) -> None:
        """Upload a backup archive to POST /restore as a multipart 'file' field."""
        headers = self._auth_headers()
        headers["Accept"] = "application/json"
        url = self.server + "/api/v1/restore"
        files = {"file": ("kowan-backup.tar.gz", r, "application/gzip")}
        try:
            resp = self._http.post(url, headers=headers, files=files)
        except httpx.HTTPError as e:
            raise APIError(f"POST /api/v1/restore: {e}") from e
        if not (200 <= resp.status_code < 300):
            raise self._status_error("POST", "/api/v1/restore", resp)

    # --- Workflows ---

    def list_workflows(self) -> list[dict[str, Any]]:
        return self._do("GET", "/api/v1/workflows") or []

    def run_workflow(self, id_: str) -> str:
        out = self._do("POST", f"/api/v1/workflows/{id_}/run") or {}
        return out.get("runId", "")

    def list_workflow_runs(self, id_: str) -> list[dict[str, Any]]:
        return self._do("GET", f"/api/v1/workflows/{id_}/runs") or []

    # --- API keys ---

    def list_api_keys(self) -> list[dict[str, Any]]:
        return self._do("GET", "/api/v1/apikeys") or []

    def create_api_key(self, name: str, role: str) -> dict[str, Any]:
        """Create an API key; returns {apiKey, secret} (secret shown once)."""
        return (
            self._do("POST", "/api/v1/apikeys", {"name": name, "role": role}) or {}
        )

    def delete_api_key(self, id_: str) -> None:
        self._do("DELETE", f"/api/v1/apikeys/{id_}", decode=False)
