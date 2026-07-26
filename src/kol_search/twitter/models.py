from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class XAccount(BaseModel):
    """Account returned by an X read provider."""

    id: str
    external_id: str | None = None
    source_provider: str = "unknown"
    captured_at: str | None = None
    username: str
    name: str | None = None
    description: str | None = None
    followers_count: int = 0
    following_count: int = 0
    tweet_count: int = 0
    listed_count: int = 0
    verified: bool = False
    created_at: str | None = None
    profile_image_url: str | None = None
    url: str | None = None
    location: str | None = None
    entities: dict[str, Any] = Field(default_factory=dict)
    protected: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def handle(self) -> str:
        return self.username.lstrip("@")


class XTweet(BaseModel):
    """Tweet returned by an X read provider."""

    id: str
    external_id: str | None = None
    source_provider: str = "unknown"
    captured_at: str | None = None
    author_id: str
    author_username: str | None = None
    text: str = ""
    created_at: str | None = None
    like_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    quote_count: int = 0
    view_count: int = 0
    bookmark_count: int = 0
    lang: str | None = None
    url: str | None = None
    conversation_id: str | None = None
    in_reply_to_user_id: str | None = None
    in_reply_to_username: str | None = None
    referenced_post_id: str | None = None
    reference_type: Literal["replied_to", "quoted", "retweeted"] | None = None
    mentioned_usernames: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def engagement(self) -> int:
        return self.like_count + self.retweet_count + self.reply_count + self.quote_count


class XTrend(BaseModel):
    """Native or derived X trend used as one input to X topic ranking."""

    name: str
    direction: str = "other"
    rank: int = 0
    post_count: int = 0
    url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class XReadCapabilities(BaseModel):
    """Fine-grained capabilities exposed by one X read provider."""

    user_search: bool = False
    post_search: bool = True
    batch_user_lookup: bool = True
    user_timeline: bool = True
    post_lookup: bool = False
    followings: bool = False
    verified_followers: bool = False
    trends: bool = False
    user_search_page_size: int = 100
    post_search_page_size: int = 100
    batch_user_lookup_size: int = 100
    followings_page_size: int = 20
    verified_followers_page_size: int = 20
