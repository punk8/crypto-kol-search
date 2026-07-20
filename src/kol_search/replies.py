from __future__ import annotations

from kol_search.db import Store
from kol_search.outreach import sanitize_provider_error, verify_opencli_sender
from kol_search.settings import Settings
from kol_search.twitter.base import TwitterBackendError
from kol_search.twitter.reply import (
    ReplyConfirmationRequiredError,
    ReplyPublishRequest,
    ReplyPublishResult,
    TwitterReplyClient,
    create_twitter_reply_client,
)


class ReplyOpportunityError(RuntimeError):
    """Raised when a reply opportunity cannot be published safely."""


class ReplyPublisher:
    """Publish a queued reply through an injected provider and persist its receipt."""

    def __init__(self, store: Store, client: TwitterReplyClient, actor_handle: str) -> None:
        self.store = store
        self.client = client
        self.actor_handle = actor_handle.strip().lstrip("@")
        if not self.actor_handle:
            raise ValueError("actor_handle is required")

    def publish(self, opportunity_id: int, *, text: str | None = None) -> ReplyPublishResult:
        opportunity = self.store.get_reply_opportunity(opportunity_id)
        if not opportunity:
            raise KeyError(opportunity_id)
        if opportunity["status"] not in {"validated", "publishing"}:
            raise ReplyOpportunityError(
                f"Reply opportunity {opportunity_id} is not ready to publish."
            )
        reply_text = (text if text is not None else opportunity.get("draft") or "").strip()
        request = ReplyPublishRequest(
            target_post_id=str(opportunity["post_id"]),
            target_post_url=str(opportunity["post_url"]),
            text=reply_text,
            actor_handle=self.actor_handle,
            idempotency_key=f"reply-opportunity:{opportunity_id}",
            metadata={"opportunity_id": opportunity_id},
        )
        try:
            result = self.client.publish_reply(request)
        except ReplyConfirmationRequiredError:
            self.store.update_reply_opportunity(
                opportunity_id,
                status="confirmation_required",
                draft=request.text,
            )
            raise
        except Exception as exc:
            self.store.update_reply_opportunity(
                opportunity_id,
                status="failed",
                draft=request.text,
                sanitized_error=sanitize_provider_error(exc),
            )
            raise
        try:
            if result.target_post_id != request.target_post_id:
                raise ValueError("Reply provider returned a receipt for another target post")
            self.store.update_reply_opportunity(
                opportunity_id,
                status="replied",
                draft=request.text,
                reply_url=result.reply_url,
            )
        except Exception as exc:
            self.store.update_reply_opportunity(
                opportunity_id,
                status="confirmation_required",
                draft=request.text,
                sanitized_error=sanitize_provider_error(exc),
            )
            raise ReplyConfirmationRequiredError(
                "Reply may have been published but its receipt could not be persisted safely",
                submission={"opportunity_id": opportunity_id},
            ) from exc
        return result


def publish_validated_comment(
    store: Store,
    settings: Settings,
    opportunity_id: int,
) -> ReplyPublishResult:
    """Verify the selected sender, reserve the limit slot, and publish one reply."""
    opportunity = store.get_reply_opportunity(opportunity_id)
    if not opportunity:
        raise KeyError(opportunity_id)
    if opportunity["status"] != "validated":
        raise ReplyOpportunityError("Reply must be validated before publishing")
    sender_id = opportunity.get("sender_account_id")
    sender = store.get_x_sender(int(sender_id)) if sender_id else None
    if not sender or not sender["enabled"]:
        raise ReplyOpportunityError("Select an enabled sender account")
    if sender["send_method"] == "mock":
        backend = "mock"
    elif sender["send_method"] == "opencli_reply":
        backend = "opencli"
        verified, reason = verify_opencli_sender(settings, sender)
        if not verified:
            raise ReplyOpportunityError(reason)
    else:
        raise ReplyOpportunityError("Selected sender cannot publish X replies")
    client = create_twitter_reply_client(
        backend, settings, profile=sender.get("opencli_profile")
    )
    try:
        try:
            reserved = store.claim_reply_for_publish(
                opportunity_id,
                sender_id=int(sender["id"]),
                backend=backend,
                daily_limit=int(sender["daily_comment_limit"]),
                comment_window_days=settings.outreach_comment_window_days,
            )
        except ValueError as exc:
            raise ReplyOpportunityError(str(exc)) from exc
        return ReplyPublisher(store, client, sender["x_handle"]).publish(
            opportunity_id, text=reserved["draft"]
        )
    except (ReplyOpportunityError, ReplyConfirmationRequiredError, TwitterBackendError):
        raise
    except Exception as exc:
        raise TwitterBackendError(sanitize_provider_error(exc)) from exc
    finally:
        client.close()
