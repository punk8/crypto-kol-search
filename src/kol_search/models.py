from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Account(BaseModel):
    """Normalized Twitter/X user profile."""

    id: str
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


class Post(BaseModel):
    """Normalized tweet/post."""

    id: str
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


class TrendSignal(BaseModel):
    """Native X trend used as one input to topic ranking."""

    name: str
    rank: int = 0
    post_count: int = 0
    url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class DiscoveryEdge(BaseModel):
    """Explainable path: why a candidate was discovered."""

    target_username: str
    source_type: str  # seed | search | mention | expand
    evidence: str
    weight: float = 1.0
    from_username: str | None = None
    query: str | None = None


class ScoreComponents(BaseModel):
    followers: float = 0.0
    engagement: float = 0.0
    recency: float = 0.0
    domain: float = 0.0
    seed_proximity: float = 0.0
    completeness: float = 0.0
    spam_penalty: float = 0.0
    total: float = 0.0


AccountType = Literal["person", "media", "fund", "project", "company", "community", "unknown"]
ContactType = Literal[
    "email", "telegram", "discord", "linkedin", "youtube", "website", "contact_page"
]
ContactStatus = Literal["pending", "verified", "rejected"]


class AccountEnrichment(BaseModel):
    account_type: AccountType = "unknown"
    languages: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    summary: str | None = None
    relevance_score: float = 0.0
    confidence: float = 0.0
    source: Literal["rules", "openai"] = "rules"


class ContactPoint(BaseModel):
    id: int | None = None
    account_id: str
    contact_type: ContactType
    value: str
    url: str | None = None
    source_url: str
    source_kind: str
    evidence: str
    status: ContactStatus = "pending"
    first_seen_at: str | None = None
    last_seen_at: str | None = None


class Candidate(BaseModel):
    account: Account
    domain_score: float = 0.0
    engagement_rate: float = 0.0
    recent_engagement_avg: float = 0.0
    is_seed: bool = False
    score: ScoreComponents = Field(default_factory=ScoreComponents)
    edges: list[DiscoveryEdge] = Field(default_factory=list)
    recent_tweets_sample: list[str] = Field(default_factory=list)
    enrichment: AccountEnrichment = Field(default_factory=AccountEnrichment)
    rank: int | None = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow)

    def to_export_row(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "username": self.account.username,
            "name": self.account.name,
            "followers": self.account.followers_count,
            "verified": self.account.verified,
            "description": self.account.description,
            "domain_score": round(self.domain_score, 4),
            "engagement_rate": round(self.engagement_rate, 6),
            "score_total": round(self.score.total, 4),
            "score_followers": round(self.score.followers, 4),
            "score_engagement": round(self.score.engagement, 4),
            "score_recency": round(self.score.recency, 4),
            "score_domain": round(self.score.domain, 4),
            "score_seed_proximity": round(self.score.seed_proximity, 4),
            "score_completeness": round(self.score.completeness, 4),
            "spam_penalty": round(self.score.spam_penalty, 4),
            "account_type": self.enrichment.account_type,
            "languages": self.enrichment.languages,
            "topics": self.enrichment.topics,
            "summary": self.enrichment.summary,
            "is_seed": self.is_seed,
            "discovery_paths": [
                {
                    "source_type": e.source_type,
                    "from": e.from_username,
                    "evidence": e.evidence,
                    "query": e.query,
                }
                for e in self.edges
            ],
            "url": f"https://x.com/{self.account.username}",
        }


class DomainConfig(BaseModel):
    domain: str
    display_name: str = ""
    seed_file: str = "seeds/crypto_handles.txt"
    keywords: list[str] = Field(default_factory=list)
    mention_query_templates: list[str] = Field(default_factory=list)
    search_max_results_per_query: int = 40
    search_max_queries: int = 12
    expand_top_n: int = 15
    expand_mentions_per_account: int = 20
    max_candidates: int = 200
    recent_tweets_per_account: int = 10
    min_followers: int = 1000
    min_domain_score: float = 0.15
    score_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "followers": 0.3,
            "engagement": 0.25,
            "recency": 0.1,
            "domain": 0.25,
            "seed_proximity": 0.1,
        }
    )
    domain_lexicon: list[str] = Field(default_factory=list)
    topic_aliases: dict[str, list[str]] = Field(default_factory=dict)


class BackendCapabilities(BaseModel):
    user_search: bool = False
    post_search: bool = True
    batch_user_lookup: bool = True
    user_timeline: bool = True
    followings: bool = False
    verified_followers: bool = False
    trends: bool = False
    user_search_page_size: int = 100
    post_search_page_size: int = 100
    batch_user_lookup_size: int = 100
    followings_page_size: int = 20
    verified_followers_page_size: int = 20


JobStatus = Literal[
    "queued", "running", "completed", "completed_with_warnings", "failed", "interrupted"
]
