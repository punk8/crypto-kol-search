from __future__ import annotations

from kol_search.db import Store
from kol_search.twitter.reply import (
    ReplyConfirmationRequiredError,
    ReplyPublishRequest,
    ReplyPublishResult,
    TwitterReplyClient,
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
        if opportunity["status"] != "pending":
            raise ReplyOpportunityError(
                f"Reply opportunity {opportunity_id} is {opportunity['status']!r}, not pending."
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
        if result.target_post_id != request.target_post_id:
            raise ReplyOpportunityError("Reply provider returned a receipt for another target post.")
        self.store.update_reply_opportunity(
            opportunity_id,
            status="replied",
            draft=request.text,
            reply_url=result.reply_url,
        )
        return result
