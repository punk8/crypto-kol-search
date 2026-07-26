from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BackendName = Literal[
    "mock", "official", "third_party", "twitterapi_io", "getxapi", "twscrape", "fxembed"
]

# Project root: search/
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    twitter_backend: BackendName = Field(default="twitterapi_io", alias="TWITTER_BACKEND")
    x_provider_chain: str = Field(
        default="fxembed,getxapi", alias="KOL_X_PROVIDER_CHAIN"
    )
    enable_mock_backend: bool = Field(default=False, alias="KOL_ENABLE_MOCK_BACKEND")
    enabled_platforms: str = Field(default="x", alias="KOL_ENABLED_PLATFORMS")

    x_bearer_token: str | None = Field(default=None, alias="X_BEARER_TOKEN")
    x_bearer_token_keychain_service: str | None = Field(
        default=None, alias="X_BEARER_TOKEN_KEYCHAIN_SERVICE"
    )
    x_api_key: str | None = Field(default=None, alias="X_API_KEY")
    x_api_secret: str | None = Field(default=None, alias="X_API_SECRET")
    x_access_token: str | None = Field(default=None, alias="X_ACCESS_TOKEN")
    x_access_token_secret: str | None = Field(default=None, alias="X_ACCESS_TOKEN_SECRET")

    twitter_tp_base_url: str | None = Field(default=None, alias="TWITTER_TP_BASE_URL")
    twitter_tp_api_key: str | None = Field(default=None, alias="TWITTER_TP_API_KEY")
    twitter_tp_api_key_header: str = Field(default="x-api-key", alias="TWITTER_TP_API_KEY_HEADER")
    twitter_tp_supports_user_search: bool = Field(
        default=False, alias="TWITTER_TP_SUPPORTS_USER_SEARCH"
    )

    twitterapi_io_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TWITTERAPI_IO_API_KEY", "API_KEY"),
    )
    twitterapi_io_base_url: str = Field(
        default="https://api.twitterapi.io",
        alias="TWITTERAPI_IO_BASE_URL",
    )

    get_x_api_key: str | None = Field(default=None, alias="GET_X_API_KEY")
    get_x_api_base_url: str = Field(
        default="https://api.getxapi.com", alias="GET_X_API_BASE_URL"
    )
    get_x_api_daily_call_limit: int = Field(
        default=120, ge=1, alias="GET_X_API_DAILY_CALL_LIMIT"
    )
    get_x_api_min_credits: float = Field(
        default=0.05, ge=0, alias="GET_X_API_MIN_CREDITS"
    )
    get_x_api_balance_cache_seconds: int = Field(
        default=300, ge=0, alias="GET_X_API_BALANCE_CACHE_SECONDS"
    )
    get_x_api_estimated_credits_per_call: float = Field(
        default=0.001, ge=0, alias="GET_X_API_ESTIMATED_CREDITS_PER_CALL"
    )

    twscrape_accounts_file: str | None = Field(default=None, alias="TWSCRAPE_ACCOUNTS_FILE")
    twscrape_accounts_db: str = Field(
        default="data/twscrape_accounts.db", alias="TWSCRAPE_ACCOUNTS_DB"
    )
    twscrape_username: str = Field(default="kol-search-test", alias="TWSCRAPE_USERNAME")
    twscrape_auth_token: str | None = Field(default=None, alias="TWSCRAPE_AUTH_TOKEN")
    twscrape_ct0: str | None = Field(default=None, alias="TWSCRAPE_CT0")
    twscrape_wait_timeout_seconds: float = Field(
        default=15.0, alias="TWSCRAPE_WAIT_TIMEOUT_SECONDS"
    )
    enable_twscrape: bool = Field(default=False, alias="ENABLE_TWSCRAPE")

    enable_fxembed: bool = Field(default=True, alias="ENABLE_FXEMBED")
    fxembed_base_url: str = Field(
        default="https://api.fxtwitter.com", alias="FXEMBED_BASE_URL"
    )
    fxembed_timeout_seconds: float = Field(
        default=20.0, alias="FXEMBED_TIMEOUT_SECONDS"
    )

    backend_api_token: str | None = Field(default=None, alias="KOL_BACKEND_API_TOKEN")
    backend_host: str = Field(default="127.0.0.1", alias="KOL_BACKEND_HOST")
    backend_port: int = Field(default=8780, alias="KOL_BACKEND_PORT")
    backend_url: str | None = Field(default=None, alias="KOL_BACKEND_URL")
    backend_request_timeout_seconds: float = Field(
        default=30.0, alias="KOL_BACKEND_REQUEST_TIMEOUT_SECONDS"
    )
    x_hot_content_query: str = Field(
        default=(
            "(AI OR crypto OR technology OR startups) min_faves:100 "
            "-filter:replies lang:en"
        ),
        alias="KOL_X_HOT_CONTENT_QUERY",
    )
    x_hot_content_limit: int = Field(default=12, alias="KOL_X_HOT_CONTENT_LIMIT")
    x_watch_handles: str = Field(default="", alias="KOL_X_WATCH_HANDLES")
    x_watch_posts_per_account: int = Field(
        default=3, alias="KOL_X_WATCH_POSTS_PER_ACCOUNT"
    )
    x_kol_discovery_domain: str = Field(
        default="AI agents", alias="KOL_X_KOL_DISCOVERY_DOMAIN"
    )
    x_kol_discovery_limit: int = Field(
        default=8, alias="KOL_X_KOL_DISCOVERY_LIMIT"
    )
    x_kol_candidate_sample_size: int = Field(
        default=20, alias="KOL_X_KOL_CANDIDATE_SAMPLE_SIZE"
    )
    x_kol_recent_posts_per_account: int = Field(
        default=2, alias="KOL_X_KOL_RECENT_POSTS_PER_ACCOUNT"
    )
    x_kol_min_engagement: int = Field(
        default=25, alias="KOL_X_KOL_MIN_ENGAGEMENT"
    )
    x_kol_discovery_cache_seconds: int = Field(
        default=3600, alias="KOL_X_KOL_DISCOVERY_CACHE_SECONDS"
    )
    x_trend_cache_seconds: int = Field(
        default=1800, ge=0, alias="KOL_X_TREND_CACHE_SECONDS"
    )
    x_content_cache_seconds: int = Field(
        default=1800, ge=0, alias="KOL_X_CONTENT_CACHE_SECONDS"
    )
    x_timeline_cache_seconds: int = Field(
        default=1800, ge=0, alias="KOL_X_TIMELINE_CACHE_SECONDS"
    )
    x_content_lookup_cache_seconds: int = Field(
        default=3600, ge=0, alias="KOL_X_CONTENT_LOOKUP_CACHE_SECONDS"
    )
    x_snapshot_stale_seconds: int = Field(
        default=86_400, ge=0, alias="KOL_X_SNAPSHOT_STALE_SECONDS"
    )

    discovery_domains_json: str = Field(
        default=(
            '[{"key":"ai","name":"AI","query":"AI agents OR LLM OR '
            'machine learning"},{"key":"crypto","name":"Crypto","query":"crypto '
            'OR bitcoin OR ethereum OR DeFi"},{"key":"financial","name":"Financial",'
            '"query":"financial markets OR investing OR macroeconomics"}]'
        ),
        alias="KOL_DISCOVERY_DOMAINS_JSON",
    )
    auto_watchlist_enabled: bool = Field(
        default=True, alias="KOL_AUTO_WATCHLIST_ENABLED"
    )
    auto_watchlist_min_score: int = Field(
        default=75, ge=0, le=100, alias="KOL_AUTO_WATCHLIST_MIN_SCORE"
    )
    auto_watchlist_min_evidence: int = Field(
        default=3, ge=1, le=20, alias="KOL_AUTO_WATCHLIST_MIN_EVIDENCE"
    )
    auto_watchlist_qualifying_runs: int = Field(
        default=2, ge=1, le=10, alias="KOL_AUTO_WATCHLIST_QUALIFYING_RUNS"
    )
    auto_watchlist_max_per_domain: int = Field(
        default=20, ge=1, le=200, alias="KOL_AUTO_WATCHLIST_MAX_PER_DOMAIN"
    )
    hot_content_interval_minutes: int = Field(
        default=30, ge=5, le=1440, alias="KOL_HOT_CONTENT_INTERVAL_MINUTES"
    )
    hot_content_window_hours: int = Field(
        default=48, ge=1, le=168, alias="KOL_HOT_CONTENT_WINDOW_HOURS"
    )
    hot_content_min_engagement: int = Field(
        default=100, ge=0, alias="KOL_HOT_CONTENT_MIN_ENGAGEMENT"
    )
    hot_following_min_engagement: int = Field(
        default=20, ge=0, alias="KOL_HOT_FOLLOWING_MIN_ENGAGEMENT"
    )

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.6-luna", alias="OPENAI_MODEL")
    deep_seek_api_key: str | None = Field(default=None, alias="DEEP_SEEK_API_KEY")
    deep_seek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEP_SEEK_BASE_URL"
    )
    deep_seek_model: str = Field(default="deepseek-chat", alias="DEEP_SEEK_MODEL")
    deep_seek_timeout_seconds: float = Field(
        default=30.0, ge=1.0, le=120.0, alias="DEEP_SEEK_TIMEOUT_SECONDS"
    )
    reply_draft_daily_limit: int = Field(
        default=50, ge=1, le=1000, alias="KOL_REPLY_DRAFT_DAILY_LIMIT"
    )
    reply_draft_cache_seconds: int = Field(
        default=600, ge=0, le=86_400, alias="KOL_REPLY_DRAFT_CACHE_SECONDS"
    )

    web_host: str = Field(default="127.0.0.1", alias="KOL_WEB_HOST")
    web_port: int = Field(default=8765, alias="KOL_WEB_PORT")
    timezone: str = Field(default="Asia/Shanghai", alias="KOL_TIMEZONE")
    enable_signal_scan: bool = Field(default=True, alias="KOL_ENABLE_SIGNAL_SCAN")
    signal_interval_minutes: int = Field(default=30, alias="KOL_SIGNAL_INTERVAL_MINUTES")
    x_signal_interval_minutes: int | None = Field(
        default=None, alias="KOL_X_SIGNAL_INTERVAL_MINUTES"
    )
    xiaohongshu_signal_interval_minutes: int | None = Field(
        default=None, alias="KOL_XIAOHONGSHU_SIGNAL_INTERVAL_MINUTES"
    )
    x_trends_per_scan: int = Field(default=5, alias="KOL_X_TRENDS_PER_SCAN")
    x_tweets_per_trend: int = Field(default=10, alias="KOL_X_TWEETS_PER_TREND")
    x_trend_woeid: int = Field(default=1, alias="KOL_X_TREND_WOEID")
    signal_posts_per_account: int = Field(default=20, alias="KOL_SIGNAL_POSTS_PER_ACCOUNT")
    signal_reply_limit: int = Field(default=20, alias="KOL_SIGNAL_REPLY_LIMIT")
    platform_account_batch_size: int = Field(
        default=50, alias="KOL_PLATFORM_ACCOUNT_BATCH_SIZE"
    )
    signal_opportunity_ttl_hours: int = Field(
        default=24, alias="KOL_SIGNAL_OPPORTUNITY_TTL_HOURS"
    )
    discovery_interval_hours: int = Field(
        default=24, alias="KOL_DISCOVERY_INTERVAL_HOURS"
    )
    inactive_pause_days: int = Field(default=90, alias="KOL_INACTIVE_PAUSE_DAYS")
    review_queue_enabled: bool = Field(
        default=False, alias="KOL_REVIEW_QUEUE_ENABLED"
    )
    action_dispatch_seconds: int = Field(
        default=60, alias="KOL_ACTION_DISPATCH_SECONDS"
    )

    admin_username: str = Field(default="admin", alias="KOL_ADMIN_USERNAME")
    admin_password: str | None = Field(default=None, alias="KOL_ADMIN_PASSWORD")
    session_secret: str | None = Field(default=None, alias="KOL_SESSION_SECRET")
    live_write_enabled: bool = Field(default=False, alias="KOL_LIVE_WRITE_ENABLED")
    approved_product_domains: str = Field(default="", alias="KOL_APPROVED_PRODUCT_DOMAINS")
    postiz_api_url: str = Field(
        default="https://api.postiz.com/public/v1", alias="POSTIZ_API_URL"
    )
    postiz_api_key: str | None = Field(default=None, alias="POSTIZ_API_KEY")
    comment_daily_limit: int = Field(default=10, alias="KOL_COMMENT_DAILY_LIMIT")
    comment_hourly_limit: int = Field(default=3, alias="KOL_COMMENT_HOURLY_LIMIT")
    dm_daily_limit: int = Field(default=5, alias="KOL_DM_DAILY_LIMIT")
    dm_hourly_limit: int = Field(default=2, alias="KOL_DM_HOURLY_LIMIT")
    author_cooldown_days: int = Field(default=7, alias="KOL_AUTHOR_COOLDOWN_DAYS")
    global_kill_switch: bool = Field(default=False, alias="KOL_GLOBAL_KILL_SWITCH")
    auto_execution_enabled: bool = Field(
        default=False, alias="KOL_AUTO_EXECUTION_ENABLED"
    )
    auto_comment_score: float = Field(default=80.0, alias="KOL_AUTO_COMMENT_SCORE")
    auto_dm_followup_score: float = Field(
        default=80.0, alias="KOL_AUTO_DM_FOLLOWUP_SCORE"
    )
    auto_publish_score: float = Field(default=80.0, alias="KOL_AUTO_PUBLISH_SCORE")
    publish_daily_limit: int = Field(default=2, alias="KOL_PUBLISH_DAILY_LIMIT")
    publish_windows: str = Field(
        default="09:00-11:00,17:00-20:00", alias="KOL_PUBLISH_WINDOWS"
    )

    kol_db_path: str = Field(default="data/kol_search.db", alias="KOL_DB_PATH")
    database_url: str | None = Field(default=None, alias="KOL_DATABASE_URL")
    provider_state_db_path: Path | None = Field(
        default=None, alias="KOL_PROVIDER_STATE_DB_PATH"
    )
    database_keychain_service: str | None = Field(
        default=None, alias="KOL_DATABASE_URL_KEYCHAIN_SERVICE"
    )
    web_read_only: bool = Field(default=False, alias="KOL_WEB_READ_ONLY")
    worker_id: str = "legacy-worker"

    def db_path(self) -> Path:
        p = Path(self.kol_db_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def database_target(self) -> str | Path:
        if self.database_url:
            return self.database_url
        if self.database_keychain_service:
            return self._keychain_secret(
                self.database_keychain_service,
                setting_name="KOL_DATABASE_URL_KEYCHAIN_SERVICE",
            )
        return self.db_path()

    def provider_state_database_target(self) -> str | Path:
        """Use a single-node durable cache DB when explicitly configured."""

        if self.provider_state_db_path is not None:
            return self.provider_state_db_path.expanduser()
        return self.database_target()

    def x_api_bearer_token(self) -> str | None:
        if self.x_bearer_token:
            return self.x_bearer_token
        if not self.x_bearer_token_keychain_service:
            return None
        return self._keychain_secret(
            self.x_bearer_token_keychain_service,
            setting_name="X_BEARER_TOKEN_KEYCHAIN_SERVICE",
        )

    @staticmethod
    def _keychain_secret(service: str, *, setting_name: str) -> str:
        try:
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-w",
                    "-s",
                    service,
                    "-a",
                    "kol-search",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"无法从 macOS 钥匙串读取密钥；请检查 {setting_name}"
            ) from exc
        value = result.stdout.strip()
        if not value:
            raise RuntimeError(f"{setting_name} 对应的 macOS 钥匙串内容为空")
        return value

    def backend_ready(self, name: str) -> tuple[bool, str | None]:
        if name == "mock":
            return (
                self.enable_mock_backend,
                None
                if self.enable_mock_backend
                else "Mock 后端仅限测试；如确需使用，请显式设置 KOL_ENABLE_MOCK_BACKEND=true",
            )
        if name == "official":
            try:
                ready = bool(self.x_api_bearer_token())
            except RuntimeError as exc:
                return False, str(exc)
            return ready, None if ready else "缺少 X_BEARER_TOKEN"
        if name == "third_party":
            ready = bool(self.twitter_tp_base_url and self.twitter_tp_api_key)
            return ready, None if ready else "缺少 TWITTER_TP_BASE_URL 或 TWITTER_TP_API_KEY"
        if name == "twitterapi_io":
            ready = bool(self.twitterapi_io_api_key)
            return ready, None if ready else "缺少 TWITTERAPI_IO_API_KEY"
        if name == "getxapi":
            ready = bool(self.get_x_api_key)
            return ready, None if ready else "缺少 GET_X_API_KEY"
        if name == "twscrape":
            db_path = Path(self.twscrape_accounts_db)
            if not db_path.is_absolute():
                db_path = PROJECT_ROOT / db_path
            has_cookie = bool(self.twscrape_auth_token and self.twscrape_ct0)
            has_state = db_path.exists() or bool(self.twscrape_accounts_file)
            ready = bool(self.enable_twscrape and (has_cookie or has_state))
            return (
                ready,
                None
                if ready
                else "需要 ENABLE_TWSCRAPE=true，并配置 Cookie 或已有账号状态库",
            )
        if name == "fxembed":
            ready = bool(self.enable_fxembed and self.fxembed_base_url)
            return ready, None if ready else "FxEmbed connector is disabled"
        return False, "未知后端"

    def twscrape_db_path(self) -> Path:
        path = Path(self.twscrape_accounts_db)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    def x_provider_names(self) -> tuple[str, ...]:
        values = [
            value.strip().lower()
            for value in self.x_provider_chain.split(",")
            if value.strip()
        ]
        supported = {
            "mock", "official", "third_party", "twitterapi_io", "getxapi", "twscrape", "fxembed"
        }
        unknown = set(values) - supported
        if unknown:
            raise ValueError(
                f"Unknown KOL_X_PROVIDER_CHAIN providers: {', '.join(sorted(unknown))}"
            )
        if not values:
            values = [self.twitter_backend]
        return tuple(dict.fromkeys(values))

    def product_domain_allowlist(self) -> set[str]:
        return {
            value.strip().lower()
            for value in self.approved_product_domains.split(",")
            if value.strip()
        }

    def platform_ids(self) -> tuple[str, ...]:
        """Configured platform IDs, preserving order and removing duplicates."""
        values = [value.strip().lower() for value in self.enabled_platforms.split(",")]
        return tuple(dict.fromkeys(value for value in values if value))

    def publishing_windows(self) -> tuple[str, ...]:
        return tuple(
            value.strip() for value in self.publish_windows.split(",") if value.strip()
        )

    def postiz_ready(self) -> tuple[bool, str | None]:
        return (
            bool(self.postiz_api_key),
            None if self.postiz_api_key else "缺少 POSTIZ_API_KEY",
        )


def get_settings() -> Settings:
    return Settings()
