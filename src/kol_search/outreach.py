from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from kol_search.db import Store
from kol_search.settings import Settings


DM_TEMPLATES = {
    "launchvibes_invitation": (
        "Hi {display_name} — we’re inviting selected creators to try LaunchVibes, "
        "a creator workflow product from Florus. You can take a look here: {launchvibes_url}"
    ),
    "creator_growth_invitation": (
        "Hi {display_name} — we’d like to invite you to the Florus Creator Growth Program, "
        "powered by LaunchVibes. Details: {launchvibes_url}"
    ),
}


def render_dm_template(template_key: str, account: dict[str, Any], launchvibes_url: str) -> str:
    template = DM_TEMPLATES.get(template_key)
    if template is None:
        raise ValueError("Unknown DM template")
    username = str(account.get("username") or "").strip().lstrip("@")
    display_name = str(account.get("name") or "").strip() or username
    if not username:
        raise ValueError("Recipient X username is required")
    return template.format(
        display_name=display_name,
        username=username,
        launchvibes_url=launchvibes_url,
    )


def sanitize_provider_error(value: object) -> str:
    text = re.sub(
        r"(?i)(bearer|token|secret|password|cookie|authorization)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        str(value),
    )
    return text.strip()[-500:] or "Unknown provider error"


def official_dm_readiness(settings: Settings) -> tuple[bool, str]:
    if not all(
        (
            settings.x_api_key,
            settings.x_api_secret,
            settings.x_access_token,
            settings.x_access_token_secret,
        )
    ):
        return False, "Official X DM requires OAuth user-context credentials"
    return (
        False,
        "OAuth credentials are present, but DM write permission and the sender identity "
        "have not been verified; real DM sending remains disabled",
    )


@dataclass
class DMSendResult:
    status: str
    provider: str
    receipt: str | None = None
    error: str | None = None


class DMBatchProcessor:
    """Process one durable DM task at a time; real X DM remains explicitly unavailable."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        *,
        sender: Callable[[dict[str, Any]], DMSendResult] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.sender = sender

    def process_one(self) -> bool:
        message = self.store.claim_next_dm_message(
            dm_window_days=self.settings.outreach_dm_window_days
        )
        if not message:
            return False
        if message["status"] != "sending":
            return True
        sender = self.store.get_x_sender(int(message["sender_account_id"]))
        if not sender or not sender["enabled"]:
            self.store.finish_dm_message(
                int(message["id"]), status="failed", provider="blocked",
                sanitized_error="Selected sender is disabled",
            )
            return True
        try:
            if self.sender:
                result = self.sender(message)
            elif sender["send_method"] == "mock":
                result = DMSendResult(
                    status="skipped",
                    provider="dry_run",
                    receipt=json.dumps(
                        {"dry_run": True, "recipient": message["recipient_handle"]},
                        ensure_ascii=False,
                    ),
                )
            elif sender["send_method"] == "official_x_dm":
                _ready, reason = official_dm_readiness(self.settings)
                result = DMSendResult(
                    status="failed", provider="official_x_dm", error=f"BLOCKED: {reason}"
                )
            else:
                result = DMSendResult(
                    status="failed",
                    provider=str(sender["send_method"]),
                    error="Selected send method cannot send targeted X DMs",
                )
            if result.status not in {"sent", "failed", "skipped", "confirmation_required"}:
                raise ValueError("DM sender returned an invalid status")
            self.store.finish_dm_message(
                int(message["id"]),
                status=result.status,
                provider=result.provider,
                provider_receipt=result.receipt,
                sanitized_error=sanitize_provider_error(result.error) if result.error else None,
            )
        except Exception as exc:
            self.store.finish_dm_message(
                int(message["id"]), status="failed", provider="processor",
                sanitized_error=sanitize_provider_error(exc),
            )
        return True


def verify_opencli_sender(
    settings: Settings,
    sender: dict[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str]:
    profile = str(sender.get("opencli_profile") or "").strip()
    if not profile:
        return False, "Sender has no OpenCLI profile"
    if not shutil.which(settings.opencli_command) and "/" not in settings.opencli_command:
        return False, f"OpenCLI command not found: {settings.opencli_command}"
    try:
        result = runner(
            [
                settings.opencli_command, "--profile", profile,
                "twitter", "whoami", "-f", "json",
            ],
            capture_output=True,
            text=True,
            timeout=settings.opencli_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, sanitize_provider_error(exc)
    if result.returncode != 0:
        return False, sanitize_provider_error(result.stderr or result.stdout)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False, "OpenCLI whoami returned invalid JSON"
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    actual = str(payload.get("username") or payload.get("user") or "").lstrip("@")
    expected = str(sender.get("x_handle") or "").lstrip("@")
    if not actual:
        return False, "OpenCLI whoami did not return an X handle"
    if actual.casefold() != expected.casefold():
        return False, f"OpenCLI is logged in as @{actual}, expected @{expected}"
    return True, f"Verified @{actual}"


def validate_comment(
    *,
    post_text: str,
    draft: str | None,
    suitable: bool,
    style: str,
    sender_type: str,
    allowed_claims: list[str],
    forbidden_terms: list[str],
) -> tuple[bool, str]:
    text = (draft or "").strip()
    if not suitable:
        return False, "Generator marked the post unsuitable"
    if not text:
        return False, "Generated reply is empty"
    if len(text) > 280:
        return False, "Reply exceeds 280 characters"
    lowered = text.casefold()
    forbidden = [value.casefold() for value in forbidden_terms if value.strip()]
    if any(value in lowered for value in forbidden):
        return False, "Reply contains forbidden wording"
    if style not in {"brand", "conversational"}:
        return False, "Invalid comment style"
    if style == "brand" and sender_type not in {"brand", "founder", "employee"}:
        return False, "Sender type is not allowed for brand comments"
    conversational_types = {"founder", "employee", "creator_partner", "community"}
    if style == "conversational" and sender_type not in conversational_types:
        return False, "Sender type is not allowed for conversational comments"
    deceptive = (
        "not affiliated", "just a random user", "independent user", "no connection to",
        "i use launchvibes every day", "users love it", "guaranteed results",
    )
    if any(value in lowered for value in deceptive):
        return False, "Reply implies a false identity, experience, testimonial, or result"
    if style == "conversational":
        # Auto-published conversational promotion must make the configured
        # relationship visible; uncertain wording stays in manual review.
        affiliation_markers = {
            "founder": (
                "founder", "i founded", "we built", "we're building",
                "we are building", "our product", "our team", "at launchvibes",
                "affiliated with",
            ),
            "employee": (
                "i work at", "i work with", "we're building", "we are building",
                "our product", "our team", "at launchvibes", "affiliated with",
            ),
            "creator_partner": (
                "creator partner", "partner with", "partnered with",
                "working with", "affiliated with",
            ),
            "community": (
                "launchvibes community", "community account", "community team",
                "working with", "affiliated with",
            ),
        }
        if not any(marker in lowered for marker in affiliation_markers[sender_type]):
            return False, "Conversational reply does not clearly disclose the sender affiliation"

    product_use = re.compile(
        r"\b(?:i|we|i['’]ve|we['’]ve|i have|we have|our team)\s+"
        r"(?:(?:have\s+)?(?:used|tried|tested)|(?:have\s+)?been\s+using|"
        r"use|using|rely on|love)\b",
        re.I,
    )
    if "launchvibes" in lowered and product_use.search(text):
        return False, "Reply contains an unsupported product-use claim"

    allowed = [" ".join(value.casefold().split()) for value in allowed_claims if value.strip()]
    high_risk = re.compile(
        r"\b(?:users?|customers?|creators?|people)\s+"
        r"(?:love|prefer|recommend|report|say|see|achieve)|"
        r"\b(?:trusted by|used by|popular with|everyone loves|thousands of|millions of)|"
        r"\b(?:boosts?|increases?|improves?|doubles?|triples?|guarantees?|delivers?|"
        r"drives?|saves?|reduces?|cuts?)\s+(?:\w+\s+){0,4}"
        r"(?:growth|engagement|reach|results?|revenue|followers?|conversion|productivity|time)\b",
        re.I,
    )
    for sentence in re.split(r"(?<=[.!?])\s+|[;\n]+", text):
        normalized_sentence = " ".join(sentence.casefold().split())
        if high_risk.search(sentence) and not any(
            claim in normalized_sentence for claim in allowed
        ):
            return False, "Reply contains an unsupported testimonial, social-proof, or outcome claim"
    original_tokens = {
        value for value in re.findall(r"[a-z0-9]{4,}", post_text.casefold())
        if value not in {"that", "this", "with", "from", "your", "have", "about"}
    }
    draft_tokens = set(re.findall(r"[a-z0-9]{4,}", lowered))
    overlap = original_tokens & draft_tokens
    required_overlap = 2 if len(original_tokens) >= 4 else 1
    if original_tokens and len(overlap) < required_overlap:
        return False, "Reply is not grounded in the original post"
    numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", text))
    evidence = f"{post_text} {' '.join(allowed_claims)}"
    if any(number not in evidence for number in numbers):
        return False, "Reply contains an unsupported numeric claim"
    return True, "Validated"
