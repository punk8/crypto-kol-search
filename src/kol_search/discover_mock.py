from __future__ import annotations

from typing import Any


PLATFORMS: tuple[dict[str, str], ...] = (
    {"id": "x", "name": "X", "mark": "X", "tone": "slate", "mode": "live", "status": "Live required"},
    {"id": "youtube", "name": "YouTube", "mark": "YT", "tone": "red", "mode": "mock", "status": "Mock"},
    {"id": "reddit", "name": "Reddit", "mark": "R", "tone": "orange", "mode": "mock", "status": "Mock"},
    {"id": "instagram", "name": "Instagram", "mark": "IG", "tone": "violet", "mode": "mock", "status": "Mock"},
)

# Keep the full fixture catalog available for future connector work, while the
# internal v1 product only exposes X in its presentation layer.
DISPLAY_PLATFORM_IDS: frozenset[str] = frozenset({"x"})

TREND_SOURCES: tuple[dict[str, str], ...] = PLATFORMS + (
    {"id": "web", "name": "Web Search", "mark": "WEB", "tone": "blue", "mode": "mock", "status": "Mock"},
)

CATEGORIES: tuple[dict[str, str], ...] = (
    {"id": "all", "name": "All Categories"},
    {"id": "ai", "name": "AI & Tech"},
    {"id": "business", "name": "Business"},
    {"id": "consumer", "name": "Consumer"},
    {"id": "gaming", "name": "Gaming"},
)


TREND_THEMES: tuple[dict[str, Any], ...] = (
    {
        "id": "ai-productivity",
        "name": "AI Productivity Tools Surge",
        "description": "Creators share AI tools that compress research and production work.",
        "category": "ai",
        "state": "rising",
        "state_label": "Rising",
        "coverage": ("youtube", "reddit", "instagram", "web"),
        "evidence": "12.4K",
        "first_seen": "May 26, 09:10",
        "last_observed": "2 min ago",
        "velocity": (34, 39, 37, 48, 55, 61, 73, 79),
    },
    {
        "id": "small-models",
        "name": "Small Models Move On-device",
        "description": "Benchmarks and launches shift attention toward local inference.",
        "category": "ai",
        "state": "new",
        "state_label": "New",
        "coverage": ("youtube", "reddit", "web"),
        "evidence": "8.7K",
        "first_seen": "Today, 07:42",
        "last_observed": "4 min ago",
        "velocity": (12, 14, 19, 27, 36, 54, 68, 83),
    },
    {
        "id": "creator-studios",
        "name": "Creator Brand Studios",
        "description": "Independent creators productize their audience and distribution.",
        "category": "business",
        "state": "persistent",
        "state_label": "Persistent",
        "coverage": ("youtube", "instagram", "web"),
        "evidence": "6.1K",
        "first_seen": "May 21, 16:30",
        "last_observed": "6 min ago",
        "velocity": (52, 57, 55, 61, 59, 64, 62, 65),
    },
    {
        "id": "indie-games",
        "name": "Indie Game Spotlight",
        "description": "Short-form demos are sending players into community discussions.",
        "category": "gaming",
        "state": "rising",
        "state_label": "Rising",
        "coverage": ("youtube", "reddit", "instagram"),
        "evidence": "5.8K",
        "first_seen": "May 25, 12:05",
        "last_observed": "8 min ago",
        "velocity": (26, 31, 38, 42, 49, 63, 67, 76),
    },
    {
        "id": "summer-deals",
        "name": "Summer Travel Deals",
        "description": "Airfare and hotel deal chatter is losing momentum after a spike.",
        "category": "consumer",
        "state": "cooling",
        "state_label": "Cooling",
        "coverage": ("instagram", "web"),
        "evidence": "3.9K",
        "first_seen": "May 18, 10:20",
        "last_observed": "15 min ago",
        "velocity": (82, 79, 73, 68, 61, 54, 47, 39),
    },
)


NATIVE_SIGNALS: dict[str, tuple[tuple[str, str], ...]] = {
    "youtube": (
        ("AI coding benchmarks", "+26%"),
        ("Local model tutorials", "+19%"),
        ("RAG explained", "+13%"),
        ("Creator business systems", "+8%"),
        ("Indie game devlogs", "+6%"),
    ),
    "reddit": (
        ("r/LocalLLaMA model thread", "+31%"),
        ("Agent reliability debate", "+17%"),
        ("Open model benchmarks", "+12%"),
        ("Solo founder stack", "+9%"),
        ("Indie launch feedback", "+7%"),
    ),
    "instagram": (
        ("AI workflow reels", "+22%"),
        ("Desk setup automation", "+16%"),
        ("Creator studio tours", "+12%"),
        ("Productivity carousel", "+10%"),
        ("Indie art process", "+6%"),
    ),
    "web": (
        ("AI productivity tools", "+20%"),
        ("Small model releases", "+17%"),
        ("Agent security", "+13%"),
        ("Creator economy platforms", "+8%"),
        ("Gaming showcase", "+5%"),
    ),
}


TREND_EVIDENCE: tuple[dict[str, str], ...] = (
    {
        "platform": "youtube",
        "time": "09:28",
        "title": "Agent coding benchmark walkthrough gains velocity",
        "source": "Build Systems · 186K views",
    },
    {
        "platform": "reddit",
        "time": "09:12",
        "title": "Open model tooling thread reaches the front page",
        "source": "r/LocalLLaMA · 2.1K upvotes",
    },
    {
        "platform": "instagram",
        "time": "09:05",
        "title": "AI workflow carousel repeated across creator accounts",
        "source": "Public creator posts · 42 sources",
    },
    {
        "platform": "web",
        "time": "08:52",
        "title": "AI productivity roundups appear across independent sites",
        "source": "Web Search · 18 domains",
    },
)


HOT_CONTENT: tuple[dict[str, Any], ...] = (
    {
        "id": "youtube-agent-stack",
        "platform": "youtube",
        "type": "Video",
        "author": "CodeCraft",
        "handle": "@codecraft",
        "excerpt": "Build an AI coding agent from scratch: the evaluation stack that catches production failures.",
        "published": "3 h ago",
        "retrieved": "4 min ago",
        "engagement": "186K views",
        "velocity": "+623%",
        "score": 91,
        "spark": (14, 21, 28, 34, 49, 56, 63, 77, 87),
        "reason": "watchlist",
        "reason_label": "Watched KOL breakout",
        "trend": "AI agent evaluation",
        "following": True,
    },
    {
        "id": "youtube-local-models",
        "platform": "youtube",
        "type": "Video",
        "author": "NeuralNerd",
        "handle": "@neuralnerd",
        "excerpt": "Small models on real hardware: a practical local inference benchmark.",
        "published": "6 h ago",
        "retrieved": "7 min ago",
        "engagement": "142K views",
        "velocity": "+418%",
        "score": 84,
        "spark": (17, 23, 29, 35, 42, 48, 57, 69, 76),
        "reason": "trend",
        "reason_label": "Trend breakout",
        "trend": "On-device AI",
        "following": True,
    },
    {
        "id": "reddit-gaia",
        "platform": "reddit",
        "type": "Discussion",
        "author": "curious_builder",
        "handle": "u/curious_builder · r/LocalLLaMA",
        "excerpt": "GAIA 2.0 is out: 172 real-world tasks and a much harder agent reliability bar.",
        "published": "2 h ago",
        "retrieved": "3 min ago",
        "engagement": "1.8K upvotes · 68 comments",
        "velocity": "+842%",
        "score": 93,
        "spark": (12, 18, 31, 38, 45, 52, 66, 74, 90),
        "reason": "watchlist",
        "reason_label": "Watched KOL breakout",
        "trend": "AI agent evaluation",
        "following": True,
    },
    {
        "id": "instagram-prompt-evals",
        "platform": "instagram",
        "type": "Reel",
        "author": "AI Workflows",
        "handle": "@aiworkflows",
        "excerpt": "Three prompt patterns that improved our evaluations by 27 percent.",
        "published": "3 h ago",
        "retrieved": "4 min ago",
        "engagement": "92K plays · 7.4K likes",
        "velocity": "+618%",
        "score": 88,
        "spark": (15, 24, 27, 38, 46, 55, 64, 72, 86),
        "reason": "watchlist",
        "reason_label": "Watched KOL breakout",
        "trend": "Prompt engineering",
        "following": True,
    },
)


KOL_SEEDS: dict[str, tuple[tuple[str, str, str, tuple[str, ...], int], ...]] = {
    "youtube": (
        ("codecraft", "CodeCraft", "@codecraft", ("AI Coding", "Python", "LLMs"), 88),
        ("neuralnerd", "NeuralNerd", "@neuralnerd", ("AI Agents", "LangChain", "Python"), 84),
        ("promptpilot", "PromptPilot", "@promptpilot", ("Prompting", "LLMs", "Evals"), 81),
        ("datadojo", "DataDojo", "@datadojo", ("Data Science", "Python", "AI"), 79),
        ("devmind", "DevMind", "@devmind", ("AI Tools", "Code Review", "DX"), 76),
        ("stacklore", "StackLore", "@stacklore", ("System Design", "Backend", "AI"), 72),
    ),
    "reddit": (
        ("curious_builder", "curious_builder", "u/curious_builder", ("Local LLMs", "Benchmarks", "Hardware"), 85),
        ("eval_engineer", "eval_engineer", "u/eval_engineer", ("Evals", "Agents", "Reliability"), 82),
        ("open_weights", "open_weights", "u/open_weights", ("Open Source", "Models", "Research"), 78),
        ("rag_practitioner", "rag_practitioner", "u/rag_practitioner", ("RAG", "Search", "Production"), 75),
        ("ml_systems", "ml_systems", "u/ml_systems", ("MLOps", "Inference", "Systems"), 72),
        ("prompt_skeptic", "prompt_skeptic", "u/prompt_skeptic", ("Prompting", "Testing", "Safety"), 68),
    ),
    "instagram": (
        ("aiworkflows", "AI Workflows", "@aiworkflows", ("AI Tools", "Workflows", "Creators"), 84),
        ("designwithmodels", "Design With Models", "@designwithmodels", ("Design", "AI", "Prototyping"), 81),
        ("buildinpubliclab", "Build in Public Lab", "@buildinpubliclab", ("Startups", "AI", "Products"), 78),
        ("promptframes", "Prompt Frames", "@promptframes", ("Prompting", "Visual AI", "Tutorials"), 75),
        ("automationdesk", "Automation Desk", "@automationdesk", ("Automation", "No-code", "AI"), 72),
        ("modelminute", "Model Minute", "@modelminute", ("AI News", "Explainers", "Reels"), 69),
    ),
}


PLATFORM_DETAIL: dict[str, dict[str, Any]] = {
    "youtube": {
        "score_label": "YouTube Score",
        "watchlist_label": "Add to YouTube Watchlist",
        "comparison_note": "Scores are comparable only within YouTube",
        "metric_labels": ("Relevant videos", "30-day views", "Upload cadence", "Avg. engagement"),
        "metric_values": ("24", "1.42M", "1.3 / week", "6.3%"),
        "content_label": "Recent relevant videos",
        "recent": (
            ("Build an AI Coding Agent from Scratch", "3 days ago · 186K views", "22:14"),
            ("10 AI Tools Every Developer Should Know", "7 days ago · 142K views", "14:37"),
            ("RAG Explained Simply", "10 days ago · 221K views", "18:05"),
        ),
    },
    "reddit": {
        "score_label": "Reddit Score",
        "watchlist_label": "Add to Reddit Watchlist",
        "comparison_note": "Scores are comparable only within Reddit",
        "metric_labels": ("Relevant discussions", "30-day upvotes", "Contribution cadence", "Reply quality"),
        "metric_values": ("31", "18.6K", "4.1 / week", "High"),
        "content_label": "Recent relevant discussions",
        "recent": (
            ("What actually improved local model latency?", "4 hours ago · 1.8K upvotes", "DISCUSSION"),
            ("Agent evaluation harness: lessons learned", "2 days ago · 940 upvotes", "POST"),
            ("Benchmark methodology review", "6 days ago · 618 upvotes", "COMMENT"),
        ),
    },
    "instagram": {
        "score_label": "Instagram Score",
        "watchlist_label": "Add to Instagram Watchlist",
        "comparison_note": "Scores are comparable only within Instagram",
        "metric_labels": ("Relevant posts / reels", "30-day reach", "Publishing cadence", "Avg. engagement"),
        "metric_values": ("28", "860K", "3.2 / week", "5.7%"),
        "content_label": "Recent relevant posts and reels",
        "recent": (
            ("My practical AI research workflow", "1 day ago · 86K plays", "REEL"),
            ("Seven prompts for product teams", "4 days ago · 12K likes", "CAROUSEL"),
            ("Behind the scenes: agent prototype", "8 days ago · 9K likes", "POST"),
        ),
    },
}


WATCHLIST_CONTENT: tuple[dict[str, Any], ...] = (
    {
        "id": "video-1",
        "platform": "youtube",
        "type": "Video",
        "author": "CodeCraft",
        "handle": "@codecraft",
        "title": "Why vector databases are changing AI application architecture",
        "body": "A benchmark-led breakdown of retrieval trade-offs and migration strategies.",
        "published": "Published 18 min ago",
        "retrieved": "Retrieved 5 min ago",
        "trend": "AI infrastructure",
        "metric": "186K views · 22:14",
        "visual": "video",
        "is_breakout": True,
        "breakout_score": 95,
        "velocity": "+1,231%",
        "velocity_points": (14, 18, 31, 28, 46, 52, 66, 73, 88),
    },
    {
        "id": "discussion-1",
        "platform": "reddit",
        "type": "Discussion",
        "author": "curious_builder",
        "handle": "u/curious_builder · r/LocalLLaMA",
        "title": "Best open-source models for on-device inference?",
        "body": "Community benchmarks compare mobile NPUs, quantization tips, and real-world results.",
        "published": "Published 2 h ago",
        "retrieved": "Retrieved 7 min ago",
        "trend": "On-device AI",
        "metric": "1.7K upvotes · 68 comments",
        "visual": "discussion",
        "is_breakout": True,
        "breakout_score": 93,
        "velocity": "+842%",
        "velocity_points": (12, 18, 31, 38, 45, 52, 66, 74, 90),
    },
    {
        "id": "reel-1",
        "platform": "instagram",
        "type": "Reel",
        "author": "AI Workflows",
        "handle": "@aiworkflows",
        "title": "Three prompt patterns that improved our evaluations",
        "body": "A visual walkthrough of prompt tests and scoring changes.",
        "published": "Published 5 h ago",
        "retrieved": "Retrieved 8 min ago",
        "trend": "Prompt engineering",
        "metric": "92K plays · 7.4K likes",
        "visual": "reel",
        "is_breakout": True,
        "breakout_score": 88,
        "velocity": "+618%",
        "velocity_points": (15, 24, 27, 38, 46, 55, 64, 72, 86),
    },
    {
        "id": "short-1",
        "platform": "youtube",
        "type": "Short",
        "author": "NeuralNerd",
        "handle": "@neuralnerd",
        "title": "One eval every agent builder should run",
        "body": "A sixty-second reliability check for tool-calling agents.",
        "published": "Published 7 h ago",
        "retrieved": "Retrieved 12 min ago",
        "trend": "Agent evaluation",
        "metric": "240K views · 0:58",
        "visual": "short",
        "is_breakout": False,
        "breakout_score": 68,
        "velocity": "+92%",
        "velocity_points": (28, 31, 33, 37, 42, 45, 49, 53, 57),
    },
)


def _platform_map() -> dict[str, dict[str, str]]:
    return {item["id"]: dict(item) for item in TREND_SOURCES}


def _build_kols(platform_id: str) -> list[dict[str, Any]]:
    detail = PLATFORM_DETAIL[platform_id]
    output: list[dict[str, Any]] = []
    for index, (native_id, name, handle, topics, score) in enumerate(
        KOL_SEEDS[platform_id]
    ):
        output.append(
            {
                "id": native_id,
                "native_id": f"{platform_id}_{native_id}",
                "name": name,
                "handle": handle,
                "topics": topics,
                "score": score,
                "confidence": "High" if score >= 76 else "Medium",
                "activity": f"{index % 4 + 1} day{'s' if index % 4 else ''} ago",
                "initials": "".join(part[0] for part in name.split())[:2].upper(),
                "tone": ("violet", "blue", "mint", "amber", "rose", "slate")[index],
                "description": (
                    f"Publishes practical {topics[0].lower()} research and "
                    f"evidence-led {topics[1].lower()} analysis on {detail['score_label'].split()[0]}."
                ),
            }
        )
    return output


def _selected_kol(platform_id: str, kol_id: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates = _build_kols(platform_id)
    selected = next((item for item in candidates if item["id"] == kol_id), candidates[0])
    score = int(selected["score"])
    detail = PLATFORM_DETAIL[platform_id]
    selected = {
        **selected,
        **detail,
        "components": (
            ("Relevance", min(96, score + 4)),
            ("Content quality", min(94, score)),
            ("Engagement", max(58, score - 5)),
            ("Consistency", min(92, score + 1)),
            ("Risk", max(8, 32 - (score // 5))),
        ),
        "evidence": (
            ("Platform profile and native ID resolved", "High"),
            ("Recent domain content sample", "High"),
            ("Public engagement snapshots", "Medium"),
            ("Independent web citation", "Medium"),
        ),
        "last_synced": "1 hour ago",
    }
    return candidates, selected


def build_mock_discover_context(
    *,
    view: str,
    platform: str | None = None,
    category: str = "all",
    kol_id: str | None = None,
    trend_id: str | None = None,
    content_source: str = "discover",
    watch_mode: str = "latest",
) -> dict[str, Any]:
    """Return presentation fixtures without fabricating X/Twitter observations."""

    if view not in {"trends", "hot_content", "kols", "watchlist"}:
        view = "trends"
    platform_map = _platform_map()
    display_platforms = [
        dict(item) for item in PLATFORMS if item["id"] in DISPLAY_PLATFORM_IDS
    ]
    context: dict[str, Any] = {
        "view": view,
        "data_mode": "X live-only",
        "platforms": display_platforms,
        "trend_sources": [dict(platform_map["x"])],
        "platform_map": platform_map,
        "x_live_required": True,
    }

    if view == "trends":
        if category not in {item["id"] for item in CATEGORIES}:
            category = "all"
        unavailable_trend = {
            "id": "x-unavailable",
            "name": "X trends unavailable",
            "description": "Connect an X trend provider to load live observations.",
            "category": category if category != "all" else "x",
            "state": "new",
            "state_label": "Unavailable",
            "coverage": ("x",),
            "evidence": "—",
            "first_seen": "—",
            "last_observed": "—",
            "velocity": (),
        }
        context.update(
            {
                "categories": [dict(item) for item in CATEGORIES],
                "selected_category": category,
                "trend_themes": [],
                "native_signals": [],
                "trend_evidence": [],
                "selected_trend": unavailable_trend,
                "requested_trend": trend_id,
                "trend_hot_posts": [],
            }
        )
    elif view == "hot_content":
        platform_id = "x"
        source_id = content_source if content_source in {"discover", "following"} else "discover"
        items = [
            {**item, "source": platform_map[item["platform"]]}
            for item in HOT_CONTENT
            if (platform_id == "all" or item["platform"] == platform_id)
            and (source_id == "discover" or item["following"])
        ]
        context.update(
            {
                "selected_platform": platform_id,
                "content_source": source_id,
                "hot_content": items,
                "hot_summary": (
                    ("Breakout posts", str(len(items)), "Current filtered view"),
                    (
                        "Trend-driven",
                        str(sum(item["reason"] == "trend" for item in items)),
                        "Linked to rising themes",
                    ),
                    (
                        "Watched KOL breakouts",
                        str(sum(item["following"] for item in items)),
                        "From enrolled accounts",
                    ),
                    ("Average velocity", "+816%" if items else "—", "Platform-relative"),
                ),
                "related_trend": ("AI agent evaluation", "+368%", "1.2K posts"),
                "breakout_mix": (
                    ("Trend breakout", "46%"),
                    ("Platform breakout", "21%"),
                    ("Watched KOL breakout", "33%"),
                ),
            }
        )
    elif view == "kols":
        context.update(
            {
                "selected_platform": "x",
                "platform": platform_map["x"],
                "kols": [],
                "selected_kol": None,
                "source_requires_live_data": True,
            }
        )
        return context
    else:
        platform_id = "x"
        mode = watch_mode if watch_mode in {"latest", "breakout"} else "latest"
        items = [
            {**item, "source": platform_map[item["platform"]]}
            for item in WATCHLIST_CONTENT
            if platform_id == "all" or item["platform"] == platform_id
            if mode == "latest" or item["is_breakout"]
        ]
        context.update(
            {
                "selected_platform": platform_id,
                "watch_mode": mode,
                "content_items": items,
                "watchlist_summary": (
                    ("Tracked X KOLs", "0", "No accounts enrolled"),
                    ("Live X posts", "0", "Current retrieval"),
                    ("New content", "0", "Since last sync"),
                    ("Trend matches", "0", "X observations"),
                ),
                "daily_brief": (
                    ("Open-source momentum continues", "New releases and tooling updates highlight rapid progress in open models."),
                    ("Evaluation becomes the new bottleneck", "KOLs are comparing reliability rather than model size alone."),
                    ("On-device AI gains traction", "Fresh benchmarks point to practical local inference workloads."),
                ),
                "breakout_alerts": (
                    ("Vector database thread accelerates", "CodeCraft is outperforming its recent baseline."),
                    ("Evaluation reel gains velocity", "AI Workflows is accelerating across new viewers."),
                ),
                "trend_connections": (
                    ("AI infrastructure", "+38%", "5 items"),
                    ("On-device AI", "+26%", "4 items"),
                    ("AI agents", "+19%", "3 items"),
                ),
                "source_health": tuple(
                    (
                        item["id"],
                        item["name"],
                        "Not connected" if item["id"] == "x" else f"{index + 2} min ago",
                        "unconfigured" if item["id"] == "x" else "healthy",
                    )
                    for index, item in enumerate(display_platforms)
                ),
            }
        )
    return context
