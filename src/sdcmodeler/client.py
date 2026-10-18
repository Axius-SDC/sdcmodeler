"""
A framework-free client for the SDCStudio API.

httpx and nothing else. Authentication is ``Authorization: Token sdcs_...``
(an API key from Settings, API Keys), read from ``SDCSTUDIO_API_KEY`` when not
given. The public catalog needs no key. Every money-spending call goes through
the server's wallet and key policy, which the client reports faithfully: a 402
is ``InsufficientCredits`` with the numbers in credits, a 403 with
``error_type`` ``key_scope`` or ``key_spend_limit`` is ``KeyPolicyRefused``, a
409 on assembly (a retired component) is ``AssemblyRefused``. The client never
funds a wallet: that is a person's act at the wallet page.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from sdcmodeler import pricing

DEFAULT_BASE_URL = "https://sdcstudio.axius-sdc.com"
KEY_PREFIX_SHOWN = 12   # "sdcs_" and the first seven hex characters, as the key list shows them


class SDCStudioError(Exception):
    """An SDCStudio response the client could not use; ``status`` and ``body`` carry what the server said."""

    def __init__(self, status: int, body: Any, message: str = ""):
        self.status = status
        self.body = body
        super().__init__(message or f"SDCStudio answered {status}: {_short(body)}")


class InsufficientCredits(SDCStudioError):
    """402: the wallet cannot pay. ``estimated_cost_credits`` and ``balance_credits`` are what to tell the person."""

    def __init__(self, status: int, body: Any):
        b = body if isinstance(body, dict) else {}
        self.estimated_cost_credits = b.get("estimated_cost_credits")
        self.balance_credits = b.get("balance_credits")
        super().__init__(status, body, b.get("error") or "Insufficient credits.")


class KeyPolicyRefused(SDCStudioError):
    """403 with ``error_type`` ``key_scope`` or ``key_spend_limit``: the key, not the wallet, said no."""

    def __init__(self, status: int, body: Any):
        b = body if isinstance(body, dict) else {}
        self.error_type = b.get("error_type", "")
        super().__init__(status, body, b.get("error") or "The API key may not make this call.")


class AssemblyRefused(SDCStudioError):
    """409 on assembly: a retired component in the tree; the message names it."""


def _short(body: Any, n: int = 200) -> str:
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    return text[:n]


def _body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


class SDCStudioClient:
    """One SDCStudio, one key (or none), one httpx client."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        http: httpx.Client | None = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or os.environ.get("SDCSTUDIO_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("SDCSTUDIO_API_KEY")
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Token {self.api_key}"
        self._http = http or httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)
        if http is not None and self.api_key and "Authorization" not in http.headers:
            http.headers["Authorization"] = f"Token {self.api_key}"

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ plumbing

    def _raise(self, resp: httpx.Response) -> None:
        if resp.is_success:
            return
        body = _body(resp)
        if resp.status_code == 402:
            raise InsufficientCredits(resp.status_code, body)
        if resp.status_code == 403 and isinstance(body, dict) and body.get("error_type") in ("key_scope", "key_spend_limit"):
            raise KeyPolicyRefused(resp.status_code, body)
        raise SDCStudioError(resp.status_code, body)

    def _get(self, path: str, **params: Any) -> Any:
        resp = self._http.get(path, params={k: v for k, v in params.items() if v not in (None, "")})
        self._raise(resp)
        return _body(resp)

    def _post(self, path: str, payload: dict | None = None) -> Any:
        resp = self._http.post(path, json=payload or {})
        self._raise(resp)
        return _body(resp)

    def _fetch_bytes(self, path: str) -> tuple[bytes, str]:
        """GET a file route; follow the ``{"download_url"}`` pointer production answers for cloud storage, without the key."""
        resp = self._http.get(path)
        self._raise(resp)
        if resp.headers.get("content-type", "").startswith("application/json"):
            data = _body(resp)
            if isinstance(data, dict) and data.get("download_url"):
                request = self._http.build_request("GET", data["download_url"])
                request.headers.pop("authorization", None)
                stored = self._http.send(request, follow_redirects=True)
                stored.raise_for_status()
                return stored.content, data.get("filename") or Path(str(data["download_url"])).name
            raise SDCStudioError(resp.status_code, data, "Expected a file, got JSON without a download_url.")
        disposition = resp.headers.get("content-disposition", "")
        name = disposition.split("filename=")[-1].strip('"; ') if "filename=" in disposition else Path(path.rstrip("/")).name
        return resp.content, name

    # ------------------------------------------------------------------ catalog (public)

    def search_components(self, query: str = "", *, type: str | None = None, project: str | None = None,
                          page: int = 1, page_size: int = 50) -> dict[str, Any]:
        """Published components in the public catalog: ``{"count", "results": [{ct_id, label, description, component_type, project_name, reuse_ref, ...}]}``."""
        return self._get("/api/v1/catalog/components/", search=query, type=type, project=project, page=page, page_size=page_size)

    def list_models(self, query: str = "", *, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        """Published models in the public catalog."""
        return self._get("/api/v1/catalog/dms/", search=query, page=page, page_size=page_size)

    def get_model(self, ct_id: str) -> dict[str, Any]:
        """One published model: title, description, project, its components, and ``artifacts`` (type to route)."""
        return self._get(f"/api/v1/catalog/dm/{ct_id}/")

    def download_artifact(self, ct_id: str, kind: str, dest_dir: str | Path) -> Path:
        """Save one artifact of a published model (``xsd``, ``xml``, ``json``, ``jsonld``, ``html``, ``ttl``, ``shacl``, ``gql``, ``zip``) and return its path."""
        content, name = self._fetch_bytes(f"/api/v1/catalog/dm/{ct_id}/{kind}/")
        dest = Path(dest_dir); dest.mkdir(parents=True, exist_ok=True)
        path = dest / name
        path.write_bytes(content)
        return path

    # ------------------------------------------------------------------ the modeler (key)

    def modeler(self) -> dict[str, Any]:
        """The key's Modeler: ``project_ct_id`` (the default project), ``projects``, preferences."""
        return self._get("/api/v1/auth/modeler/")

    def set_default_project(self, project_ct_id: str) -> dict[str, Any]:
        resp = self._http.patch("/api/v1/auth/modeler/update/", json={"project_ct_id": project_ct_id})
        self._raise(resp)
        return _body(resp)

    def create_project(self, name: str, description: str = "") -> dict[str, Any]:
        return self._post("/api/v1/dmgen/projects/", {"name": name, "description": description})

    def ensure_default_project(self, name: str = "Agent models") -> str:
        """The Modeler's default project id, creating one when there is none (the PRD's 3.1.4, done client-side)."""
        m = self.modeler()
        if m.get("project_ct_id"):
            return m["project_ct_id"]
        project = self.create_project(name, "Models assembled by an agent through sdcmodeler.")
        self.set_default_project(project["ct_id"])
        return project["ct_id"]

    def wallet(self) -> dict[str, Any]:
        """``balance_credits`` and the auto-reload settings. Credits, at 1,000 per dollar."""
        return self._get("/api/v1/wallet/")

    def key_policy(self) -> dict[str, Any] | None:
        """This key's own row from the key list (scopes, spend_limit_credits, spent_credits, expires_at), or None without a key."""
        if not self.api_key:
            return None
        prefix = self.api_key[:KEY_PREFIX_SHOWN]
        for row in self._get("/api/v1/auth/api-tokens/").get("tokens", []):
            if row.get("prefix") == prefix:
                return row
        return None

    # ------------------------------------------------------------------ building

    def estimate(self, tree: dict[str, Any], title: str = "") -> dict[str, Any]:
        """What the assembly would reuse, mint and cost, computed locally (``sdcmodeler.pricing``)."""
        return pricing.estimate_tree(tree, title)

    def assemble(self, title: str, tree: dict[str, Any], *, description: str = "",
                 contextual: dict | None = None, allow_retired: bool = False) -> dict[str, Any]:
        """
        POST ``/api/v1/dmgen/assemble/``.

        Every leaf existing: the server assembles, publishes and generates
        synchronously and answers 200 with the model (``mode`` ``"reuse"``).
        Some leaves new: 202 with ``task_id`` and ``data_source_ct_id`` to poll
        with ``job_status`` (``mode`` ``"mixed"``; the result is a draft model).
        """
        payload: dict[str, Any] = {"title": title, "description": description, "data": tree}
        if contextual:
            payload["contextual"] = contextual
        if allow_retired:
            payload["allow_retired"] = True
        resp = self._http.post("/api/v1/dmgen/assemble/", json=payload)
        if resp.status_code == 409:
            raise AssemblyRefused(409, _body(resp), (_body(resp) or {}).get("error", "Assembly refused.") if isinstance(_body(resp), dict) else "Assembly refused.")
        self._raise(resp)
        data = _body(resp)
        data = dict(data) if isinstance(data, dict) else {"result": data}
        data["mode"] = "mixed" if resp.status_code == 202 else "reuse"
        return data

    def job_status(self, data_source_ct_id: str) -> dict[str, Any]:
        """The pipeline's status for a mixed assembly or an upload (``status``: uploaded, parsing, parsed, agent_processing, modeling, completed, error, agent_error)."""
        return self._get(f"/api/v1/uploader/api/data-sources/{data_source_ct_id}/workflow_status/")

    def publish_model(self, ct_id: str) -> dict[str, Any]:
        return self._post(f"/api/v1/dmgen/dms/{ct_id}/publish/")

    def generate_package(self, ct_id: str) -> dict[str, Any]:
        """Build the published model's package (free; synchronous)."""
        return self._post(f"/api/v1/dmgen/dms/{ct_id}/generate/")

    def download_package(self, ct_id: str, dest_dir: str | Path) -> Path:
        content, name = self._fetch_bytes(f"/api/v1/dmgen/dms/{ct_id}/download_package/")
        dest = Path(dest_dir); dest.mkdir(parents=True, exist_ok=True)
        path = dest / name
        path.write_bytes(content)
        return path

    def generate_app(self, ct_id: str, profile: str = "lightweight") -> dict[str, Any]:
        """Generate the application (free; synchronous). ``profile`` is ``lightweight`` or ``enterprise``."""
        if profile not in ("lightweight", "enterprise"):
            raise ValueError("profile must be 'lightweight' or 'enterprise'")
        return self._post(f"/api/v1/dmgen/dms/{ct_id}/generate_app/", {"profile": profile})

    def download_app(self, ct_id: str, dest_dir: str | Path, profile: str = "lightweight") -> tuple[Path, bool | None]:
        """
        Save the generated application and return ``(path, charged)``.

        The first download of a model by a user costs 1,500 credits; later
        downloads of the same model are free (SDCStudio 4.16.0). ``charged`` is
        what the server reported, or None when it streamed the file without saying.
        """
        route = "download_app" if profile == "lightweight" else "download_enterprise_app"
        resp = self._http.get(f"/api/v1/dmgen/dms/{ct_id}/{route}/")
        self._raise(resp)
        charged: bool | None = None
        if resp.headers.get("content-type", "").startswith("application/json"):
            data = _body(resp)
            charged = data.get("charged") if isinstance(data, dict) else None
            request = self._http.build_request("GET", data["download_url"])
            request.headers.pop("authorization", None)
            stored = self._http.send(request, follow_redirects=True)
            stored.raise_for_status()
            content, name = stored.content, data.get("filename") or f"app-{ct_id}.zip"
        else:
            content, name = resp.content, f"app-{ct_id}-{profile}.zip"
        dest = Path(dest_dir); dest.mkdir(parents=True, exist_ok=True)
        path = dest / name
        path.write_bytes(content)
        return path, charged
