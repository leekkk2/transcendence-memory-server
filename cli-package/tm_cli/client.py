"""HTTP client wrapper around ``httpx`` for the ``tm`` CLI.

* Always sends ``User-Agent: transcendence-memory-cli/<version>`` (defeats
  Cloudflare WAF rule 1010 that blocks the default Python UA).
* Always sends ``X-API-KEY`` header when an API key is configured.
* Maps server errors to typed exceptions so the command layer can render them.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import httpx

from . import __version__
from .config import Settings


USER_AGENT = f"transcendence-memory-cli/{__version__}"
DEFAULT_TIMEOUT = 30.0


class CLIError(RuntimeError):
    """Base error mapped to a non-zero exit code by the command layer."""

    exit_code: int = 1


class AuthError(CLIError):
    exit_code = 2


class ConnectionError_(CLIError):  # noqa: N801 — avoid shadowing built-in
    exit_code = 3


class ServerError(CLIError):
    exit_code = 4

    def __init__(self, message: str, status_code: int, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class Client:
    """Thin facade over ``httpx.Client`` carrying ``tm`` defaults."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        verbose: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.settings = settings
        self.verbose = verbose
        headers: dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        if settings.api_key:
            headers["X-API-KEY"] = settings.api_key
        self._client = httpx.Client(
            base_url=settings.require_endpoint() if settings.endpoint else "",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------ context
    def __enter__(self) -> "Client":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ verbs
    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json_body: Any | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._request(
            "POST",
            path,
            json_body=json_body,
            params=params,
            data=data,
            files=files,
        )

    def put(self, path: str, *, json_body: Any | None = None) -> Any:
        return self._request("PUT", path, json_body=json_body)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    # ------------------------------------------------------------------ core
    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
    ) -> Any:
        if self.verbose:
            print(f"[tm] {method} {path} json={json_body!r} params={params!r}")
        try:
            response = self._client.request(
                method,
                path,
                json=json_body,
                params=params,
                data=data,
                files=files,
            )
        except httpx.TimeoutException as exc:
            raise ConnectionError_(
                f"Request timed out talking to {self.settings.endpoint}. "
                "Check network/firewall."
            ) from exc
        except httpx.ConnectError as exc:
            raise ConnectionError_(
                f"Could not connect to {self.settings.endpoint}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ConnectionError_(f"HTTP transport error: {exc}") from exc

        if self.verbose:
            print(f"[tm] -> {response.status_code} {response.headers.get('content-type')}")

        if response.status_code in (401, 403):
            raise AuthError(
                "Authentication failed. Try `tm connect <token>` to re-authenticate."
            )

        if response.status_code >= 500:
            raise ServerError(
                f"Server error {response.status_code} from {self.settings.endpoint}. "
                "Run `tm status` or check /admin/system-health.",
                status_code=response.status_code,
                body=_safe_text(response),
            )

        if response.status_code >= 400:
            raise ServerError(
                f"Server returned {response.status_code}: {_safe_text(response) or '(no body)'}",
                status_code=response.status_code,
                body=_safe_text(response),
            )

        if not response.content:
            return None
        ctype = response.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                return response.json()
            except json.JSONDecodeError as exc:  # pragma: no cover - defensive
                raise ServerError(
                    f"Server returned non-JSON body: {exc}",
                    status_code=response.status_code,
                    body=_safe_text(response),
                ) from exc
        return response.text


def _safe_text(response: httpx.Response, *, limit: int = 800) -> str:
    try:
        text = response.text
    except Exception:  # pragma: no cover - defensive
        return ""
    return text[:limit]
