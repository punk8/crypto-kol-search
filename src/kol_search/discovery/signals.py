from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from kol_search.config_loader import load_domain_config, load_seed_handles
from kol_search.db import Store
from kol_search.discovery.classify import classify_account
from kol_search.discovery.query import all_aliases
from kol_search.discovery.signal_ai import (
    generate_reply_draft,
    generate_semantic_topic_clusters,
    generate_topic_content,
)
from kol_search.models import Account, Post, TrendSignal
from kol_search.outreach import validate_comment
from kol_search.replies import ReplyOpportunityError, publish_validated_comment
from kol_search.settings import Settings
from kol_search.twitter.base import TwitterBackendError, merge_client_diagnostics
from kol_search.twitter.factory import create_twitter_client
from kol_search.twitter.reply import ReplyConfirmationRequiredError


ProgressCallback = Callable[[int, str, dict, list[str]], None]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _language(text: str, provided: str | None = None) -> str:
    if provided in {"zh", "en"}:
        return provided
    if re.search(r"[\u4e00-\u9fff]", text or ""):
        return "zh"
    return "en" if re.search(r"[A-Za-z]", text or "") else "unknown"


def _contains(text: str, alias: str) -> bool:
    text = text.lower()
    alias = alias.lower().strip()
    if not alias:
        return False
    if alias.isascii() and re.fullmatch(r"[a-z0-9]+", alias):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text))
    return alias in text


def _matched_topics(text: str, topic_aliases: dict[str, list[str]]) -> list[str]:
    return [
        slug
        for slug, aliases in topic_aliases.items()
        if any(_contains(text, value) for value in [slug.replace("_", " "), *aliases])
    ]


def _account_from_row(row: dict[str, Any]) -> Account:
    try:
        entities = json.loads(row.get("entities_json") or "{}")
    except json.JSONDecodeError:
        entities = {}
    return Account(
        id=str(row["id"]),
        username=row["username"],
        name=row.get("name"),
        description=row.get("description"),
        followers_count=int(row.get("followers_count") or 0),
        following_count=int(row.get("following_count") or 0),
        tweet_count=int(row.get("tweet_count") or 0),
        listed_count=int(row.get("listed_count") or 0),
        verified=bool(row.get("verified")),
        protected=bool(row.get("protected")),
        created_at=row.get("created_at"),
        profile_image_url=row.get("profile_image_url"),
        url=row.get("profile_url"),
        location=row.get("location"),
        entities=entities,
    )


def _latest_post_id(posts: list[Post]) -> str | None:
    ids = [post.id for post in posts if post.id]
    if not ids:
        return None
    numeric = [value for value in ids if value.isdigit()]
    if len(numeric) == len(ids):
        return max(numeric, key=int)
    return max(posts, key=lambda post: post.created_at or "").id


def _safe_log_ratio(value: float, reference: float) -> float:
    return min(1.0, max(0.0, math.log1p(max(0.0, value)) / math.log1p(reference)))


def _sanitize_draft(draft: str | None, forbidden_terms: list[str]) -> str | None:
    if not draft:
        return None
    lowered = draft.lower()
    if any(term.strip() and term.strip().lower() in lowered for term in forbidden_terms):
        return None
    return draft.strip()[:280]


class SignalPipeline:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self._now = now_provider or (lambda: datetime.now(timezone.utc))

    def run(self, run: dict[str, Any], progress: ProgressCallback | None = None) -> list[str]:
        config = load_domain_config("crypto")
        warnings: list[str] = []
        stats: dict[str, Any] = {
            "timeline_requests": 0,
            "post_queries": 0,
            "posts_observed": 0,
            "native_trends": 0,
            "reply_opportunities": 0,
            "topic_clusters": 0,
            "outcome_checks": 0,
            "ai_calls": 0,
        }
        client = create_twitter_client(
            run["backend"], self.settings, self.store.resolve_account_id
        )

        def report(percent: int, phase: str) -> None:
            merge_client_diagnostics(client, stats, warnings)
            if progress:
                progress(percent, phase, stats, warnings)

        try:
            report(5, "signal_planning")
            watch_accounts = self._watch_accounts(client, config, warnings)
            posts, timelines, trends = self._collect(
                client, run["id"], watch_accounts, config, stats, warnings
            )
            report(50, "reply_scoring")
            brand = self.store.get_brand_profile()
            reply_ids = self._build_reply_queue(
                posts,
                timelines,
                watch_accounts,
                brand,
                config,
                run,
                stats,
                warnings,
            )
            self._auto_publish_replies(reply_ids, run, warnings)
            report(70, "topic_clustering")
            topic_ids = self._build_topic_queue(
                trends, watch_accounts, brand, config, run, stats, warnings
            )
            report(90, "outcome_checks")
            self._refresh_outcomes(client, stats, warnings)
            self.store.expire_reply_opportunities(self._now().isoformat())
            report(98, "persisting")
            return [f"reply:{value}" for value in reply_ids] + [
                f"topic:{value}" for value in topic_ids
            ]
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    def _watch_accounts(self, client, config, warnings: list[str]) -> list[Account]:  # noqa: ANN001
        rows = self.store.latest_approved_watch_accounts(limit=500)
        if rows:
            core_count = max(1, self.settings.signal_core_accounts)
            rotation_count = max(0, self.settings.signal_rotation_accounts)
            core = rows[:core_count]
            rotation_pool = rows[core_count:]
            checkpoint = self.store.get_scan_checkpoint("signal:watch-rotation") or {}
            metadata = checkpoint.get("metadata") or {}
            offset = int(metadata.get("offset") or 0)
            rotated: list[dict[str, Any]] = []
            if rotation_pool and rotation_count:
                for index in range(min(rotation_count, len(rotation_pool))):
                    rotated.append(rotation_pool[(offset + index) % len(rotation_pool)])
                offset = (offset + len(rotated)) % len(rotation_pool)
            self.store.upsert_scan_checkpoint(
                "signal:watch-rotation", metadata={"offset": offset, "pool_size": len(rotation_pool)}
            )
            return [_account_from_row(row) for row in [*core, *rotated]]

        handles = load_seed_handles(config.seed_file)
        target = max(1, self.settings.signal_core_accounts + self.settings.signal_rotation_accounts)
        try:
            accounts = client.get_users_by_usernames(handles[:target])
        except TwitterBackendError as exc:
            warnings.append(f"signal seed lookup: {exc}")
            accounts = []
        if not accounts and client.capabilities.user_search:
            try:
                accounts = client.search_users("crypto OR web3", max_results=target)
            except TwitterBackendError as exc:
                warnings.append(f"signal fallback user search: {exc}")
        return accounts[:target]

    def _collect(
        self,
        client,  # noqa: ANN001
        run_id: int,
        watch_accounts: list[Account],
        config,  # noqa: ANN001
        stats: dict[str, Any],
        warnings: list[str],
    ) -> tuple[list[Post], dict[str, list[Post]], list[TrendSignal]]:
        observed_at = self._now().isoformat()
        collected: dict[str, Post] = {}
        timelines: dict[str, list[Post]] = {}

        for account in watch_accounts:
            try:
                timeline = client.get_user_tweets(
                    account.id,
                    max_results=max(5, self.settings.signal_posts_per_account),
                    username=account.username,
                    include_replies=True,
                )
                stats["timeline_requests"] += 1
            except TwitterBackendError as exc:
                warnings.append(f"signal timeline @{account.username}: {exc}")
                continue
            for post in timeline:
                post.author_id = account.id
                post.author_username = post.author_username or account.username
                post.url = post.url or f"https://x.com/{account.username}/status/{post.id}"
                collected[post.id] = post
            timelines[account.id] = timeline
            enrichment = classify_account(account, timeline, config)
            account.id = self.store.upsert_account(
                account.model_dump(), enrichment.model_dump()
            )
            self.store.save_posts(timeline, captured_at=observed_at)
            self.store.save_post_observations(
                run_id,
                [(post.id, "timeline", account.username) for post in timeline if post.id],
                observed_at=observed_at,
            )

        queries = config.keywords[: max(1, self.settings.max_post_queries)]
        for query in queries:
            source_key = f"{client.name}:search:{query}"
            checkpoint = self.store.get_scan_checkpoint(source_key) or {}
            try:
                found = client.search_tweets(
                    query,
                    max_results=min(
                        config.search_max_results_per_query,
                        client.capabilities.post_search_page_size,
                    ),
                    since_id=checkpoint.get("since_id"),
                )
                stats["post_queries"] += 1
            except TwitterBackendError as exc:
                warnings.append(f"signal search {query}: {exc}")
                continue
            self.store.save_posts(found, captured_at=observed_at)
            self.store.save_post_observations(
                run_id,
                [(post.id, "search", query) for post in found if post.id],
                observed_at=observed_at,
            )
            for post in found:
                collected[post.id] = post
            self.store.upsert_scan_checkpoint(
                source_key,
                since_id=_latest_post_id(found) or checkpoint.get("since_id"),
                metadata={"query": query, "result_count": len(found)},
                succeeded_at=observed_at,
            )

        trends: list[TrendSignal] = []
        if client.capabilities.trends and hasattr(client, "get_trends"):
            try:
                trends = client.get_trends(max_results=20)
                stats["native_trends"] = len(trends)
            except TwitterBackendError as exc:
                warnings.append(f"native trends: {exc}")
        topic_aliases = all_aliases(config)
        relevant_trends = [
            trend for trend in trends if _matched_topics(trend.name, topic_aliases)
        ][:5]
        for trend in relevant_trends:
            source_key = f"{client.name}:trend:{trend.name}"
            checkpoint = self.store.get_scan_checkpoint(source_key) or {}
            try:
                found = client.search_tweets(
                    trend.name,
                    max_results=min(20, client.capabilities.post_search_page_size),
                    since_id=checkpoint.get("since_id"),
                )
                stats["post_queries"] += 1
            except TwitterBackendError as exc:
                warnings.append(f"trend search {trend.name}: {exc}")
                continue
            self.store.save_posts(found, captured_at=observed_at)
            self.store.save_post_observations(
                run_id,
                [(post.id, "native_trend", trend.name) for post in found if post.id],
                observed_at=observed_at,
            )
            for post in found:
                collected[post.id] = post
            self.store.upsert_scan_checkpoint(
                source_key,
                since_id=_latest_post_id(found) or checkpoint.get("since_id"),
                metadata={"trend": trend.name, "result_count": len(found)},
                succeeded_at=observed_at,
            )
        stats["posts_observed"] = len(collected)
        return list(collected.values()), timelines, trends

    def _build_reply_queue(
        self,
        posts: list[Post],
        timelines: dict[str, list[Post]],
        watch_accounts: list[Account],
        brand: dict[str, Any],
        config,  # noqa: ANN001
        run: dict[str, Any],
        stats: dict[str, Any],
        warnings: list[str],
    ) -> list[int]:
        now = self._now()
        run_config = run.get("config") or {}
        sender_id = run_config.get("sender_account_id")
        sender = self.store.get_x_sender(int(sender_id)) if sender_id else None
        comment_style = str(run_config.get("comment_style") or "brand")
        publish_mode = str(run_config.get("publish_mode") or "review")
        campaign_goal = str(
            run_config.get("campaign_goal")
            or "Introduce LaunchVibes only when directly relevant to the original post"
        )
        account_by_id = {account.id: account for account in watch_accounts}
        topic_aliases = all_aliases(config)
        candidates: list[dict[str, Any]] = []
        for post in posts:
            account = account_by_id.get(post.author_id)
            created = _parse_time(post.created_at)
            if not account or not created or post.reference_type not in {None, "quoted"}:
                continue
            age_hours = max(0.0, (now - created).total_seconds() / 3600)
            if age_hours > self.settings.signal_opportunity_ttl_hours:
                continue
            topics = _matched_topics(post.text, topic_aliases)
            brand_blob = f"{brand.get('description', '')} {brand.get('audience', '')}"
            brand_tokens = {
                value.lower()
                for value in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", brand_blob)
            }
            overlap = sum(1 for value in brand_tokens if value in post.text.lower())
            relevance = min(1.0, 0.25 + 0.25 * len(topics) + 0.1 * overlap)
            timeline = timelines.get(account.id, [])
            authored_reply_rate = (
                sum(1 for item in timeline if item.reference_type == "replied_to") / len(timeline)
                if timeline
                else 0.0
            )
            response_probability = min(1.0, 0.25 + authored_reply_rate * 2.0)
            freshness = max(0.0, 1.0 - age_hours / self.settings.signal_opportunity_ttl_hours)
            relationship_value = _safe_log_ratio(account.followers_count, 5_000_000)
            per_hour = post.engagement / max(1.0, age_hours)
            velocity = _safe_log_ratio(per_hour, 500)
            score_payload = {
                "relevance": relevance * 30,
                "response_probability": response_probability * 25,
                "freshness": freshness * 20,
                "relationship_value": relationship_value * 15,
                "engagement_velocity": velocity * 10,
            }
            score = sum(score_payload.values())
            if post.reply_count > 500:
                score -= 8
                score_payload["saturation_penalty"] = -8
            reasons = []
            if topics:
                reasons.append(f"匹配主题：{', '.join(topics[:3])}")
            if freshness >= 0.7:
                reasons.append("仍在早期回复窗口")
            if response_probability >= 0.5:
                reasons.append("作者近期有公开回复行为")
            if velocity >= 0.5:
                reasons.append("互动增长较快")
            language = _language(post.text, post.lang)
            topic_label = (topics[0].replace("_", " ") if topics else "this topic")
            if language == "zh":
                rule_draft = (
                    f"你提到的 {topic_label} 很关键。我们在 LaunchVibes 也在帮助创作者"
                    "把内容规划与增长工作流连接起来。"
                )
            else:
                prefix = "We’re building LaunchVibes" if comment_style == "brand" else "I’m affiliated with LaunchVibes"
                rule_draft = f"{topic_label.title()} is a real creator workflow challenge. {prefix} to make planning and growth work more connected."
            suitable = bool(topics or overlap)
            candidates.append(
                {
                    "post": post,
                    "account": account,
                    "score": max(0.0, score),
                    "score_payload": score_payload,
                    "reasons": reasons or ["来自重点 KOL 的相关内容"],
                    "language": language,
                    "draft": rule_draft if suitable else "",
                    "draft_source": "rules",
                    "suitable": suitable,
                    "suitability_reason": (
                        "The post overlaps the configured creator/product context"
                        if suitable else "The post is not relevant enough to LaunchVibes"
                    ),
                    "expires_at": (
                        created + timedelta(hours=self.settings.signal_opportunity_ttl_hours)
                    ).isoformat(),
                }
            )

        candidates.sort(key=lambda item: item["score"], reverse=True)
        selected = candidates[: max(1, self.settings.signal_reply_limit)]
        ai_enabled = bool(run.get("use_ai") and self.settings.openai_api_key)
        for item in selected:
            post = item["post"]
            if ai_enabled:
                try:
                    saved_kol = self.store.get_account(item["account"].id) or {}
                    kol_context = item["account"].model_dump(exclude={"raw"})
                    for source, target in (
                        ("topics_json", "topics"), ("languages_json", "languages")
                    ):
                        try:
                            kol_context[target] = json.loads(saved_kol.get(source) or "[]")
                        except json.JSONDecodeError:
                            kol_context[target] = []
                    kol_context["summary"] = saved_kol.get("summary")
                    result = generate_reply_draft(
                        post=post.model_dump(exclude={"raw"}),
                        brand=brand,
                        kol=kol_context,
                        recent_context=[
                            value.model_dump(exclude={"raw"})
                            for value in timelines.get(item["account"].id, [])[:5]
                        ],
                        conversation_context=[
                            value.model_dump(exclude={"raw"})
                            for value in posts
                            if post.conversation_id
                            and value.conversation_id == post.conversation_id
                            and value.id != post.id
                        ][:5],
                        sender=sender or {},
                        comment_style=comment_style,
                        campaign_goal=campaign_goal,
                        language=item["language"],
                        api_key=self.settings.openai_api_key or "",
                        model=run.get("model") or self.settings.openai_model,
                        cache_get=self.store.cache_get,
                        cache_set=self.store.cache_set,
                    )
                    draft = _sanitize_draft(result.draft, brand.get("forbidden_terms", []))
                    if draft:
                        item["draft"] = draft
                        item["draft_source"] = "openai"
                    item["suitable"] = result.suitable
                    item["suitability_reason"] = result.reason
                    if result.angle:
                        item["reasons"].append(result.angle)
                    stats["ai_calls"] += 1
                except Exception as exc:
                    if len([value for value in warnings if value.startswith("reply AI")]) < 3:
                        warnings.append(f"reply AI @{post.author_username}: {exc}")

        ids: list[int] = []
        for item in selected:
            valid, validation_reason = validate_comment(
                post_text=item["post"].text,
                draft=item["draft"],
                suitable=bool(item["suitable"]),
                style=comment_style,
                sender_type=str((sender or {}).get("sender_type") or "brand"),
                allowed_claims=brand.get("allowed_claims", []),
                forbidden_terms=brand.get("forbidden_terms", []),
            )
            if publish_mode == "auto" and item["draft_source"] != "openai":
                valid = False
                validation_reason = "Auto publish requires an AI suitability assessment"
            status = "unsuitable" if not item["suitable"] else (
                "validated" if valid else "review_required"
            )
            ids.append(self.store.upsert_reply_opportunity(
                post_id=item["post"].id,
                account_id=item["account"].id,
                score=item["score"],
                language=item["language"],
                score_payload=item["score_payload"],
                reasons=item["reasons"],
                draft=item["draft"],
                draft_source=item["draft_source"],
                expires_at=item["expires_at"],
                observed_at=now.isoformat(),
                sender_account_id=int(sender_id) if sender_id else None,
                comment_style=comment_style,
                publish_mode=publish_mode,
                campaign_goal=campaign_goal,
                suitable=bool(item["suitable"]),
                suitability_reason=item["suitability_reason"],
                validation_status="passed" if valid else "failed",
                validation_reason=validation_reason,
                initial_status=status,
            ))
        stats["reply_opportunities"] = len(ids)
        return ids

    def _auto_publish_replies(
        self, opportunity_ids: list[int], run: dict[str, Any], warnings: list[str]
    ) -> None:
        config = run.get("config") or {}
        if config.get("publish_mode") != "auto" or not config.get("sender_account_id"):
            return
        sender = self.store.get_x_sender(int(config["sender_account_id"]))
        if not sender or not sender["enabled"] or not sender["comment_auto_publish"]:
            warnings.append("Comment auto-publish is disabled for the selected sender")
            return
        for opportunity_id in opportunity_ids:
            item = self.store.get_reply_opportunity(opportunity_id)
            if not item or item["status"] != "validated":
                continue
            try:
                publish_validated_comment(self.store, self.settings, opportunity_id)
            except ReplyConfirmationRequiredError:
                warnings.append(f"Reply #{opportunity_id} requires manual confirmation")
            except ReplyOpportunityError as exc:
                self.store.update_reply_opportunity(
                    opportunity_id,
                    status="review_required",
                    validation_status="failed",
                    validation_reason=str(exc),
                )
                warnings.append(f"Reply #{opportunity_id} blocked: {exc}")
            except TwitterBackendError as exc:
                self.store.update_reply_opportunity(
                    opportunity_id, status="failed",
                )
                warnings.append(f"Reply #{opportunity_id} failed: {exc}")

    def _build_topic_queue(
        self,
        trends: list[TrendSignal],
        watch_accounts: list[Account],
        brand: dict[str, Any],
        config,  # noqa: ANN001
        run: dict[str, Any],
        stats: dict[str, Any],
        warnings: list[str],
    ) -> list[int]:
        now = self._now()
        rows = self.store.list_recent_signal_posts(hours=48)
        topic_aliases = all_aliases(config)
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        ai_titles: dict[str, str] = {}
        for row in rows:
            text = row.get("text") or ""
            for topic in _matched_topics(text, topic_aliases):
                groups[f"topic:{topic}"].append(row)
            for anchor in re.findall(r"[$#][A-Za-z][A-Za-z0-9_]{1,14}", text):
                groups[f"anchor:{anchor.lower()}"].append(row)
            for url in re.findall(r"https?://[^\s)]+", text):
                digest = hashlib.sha1(url.rstrip(".,").encode()).hexdigest()[:12]
                groups[f"url:{digest}"].append(row)
            if row.get("referenced_post_id"):
                groups[f"reference:{row['referenced_post_id']}"].append(row)
            if row.get("reference_type") == "replied_to" and row.get("conversation_id"):
                groups[f"conversation:{row['conversation_id']}"].append(row)

        ai_enabled = bool(run.get("use_ai") and self.settings.openai_api_key)
        if ai_enabled and rows:
            try:
                semantic = generate_semantic_topic_clusters(
                    posts=rows,
                    api_key=self.settings.openai_api_key or "",
                    model=run.get("model") or self.settings.openai_model,
                    cache_get=self.store.cache_get,
                    cache_set=self.store.cache_set,
                )
                rows_by_id = {str(row["id"]): row for row in rows}
                for cluster in semantic.clusters:
                    key = re.sub(r"[^a-z0-9_]+", "_", cluster.cluster_key.lower()).strip("_")
                    members = [rows_by_id[value] for value in cluster.post_ids if value in rows_by_id]
                    if key and len(members) >= 2:
                        fingerprint = f"event:{key}"
                        groups[fingerprint].extend(members)
                        ai_titles[fingerprint] = cluster.title
                stats["ai_calls"] += 1
            except Exception as exc:
                warnings.append(f"topic semantic clustering: {exc}")

        trend_names = {trend.name.lower(): trend for trend in trends}
        watch_ids = {account.id for account in watch_accounts}
        candidates: list[dict[str, Any]] = []
        for fingerprint, values in groups.items():
            unique_posts = {str(value["id"]): value for value in values}
            posts = list(unique_posts.values())
            current = [
                value
                for value in posts
                if (created := _parse_time(value.get("created_at")))
                and 0 <= (now - created).total_seconds() <= 24 * 3600
            ]
            authors = {value.get("author_id") for value in current if value.get("author_id")}
            kol_count = len(authors & watch_ids)
            if not ((len(current) >= 3 and len(authors) >= 2) or kol_count >= 2):
                continue
            recent = [
                value
                for value in current
                if (created := _parse_time(value.get("created_at")))
                and (now - created).total_seconds() <= 6 * 3600
            ]
            older = [value for value in current if value not in recent]
            recent_rate = len(recent) / 6
            previous_rate = len(older) / 18
            growth = (recent_rate + 0.1) / (previous_rate + 0.1)
            velocity_score = min(1.0, growth / 3)
            engagement_total = sum(
                int(value.get("like_count") or 0)
                + int(value.get("retweet_count") or 0)
                + int(value.get("reply_count") or 0)
                + int(value.get("quote_count") or 0)
                for value in current
            )
            scale_score = (
                _safe_log_ratio(len(current), 30) + _safe_log_ratio(engagement_total, 20_000)
            ) / 2
            author_score = min(1.0, len(authors) / 10)
            kol_score = min(1.0, kol_count / 5)
            label = fingerprint.split(":", 1)[1]
            native = any(_contains(name, label.lstrip("$#")) or _contains(label, name) for name in trend_names)
            heat_score = 100 * (
                velocity_score * 0.35
                + scale_score * 0.25
                + kol_score * 0.20
                + author_score * 0.15
                + (1.0 if native else 0.0) * 0.05
            )
            first_seen = min(
                (_parse_time(value.get("created_at")) for value in current),
                default=now,
            ) or now
            if heat_score >= 75 and len(authors) >= 3:
                lifecycle = "breakout"
            elif previous_rate > 0 and recent_rate < previous_rate * 0.7:
                lifecycle = "declining"
            elif (now - first_seen).total_seconds() <= 6 * 3600:
                lifecycle = "emerging"
            elif recent_rate >= previous_rate * 1.5:
                lifecycle = "rising"
            else:
                lifecycle = "emerging"
            representative = max(
                current,
                key=lambda value: int(value.get("like_count") or 0)
                + int(value.get("retweet_count") or 0)
                + int(value.get("reply_count") or 0)
                + int(value.get("quote_count") or 0),
            )
            language_weights: dict[str, int] = defaultdict(int)
            for value in current:
                lang = _language(value.get("text") or "", value.get("lang"))
                language_weights[lang] += 1 + int(value.get("like_count") or 0)
            language = max(language_weights, key=language_weights.get)
            representative_topics = _matched_topics(
                representative.get("text") or "", topic_aliases
            )
            fallback_topic = (
                representative_topics[0].replace("_", " ").title()
                if representative_topics
                else "Crypto"
            )
            if fingerprint.startswith("anchor:"):
                rule_title = label.upper()
            elif fingerprint.startswith("topic:"):
                rule_title = label.replace("_", " ").title()
            elif fingerprint.startswith("url:"):
                rule_title = f"{fallback_topic} Shared Link"
            else:
                rule_title = f"{fallback_topic} Conversation"
            title = ai_titles.get(fingerprint) or rule_title
            if language == "zh":
                summary = f"{len(authors)} 位作者正在讨论 {title}，近 6 小时热度变化 {growth:.1f}x。"
                outline = f"现象：{title} 讨论升温\n证据：{len(current)} 条帖子，{kol_count} 位重点 KOL\n观点：说明值得关注的后续变量"
                draft = f"{title} 正在成为新的讨论焦点。真正值得关注的不是声量本身，而是接下来哪些链上或产品指标会持续验证这一趋势。"
            else:
                summary = f"{len(authors)} authors are discussing {title}; 6h activity changed {growth:.1f}x."
                outline = f"Signal: {title} is gaining attention\nEvidence: {len(current)} posts and {kol_count} watched KOLs\nAngle: identify the next metric that confirms the trend"
                draft = f"{title} is becoming a focal point. The useful question is not just how loud the conversation is, but which onchain or product metric confirms it next."
            metrics = {
                "post_count": len(current),
                "unique_authors": len(authors),
                "engagement_total": engagement_total,
                "kol_count": kol_count,
                "growth_6h": round(growth, 3),
                "velocity_score": round(velocity_score, 4),
                "scale_score": round(scale_score, 4),
                "representative_post_id": representative["id"],
            }
            candidates.append(
                {
                    "fingerprint": fingerprint,
                    "title": title,
                    "summary": summary,
                    "language": language,
                    "lifecycle": lifecycle,
                    "heat_score": heat_score,
                    "metrics": metrics,
                    "outline": outline,
                    "draft": draft,
                    "draft_source": "rules",
                    "native": native,
                    "posts": sorted(
                        current,
                        key=lambda value: int(value.get("like_count") or 0),
                        reverse=True,
                    ),
                }
            )

        candidates.sort(key=lambda item: item["heat_score"], reverse=True)
        selected = candidates[: max(1, self.settings.signal_topic_limit)]
        for item in selected:
            if ai_enabled:
                try:
                    result = generate_topic_content(
                        topic=item["title"],
                        posts=item["posts"],
                        brand=brand,
                        language=item["language"],
                        api_key=self.settings.openai_api_key or "",
                        model=run.get("model") or self.settings.openai_model,
                        cache_get=self.store.cache_get,
                        cache_set=self.store.cache_set,
                    )
                    sanitized = _sanitize_draft(result.draft, brand.get("forbidden_terms", []))
                    if sanitized:
                        item.update(
                            title=result.title,
                            summary=result.summary,
                            outline=result.outline,
                            draft=sanitized,
                            draft_source="openai",
                        )
                    stats["ai_calls"] += 1
                except Exception as exc:
                    if len([value for value in warnings if value.startswith("topic AI")]) < 3:
                        warnings.append(f"topic AI {item['title']}: {exc}")

        ids = [
            self.store.upsert_topic_cluster(
                fingerprint=item["fingerprint"],
                title=item["title"],
                summary=item["summary"],
                language=item["language"],
                lifecycle=item["lifecycle"],
                heat_score=item["heat_score"],
                metrics=item["metrics"],
                outline=item["outline"],
                draft=item["draft"],
                draft_source=item["draft_source"],
                native_trend=item["native"],
                post_ids=[(str(post["id"]), 1.0) for post in item["posts"]],
                observed_at=now.isoformat(),
            )
            for item in selected
        ]
        stats["topic_clusters"] = len(ids)
        return ids

    def _refresh_outcomes(self, client, stats: dict[str, Any], warnings: list[str]) -> None:  # noqa: ANN001
        for item in self.store.due_reply_outcomes(self._now().isoformat()):
            brand_handle = (item.get("brand_handle") or "").lstrip("@")
            reply_url = item.get("reply_url") or ""
            match = re.search(r"/status/(\d+)", reply_url)
            if not brand_handle or not match:
                warnings.append(f"outcome #{item['id']}: missing brand handle or valid reply URL")
                continue
            try:
                own_posts = client.search_tweets(f"from:{brand_handle}", max_results=100)
                reply_post = next((post for post in own_posts if post.id == match.group(1)), None)
                conversation_id = item.get("conversation_id") or item.get("post_id")
                author = item.get("author_username")
                responses = client.search_tweets(
                    f"conversation_id:{conversation_id} from:{author} to:{brand_handle}",
                    max_results=20,
                ) if author else []
            except TwitterBackendError as exc:
                warnings.append(f"outcome #{item['id']}: {exc}")
                continue
            try:
                outcome = json.loads(item.get("outcome_json") or "{}")
            except json.JSONDecodeError:
                outcome = {}
            stage = next((value for value in (1, 6, 24) if str(value) not in outcome), 24)
            metrics = {
                "like_count": reply_post.like_count if reply_post else 0,
                "retweet_count": reply_post.retweet_count if reply_post else 0,
                "reply_count": reply_post.reply_count if reply_post else 0,
                "quote_count": reply_post.quote_count if reply_post else 0,
                "view_count": reply_post.view_count if reply_post else 0,
            }
            responded = bool(responses)
            self.store.save_reply_outcome(
                int(item["id"]),
                stage_hours=stage,
                metrics=metrics,
                author_responded=responded,
                payload={"response_post_ids": [post.id for post in responses]},
                captured_at=self._now().isoformat(),
            )
            stats["outcome_checks"] += 1
