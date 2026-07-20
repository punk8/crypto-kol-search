from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from kol_search.settings import Settings
from kol_search.twitter.base import TwitterBackendError


class ReplyPublishRequest(BaseModel):
    """Provider-neutral request for publishing one public X reply."""

    target_post_id: str = Field(min_length=1)
    target_post_url: str = Field(
        pattern=r"^https://(?:x\.com|twitter\.com)/[^/]+/status/[^/?#]+"
    )
    text: str = Field(min_length=1, max_length=280)
    actor_handle: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_post_id", "text", "actor_handle", "idempotency_key")
    @classmethod
    def strip_required_values(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("actor_handle")
    @classmethod
    def normalize_actor_handle(cls, value: str) -> str:
        return value.lstrip("@")


class ReplyPublishResult(BaseModel):
    """Normalized receipt returned by any reply provider."""

    backend: str
    reply_post_id: str = Field(min_length=1)
    reply_url: str = Field(pattern=r"^https://(?:x\.com|twitter\.com)/[^/]+/status/\d+")
    target_post_id: str = Field(min_length=1)
    published_at: str
    raw: dict[str, Any] = Field(default_factory=dict)


class ReplyBackendUnavailableError(TwitterBackendError):
    """Raised when a read backend has no configured reply adapter yet."""


class ReplyConfirmationRequiredError(TwitterBackendError):
    """The provider submitted a reply but could not recover its durable receipt."""

    def __init__(self, message: str, *, submission: dict[str, Any]) -> None:
        super().__init__(
            message,
            hint="Do not retry automatically; inspect the actor profile and attach the reply URL.",
        )
        self.submission = submission


@runtime_checkable
class TwitterReplyClient(Protocol):
    """Write-only X contract kept separate from the read client."""

    name: str

    def publish_reply(self, request: ReplyPublishRequest) -> ReplyPublishResult:
        """Publish one reply and return a normalized, durable receipt."""
        ...

    def close(self) -> None:
        ...


class MockTwitterReplyClient:
    """Deterministic reply client for tests and local workflow demos."""

    name = "mock"

    def __init__(
        self,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self._receipts: dict[str, ReplyPublishResult] = {}

    def publish_reply(self, request: ReplyPublishRequest) -> ReplyPublishResult:
        existing = self._receipts.get(request.idempotency_key)
        if existing:
            return existing
        digest = hashlib.sha256(
            f"{request.idempotency_key}:{request.target_post_id}:{request.text}".encode()
        ).hexdigest()
        reply_post_id = str(int(digest[:16], 16))
        result = ReplyPublishResult(
            backend=self.name,
            reply_post_id=reply_post_id,
            reply_url=f"https://x.com/{request.actor_handle}/status/{reply_post_id}",
            target_post_id=request.target_post_id,
            published_at=self._now().isoformat(),
            raw={"mock": True},
        )
        self._receipts[request.idempotency_key] = result
        return result

    def close(self) -> None:
        return None


class OpenCliTwitterReplyClient:
    """Publish through OpenCLI Browser Bridge and verify the resulting X post."""

    name = "opencli"

    def __init__(
        self,
        *,
        command: str = "opencli",
        profile: str = "ddd",
        timeout: float = 90.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        now_provider: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        verification_attempts: int = 2,
    ) -> None:
        self.command = command
        self.profile = profile
        self.timeout = timeout
        self._runner = runner
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self.verification_attempts = max(1, verification_attempts)

    def _run(self, *args: str) -> list[dict[str, Any]]:
        if not shutil.which(self.command) and "/" not in self.command:
            raise TwitterBackendError(
                f"OpenCLI command not found: {self.command}",
                hint="Install OpenCLI or set OPENCLI_COMMAND.",
            )
        command = [
            self.command,
            "--profile",
            self.profile,
            "twitter",
            *args,
            "-f",
            "json",
        ]
        try:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TwitterBackendError(
                f"OpenCLI reply timed out after {self.timeout:g}s",
                hint="Keep Chrome and Browser Bridge connected, then inspect X before retrying.",
            ) from exc
        except OSError as exc:
            raise TwitterBackendError(f"OpenCLI failed to start: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown OpenCLI error").strip()[-800:]
            raise TwitterBackendError(
                f"OpenCLI command failed: {detail}",
                status_code=77 if "login" in detail.lower() or "auth" in detail.lower() else None,
                hint="Run `opencli doctor`, connect Browser Bridge, and verify X is logged in.",
            )
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise TwitterBackendError("OpenCLI returned invalid JSON for reply command.") from exc
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise TwitterBackendError("OpenCLI returned an unexpected reply response shape.")
        return [item for item in payload if isinstance(item, dict)]

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(value.split())

    def publish_reply(self, request: ReplyPublishRequest) -> ReplyPublishResult:
        from kol_search.twitter.opencli import OpenCliTwitterClient

        if not request.target_post_id.isdigit() or not re.search(
            r"/status/\d+(?:[/?#]|$)", request.target_post_url
        ):
            raise TwitterBackendError("OpenCLI replies require a real numeric X post ID and URL.")
        with OpenCliTwitterClient._lock:
            submitted = self._run(
                "reply",
                request.target_post_url,
                request.text,
            )
            submission = submitted[0] if submitted else {}
            if str(submission.get("status") or "").lower() != "success":
                message = submission.get("message") or "OpenCLI did not confirm reply submission."
                raise TwitterBackendError(f"OpenCLI reply failed: {message}")

            returned_url = str(submission.get("url") or "")
            returned_id = re.search(r"/status/(\d+)", returned_url)
            if returned_id:
                return ReplyPublishResult(
                    backend=self.name,
                    reply_post_id=returned_id.group(1),
                    reply_url=returned_url,
                    target_post_id=request.target_post_id,
                    published_at=self._now().isoformat(),
                    raw={"submission": submission},
                )

            expected = self._normalized_text(request.text)
            for attempt in range(self.verification_attempts):
                rows = self._run(
                    "tweets",
                    request.actor_handle,
                    "--limit",
                    "20",
                )
                match = next(
                    (
                        row
                        for row in rows
                        if row.get("id")
                        and self._normalized_text(str(row.get("text") or "")) == expected
                    ),
                    None,
                )
                if match:
                    reply_post_id = str(match["id"])
                    return ReplyPublishResult(
                        backend=self.name,
                        reply_post_id=reply_post_id,
                        reply_url=str(
                            match.get("url")
                            or f"https://x.com/{request.actor_handle}/status/{reply_post_id}"
                        ),
                        target_post_id=request.target_post_id,
                        published_at=self._now().isoformat(),
                        raw={"submission": submission, "verification": match},
                    )
                if attempt + 1 < self.verification_attempts:
                    self._sleep(2)

        raise ReplyConfirmationRequiredError(
            "OpenCLI submitted the reply but its post ID was not visible on the actor profile.",
            submission=submission,
        )

    def close(self) -> None:
        return None


def create_twitter_reply_client(
    backend: str,
    settings: Settings,
    *,
    profile: str | None = None,
) -> TwitterReplyClient:
    """Create a write adapter independently from the configured read adapter."""

    name = backend.lower().strip()
    if name == "mock":
        return MockTwitterReplyClient()
    if name == "opencli":
        return OpenCliTwitterReplyClient(
            command=settings.opencli_command,
            profile=profile or settings.opencli_profile,
            timeout=settings.opencli_timeout_seconds,
        )
    if name == "twitterapi_io":
        raise ReplyBackendUnavailableError(
            f"Reply adapter for {name} is not implemented yet.",
            hint=(
                "Implement TwitterReplyClient for this backend; the trend scanner can continue "
                "using its existing read adapter independently."
            ),
        )
    raise ReplyBackendUnavailableError(
        f"Backend {name!r} is not registered for reply publishing.",
        hint="Use mock locally or add a TwitterReplyClient adapter.",
    )
