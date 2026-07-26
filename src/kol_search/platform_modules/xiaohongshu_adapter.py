from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any

from kol_search.discovery.promotion import (
    BalancedPromotionPolicy,
    KolStatus,
    PromotionInput,
)
from kol_search.platform_modules.adapter_utils import (
    keyword_relevance_score,
    log_ratio,
    parse_time,
    resolve_database_path,
    stable_native_id,
    utc_now,
)
from kol_search.platform_modules.content_intelligence import (
    ContentInsight,
    ContentIntelligence,
    NativeContentSample,
    content_intelligence_from_settings,
)
from kol_search.platform_modules.xiaohongshu_native import XiaohongshuRepository
from kol_search.platforms.kernel import (
    NativeObjectReference,
    PlatformActionProposal,
    PlatformOpportunityProposal,
    PlatformProjectionResult,
    PlatformSuggestedAction,
)
from kol_search.platforms.xiaohongshu import (
    XiaohongshuComment,
    XiaohongshuNote,
    XiaohongshuTrend,
    XiaohongshuUser,
)


_XHS_RELEVANCE_TERMS = (
    "crypto",
    "bitcoin",
    "ethereum",
    "defi",
    "web3",
    "blockchain",
    "stablecoin",
    "token",
    "rwa",
    "加密",
    "区块链",
    "链上",
    "数字资产",
)

_XHS_NEGATED_RELEVANCE_PHRASES = (
    "no crypto",
    "not crypto",
    "not about crypto",
    "unrelated to crypto",
    "without crypto",
    "非加密",
    "与加密无关",
    "不是加密",
    "不聊加密",
)


def _classify_relevance(text: str) -> float:
    return keyword_relevance_score(
        text,
        _XHS_RELEVANCE_TERMS,
        negated_phrases=_XHS_NEGATED_RELEVANCE_PHRASES,
    )


class XiaohongshuAutomationAdapter:
    """Owns Xiaohongshu persistence, scoring, promotion, and opportunity proposals."""

    platform_id = "xiaohongshu"

    def __init__(
        self,
        settings: object,
        *,
        repository: XiaohongshuRepository | None = None,
        promotion: BalancedPromotionPolicy | None = None,
        content_intelligence: ContentIntelligence | None = None,
    ) -> None:
        self.settings = settings
        self._database_path = resolve_database_path(settings)
        self._repository = repository
        self.promotion = promotion or BalancedPromotionPolicy(
            review_enabled=bool(getattr(settings, "review_queue_enabled", False))
        )
        self.content_intelligence = (
            content_intelligence or content_intelligence_from_settings(settings)
        )

    @property
    def repository(self) -> XiaohongshuRepository:
        if self._repository is None:
            if self._database_path is None:
                raise RuntimeError("Xiaohongshu automation adapter requires a database path")
            self._repository = XiaohongshuRepository(self._database_path)
        return self._repository

    def project_discovery(
        self,
        items: Iterable[object],
        *,
        payload: Mapping[str, Any],
    ) -> PlatformProjectionResult:
        users, notes, comments, trends = self._split(items)
        notes, _insights, intelligence_warnings = self._enrich_notes(notes, payload)
        with self.repository.unit_of_work():
            self._persist(users, notes, comments, trends)
            self._promote(users, notes, comments)
            self.repository.pause_inactive(
                days=int(getattr(self.settings, "inactive_pause_days", 90))
            )
        return PlatformProjectionResult(
            native_counts={
                "accounts": len(users),
                "content": len(notes),
                "comments": len(comments),
            },
            warnings=intelligence_warnings,
        )

    def project_signals(
        self,
        items: Iterable[object],
        *,
        payload: Mapping[str, Any],
    ) -> PlatformProjectionResult:
        users, notes, comments, trends = self._split(items)
        notes, insights, intelligence_warnings = self._enrich_notes(notes, payload)
        with self.repository.unit_of_work():
            self._persist(users, notes, comments, trends)
            self._promote(users, notes, comments)
            self.repository.pause_inactive(
                days=int(getattr(self.settings, "inactive_pause_days", 90))
            )
            opportunities = tuple(
                proposal
                for proposal in (
                    *(
                        self._content_opportunity(note, insights.get(note.external_id))
                        for note in notes
                    ),
                    *(self._trend_opportunity(trend) for trend in trends),
                )
                if proposal is not None
            )
        return PlatformProjectionResult(
            native_counts={
                "accounts": len(users),
                "content": len(notes),
                "comments": len(comments),
                "trends": len(trends),
            },
            opportunities=opportunities,
            warnings=intelligence_warnings,
            metadata={"content_intelligence_count": len(insights)},
        )

    def list_kols(
        self, *, status: str = "all", limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = self.repository.list_kols(status=status, limit=limit)
        for row in rows:
            row["actionable"] = bool(row.get("native_id_resolved", True))
        return rows

    def list_trends(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.repository.list_recent_trends(limit=limit)

    def get_kol(self, native_id: str) -> dict[str, Any] | None:
        row = self.repository.get_kol(native_id)
        if row is not None:
            row["actionable"] = bool(row.get("native_id_resolved", True))
        return row

    def list_scan_targets(
        self,
        *,
        statuses: Iterable[str],
        after: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        return self.repository.list_scan_targets(
            statuses=tuple(statuses), after=after, limit=limit
        )

    def summary(self) -> Mapping[str, int]:
        return self.repository.summary()

    def set_kol_status(self, native_id: str, status: str) -> None:
        current = self.repository.get_kol(native_id)
        if current is None:
            raise KeyError(native_id)
        self.repository.set_kol_status(
            native_id,
            KolStatus(status),
            score=float(current["score"]),
            reasons=tuple(current.get("reasons") or ()),
        )

    def add_seed(self, native_id: str, display_name: str | None = None) -> None:
        normalized = native_id.strip()
        if not normalized:
            raise ValueError("Xiaohongshu seed ID cannot be empty")
        if normalized.startswith("unresolved:"):
            raise ValueError("Xiaohongshu seed requires a native user ID")
        current = self.repository.get_kol(normalized)
        if current is None:
            self.repository.upsert_user(
                XiaohongshuUser(
                    external_id=normalized,
                    nickname=(display_name or "").strip() or normalized,
                    profile_url=f"https://www.xiaohongshu.com/user/profile/{normalized}",
                    source_provider="manual_seed",
                )
            )
            current = self.repository.get_kol(normalized)
            assert current is not None
        self.repository.set_kol_status(
            normalized,
            KolStatus.SEED,
            score=float(current.get("score") or 0),
            reasons=("curated seed",),
        )

    @staticmethod
    def _split(
        items: Iterable[object],
    ) -> tuple[
        list[XiaohongshuUser],
        list[XiaohongshuNote],
        list[XiaohongshuComment],
        list[XiaohongshuTrend],
    ]:
        values = list(items)
        users = [value for value in values if isinstance(value, XiaohongshuUser)]
        comments = [
            value for value in values if isinstance(value, XiaohongshuComment)
        ]
        known_user_ids = {user.external_id for user in users}
        for comment in comments:
            if (
                not comment.author_external_id
                or comment.author_external_id == "unknown"
                or comment.author_external_id in known_user_ids
            ):
                continue
            users.append(
                XiaohongshuUser(
                    external_id=comment.author_external_id,
                    nickname=comment.author_nickname or comment.author_external_id,
                    protected=comment.author_protected,
                    disabled=comment.author_disabled,
                    spam_risk=comment.author_spam_risk,
                    anomaly_signals=comment.author_anomaly_signals,
                    source_provider=comment.source_provider,
                    captured_at=comment.captured_at,
                )
            )
            known_user_ids.add(comment.author_external_id)
        return (
            users,
            [value for value in values if isinstance(value, XiaohongshuNote)],
            comments,
            [value for value in values if isinstance(value, XiaohongshuTrend)],
        )

    def _persist(
        self,
        users: list[XiaohongshuUser],
        notes: list[XiaohongshuNote],
        comments: list[XiaohongshuComment],
        trends: list[XiaohongshuTrend],
    ) -> None:
        repository = self.repository
        rich_ids = {user.external_id for user in users}
        for note in notes:
            if (
                note.author_external_id not in rich_ids
                and repository.get_kol(note.author_external_id) is None
            ):
                repository.upsert_user(
                    XiaohongshuUser(
                        external_id=note.author_external_id,
                        nickname=note.author_nickname or note.author_external_id,
                        native_id_resolved=note.author_native_id_resolved,
                        source_provider=note.source_provider,
                        captured_at=note.captured_at,
                    )
                )
        for user in users:
            repository.upsert_user(user)
        repository.upsert_notes(notes)
        repository.upsert_comments(comments)
        repository.record_comment_relationship_evidence(comments)
        repository.upsert_trends(trends)

    def _enrich_notes(
        self,
        notes: list[XiaohongshuNote],
        payload: Mapping[str, Any],
    ) -> tuple[
        list[XiaohongshuNote], Mapping[str, ContentInsight], tuple[str, ...]
    ]:
        insights: Mapping[str, ContentInsight] = {}
        warnings: tuple[str, ...] = ()
        brand = payload.get("brand")
        try:
            insights = self.content_intelligence.analyze(
                platform_id=self.platform_id,
                contents=[
                    NativeContentSample(
                        object_id=note.external_id,
                        author_id=note.author_external_id,
                        text="\n".join(
                            value for value in (note.title, note.body) if value
                        ),
                        url=note.url,
                    )
                    for note in notes
                ],
                brand=brand if isinstance(brand, Mapping) else {},
            )
        except Exception as exc:
            warnings = (f"optional Xiaohongshu content intelligence failed: {exc}",)
        classified = [
            note.model_copy(
                update={
                    "relevance_score": max(
                        note.relevance_score,
                        _classify_relevance(f"{note.title}\n{note.body}"),
                        (
                            insights[note.external_id].relevance_score
                            if note.external_id in insights
                            else 0.0
                        ),
                    )
                }
            )
            for note in notes
        ]
        return classified, insights, warnings

    def _promote(
        self,
        users: list[XiaohongshuUser],
        notes: list[XiaohongshuNote],
        comments: list[XiaohongshuComment],
    ) -> None:
        repository = self.repository
        notes_by_author: dict[str, int] = {}
        for note in notes:
            notes_by_author[note.author_external_id] = (
                notes_by_author.get(note.author_external_id, 0) + 1
            )
        stored_by_id = {
            str(row["id"]): row for row in repository.list_kols(limit=500)
        }
        all_ids = (
            set(notes_by_author)
            | {user.external_id for user in users}
            | {
                comment.author_external_id
                for comment in comments
                if comment.author_external_id != "unknown"
            }
            | set(stored_by_id)
        )
        by_id = {user.external_id: user for user in users}
        for user_id in all_ids:
            user = by_id.get(user_id)
            stored = stored_by_id.get(user_id) or repository.get_kol(user_id) or {}
            native_id_resolved = (
                user.native_id_resolved
                if user is not None
                else bool(stored.get("native_id_resolved", True))
            )
            if not native_id_resolved:
                repository.set_kol_status(
                    user_id,
                    KolStatus.CANDIDATE,
                    score=0.0,
                    reasons=("native account identifier is unavailable",),
                    qualified=False,
                )
                continue
            followers = user.followers_count if user else int(stored.get("followers_count") or 0)
            content_count = repository.recent_relevant_content_count(user_id)
            relationship_count = self.repository.relationship_evidence_count(user_id)
            has_relationship = relationship_count > 0
            score = min(
                1.0,
                0.30
                + 0.30 * log_ratio(followers, 1_000_000)
                + 0.25 * min(1.0, content_count / 3)
                + (0.20 if has_relationship else 0),
            )
            current = KolStatus(stored.get("status", KolStatus.CANDIDATE.value))
            decision = self.promotion.decide(
                PromotionInput(
                    score=score,
                    relevant_content_count_30d=content_count,
                    relationship_evidence_count=relationship_count,
                    protected=(
                        user.protected
                        if user is not None
                        else bool(stored.get("protected"))
                    ),
                    disabled=(
                        user.disabled
                        if user is not None
                        else bool(stored.get("disabled"))
                    ),
                    spam_risk=(
                        user.spam_risk
                        if user is not None
                        else float(stored.get("spam_risk") or 0.0)
                    ),
                    current_status=current,
                )
            )
            repository.set_kol_status(
                user_id,
                decision.status,
                score=score,
                reasons=decision.reasons,
                qualified=decision.qualified,
            )

    def _content_opportunity(
        self,
        content: XiaohongshuNote,
        insight: ContentInsight | None = None,
    ) -> PlatformOpportunityProposal | None:
        if content.relevance_score < 0.5:
            return None
        ttl_hours = max(1, int(getattr(self.settings, "signal_opportunity_ttl_hours", 24)))
        created = parse_time(content.published_at)
        age_hours = max(0.0, (utc_now() - created).total_seconds() / 3600) if created else 24.0
        if age_hours > ttl_hours:
            return None
        engagement = (
            content.metrics.likes + content.metrics.comments + content.metrics.collects
        )
        engagement_points = min(30.0, math.log1p(max(0, engagement)) * 6)
        recency_points = max(0.0, 20 * (1 - age_hours / ttl_hours))
        relevance_points = content.relevance_score * 30
        score = min(100.0, 20 + relevance_points + engagement_points + recency_points)
        expires_at = ((created or utc_now()) + timedelta(hours=ttl_hours)).isoformat()
        text = "\n".join(value for value in (content.title, content.body) if value)
        fallback_draft = (
            "这个观察很有启发。接下来最值得验证的指标是什么？"
            if any("\u4e00" <= char <= "\u9fff" for char in text)
            else "Useful framing. Which measurable signal would confirm this next?"
        )
        draft = str((insight.reply_draft if insight else None) or fallback_draft).strip()[
            :300
        ]
        evidence: list[dict[str, Any]] = [
            {"kind": "recent_content", "engagement": engagement}
        ]
        if insight is not None:
            evidence.append(
                {
                    "kind": "content_intelligence",
                    "relevance_score": insight.relevance_score,
                    "cluster_key": insight.cluster_key,
                    "cluster_title": insight.cluster_title,
                    "rationale": insight.rationale,
                }
            )
        return PlatformOpportunityProposal(
            opportunity_type="reply",
            native_object=NativeObjectReference(
                "xiaohongshu", "note", content.external_id
            ),
            priority=round(score),
            score=score,
            title=text.splitlines()[0][:120] if text else content.external_id,
            evidence=tuple(evidence),
            payload={
                "target_url": content.url,
                "target_author_id": content.author_external_id,
                "text": text,
                "draft": draft,
                "is_initial_dm": False,
                "note_type": content.note_type,
                "cluster_key": insight.cluster_key if insight else None,
                "cluster_title": insight.cluster_title if insight else None,
            },
            expires_at=expires_at,
            score_reasons=(
                "20 platform opportunity base",
                f"{relevance_points:.1f} relevance points",
                f"{engagement_points:.1f} engagement points",
                f"{recency_points:.1f} recency points",
            ),
            suggested_actions=(
                PlatformActionProposal(
                    PlatformSuggestedAction.COMMENT,
                    draft=draft,
                    score=score,
                    expires_at=expires_at,
                ),
            ),
        )

    def _trend_opportunity(
        self,
        trend: XiaohongshuTrend,
    ) -> PlatformOpportunityProposal | None:
        relevance = _classify_relevance(trend.name)
        if relevance < 0.5:
            return None
        rank = max(1, trend.rank or 20)
        rank_points = max(0.0, (20 - rank) * 1.25)
        volume_points = min(25.0, math.log1p(max(0, trend.note_count)) * 3)
        relevance_points = relevance * 25
        score = min(100.0, 30 + relevance_points + rank_points + volume_points)
        observed_at = utc_now()
        ttl_hours = max(
            1, int(getattr(self.settings, "signal_opportunity_ttl_hours", 24))
        )
        draft = f"{trend.name}：下一步最值得验证的信号是什么？"
        return PlatformOpportunityProposal(
            opportunity_type="topic",
            native_object=NativeObjectReference(
                "xiaohongshu",
                "trend",
                stable_native_id("xiaohongshu", trend.name.casefold()),
            ),
            priority=round(score),
            score=score,
            title=trend.name,
            evidence=(
                {
                    "kind": "native_trend",
                    "rank": rank,
                    "content_count": trend.note_count,
                    "relevance_score": relevance,
                },
            ),
            payload={
                "draft": draft,
                "target_url": trend.url,
                "observed_at": observed_at.isoformat(),
            },
            expires_at=(observed_at + timedelta(hours=ttl_hours)).isoformat(),
            score_reasons=(
                "30 native trend base",
                f"{relevance_points:.1f} deterministic relevance points",
                f"{rank_points:.1f} rank points",
                f"{volume_points:.1f} volume points",
            ),
            # Xiaohongshu does not declare owned publishing, so no invalid action
            # is proposed and the shared core never needs an X-specific fallback.
            suggested_actions=(),
        )


__all__ = ["XiaohongshuAutomationAdapter"]
