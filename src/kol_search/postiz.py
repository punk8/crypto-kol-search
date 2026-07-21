from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


class PostizError(RuntimeError):
    """A confirmed Postiz request failure."""


class PostizSubmissionUncertain(PostizError):
    """Postiz may have accepted a submission whose response was not confirmed."""


@dataclass(frozen=True)
class PostizIntegration:
    id: str
    name: str
    identifier: str
    disabled: bool


@dataclass(frozen=True)
class PostizCreateResult:
    post_id: str
    integration_id: str


def sanitize_postiz_error(value: object) -> str:
    text = str(value or "Postiz request failed").strip()
    text = re.sub(
        r"(?i)(authorization|api[_ -]?key|token|secret|cookie)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    return text[-800:] or "Postiz request failed"


def safe_postiz_release_url(value: object) -> str | None:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return text


class PostizClient:
    """Narrow client for Postiz owned-content publishing."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if isinstance(payload, dict):
            payload = payload.get("msg") or payload.get("message") or payload.get("error") or payload
        return sanitize_postiz_error(f"Postiz HTTP {response.status_code}: {payload}")

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        try:
            response = self._client.get(path, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise PostizError(sanitize_postiz_error(exc)) from exc
        if response.is_error:
            raise PostizError(self._error_message(response))
        try:
            return response.json()
        except ValueError as exc:
            raise PostizError("Postiz returned invalid JSON") from exc

    def list_integrations(self) -> list[PostizIntegration]:
        payload = self._get_json("/public/v1/integrations")
        if not isinstance(payload, list):
            raise PostizError("Postiz returned an invalid integrations response")
        output: list[PostizIntegration] = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("id") or not item.get("identifier"):
                continue
            output.append(
                PostizIntegration(
                    id=str(item["id"]),
                    name=str(item.get("name") or item["identifier"]),
                    identifier=str(item["identifier"]),
                    disabled=bool(item.get("disabled")),
                )
            )
        return output

    def create_post(
        self,
        *,
        integration: PostizIntegration,
        content: str,
        publish_mode: str,
        publish_at: str,
    ) -> PostizCreateResult:
        if publish_mode not in {"now", "schedule"}:
            raise ValueError("Invalid Postiz publish mode")
        provider_settings: dict[str, Any] = {}
        if integration.identifier == "x":
            provider_settings["who_can_reply_post"] = "everyone"
        payload = {
            "type": publish_mode,
            "date": publish_at,
            "shortLink": False,
            "tags": [],
            "posts": [
                {
                    "integration": {"id": integration.id},
                    "value": [{"content": content, "image": []}],
                    "settings": provider_settings,
                }
            ],
        }
        try:
            response = self._client.post("/public/v1/posts", json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise PostizSubmissionUncertain(sanitize_postiz_error(exc)) from exc
        if response.is_error:
            raise PostizError(self._error_message(response))
        try:
            result = response.json()
        except ValueError as exc:
            raise PostizSubmissionUncertain("Postiz accepted the request but returned invalid JSON") from exc
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise PostizSubmissionUncertain("Postiz returned an unexpected create-post receipt")
        post_id = result[0].get("postId")
        integration_id = result[0].get("integration")
        if not post_id or str(integration_id) != integration.id:
            raise PostizSubmissionUncertain("Postiz create-post receipt could not be confirmed")
        return PostizCreateResult(post_id=str(post_id), integration_id=str(integration_id))

    def get_post(self, post_id: str, *, start_date: str, end_date: str) -> dict[str, Any] | None:
        payload = self._get_json(
            "/public/v1/posts",
            params={"startDate": start_date, "endDate": end_date},
        )
        posts = payload.get("posts") if isinstance(payload, dict) else payload
        if not isinstance(posts, list):
            raise PostizError("Postiz returned an invalid posts response")
        for item in posts:
            if isinstance(item, dict) and str(item.get("id")) == post_id:
                return item
        return None
