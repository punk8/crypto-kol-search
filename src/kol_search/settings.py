from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BackendName = Literal["mock", "official", "third_party", "twitterapi_io", "twscrape", "opencli"]

# Project root: search/
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    twitter_backend: BackendName = Field(default="twitterapi_io", alias="TWITTER_BACKEND")
    enable_mock_backend: bool = Field(default=False, alias="KOL_ENABLE_MOCK_BACKEND")

    x_bearer_token: str | None = Field(default=None, alias="X_BEARER_TOKEN")
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

    twscrape_accounts_file: str | None = Field(default=None, alias="TWSCRAPE_ACCOUNTS_FILE")
    enable_twscrape: bool = Field(default=False, alias="ENABLE_TWSCRAPE")

    opencli_command: str = Field(default="opencli", alias="OPENCLI_COMMAND")
    opencli_profile: str = Field(default="ddd", alias="OPENCLI_PROFILE")
    xiaohongshu_opencli_profile: str = Field(
        default="ddd", alias="XIAOHONGSHU_OPENCLI_PROFILE"
    )
    opencli_timeout_seconds: float = Field(default=90.0, alias="OPENCLI_TIMEOUT_SECONDS")
    twitter_fallback_backend: str | None = Field(
        default="opencli", alias="TWITTER_FALLBACK_BACKEND"
    )

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.6-luna", alias="OPENAI_MODEL")

    web_host: str = Field(default="127.0.0.1", alias="KOL_WEB_HOST")
    web_port: int = Field(default=8765, alias="KOL_WEB_PORT")
    timezone: str = Field(default="Asia/Shanghai", alias="KOL_TIMEZONE")
    weekly_day: str = Field(default="sun", alias="KOL_WEEKLY_DAY")
    weekly_hour: int = Field(default=3, alias="KOL_WEEKLY_HOUR")
    enable_weekly_refresh: bool = Field(default=True, alias="KOL_ENABLE_WEEKLY_REFRESH")
    enable_signal_scan: bool = Field(default=False, alias="KOL_ENABLE_SIGNAL_SCAN")
    signal_interval_minutes: int = Field(default=30, alias="KOL_SIGNAL_INTERVAL_MINUTES")
    signal_core_accounts: int = Field(default=20, alias="KOL_SIGNAL_CORE_ACCOUNTS")
    signal_rotation_accounts: int = Field(default=20, alias="KOL_SIGNAL_ROTATION_ACCOUNTS")
    signal_posts_per_account: int = Field(default=20, alias="KOL_SIGNAL_POSTS_PER_ACCOUNT")
    signal_reply_limit: int = Field(default=20, alias="KOL_SIGNAL_REPLY_LIMIT")
    signal_topic_limit: int = Field(default=10, alias="KOL_SIGNAL_TOPIC_LIMIT")
    signal_opportunity_ttl_hours: int = Field(
        default=24, alias="KOL_SIGNAL_OPPORTUNITY_TTL_HOURS"
    )
    trend_cache_minutes: int = Field(default=15, alias="KOL_TREND_CACHE_MINUTES")

    admin_username: str = Field(default="admin", alias="KOL_ADMIN_USERNAME")
    admin_password: str | None = Field(default=None, alias="KOL_ADMIN_PASSWORD")
    session_secret: str | None = Field(default=None, alias="KOL_SESSION_SECRET")
    live_write_enabled: bool = Field(default=False, alias="KOL_LIVE_WRITE_ENABLED")
    approved_product_domains: str = Field(default="", alias="KOL_APPROVED_PRODUCT_DOMAINS")
    postiz_api_url: str = Field(
        default="https://api.postiz.com/public/v1", alias="POSTIZ_API_URL"
    )
    postiz_api_key: str | None = Field(default=None, alias="POSTIZ_API_KEY")
    postiz_integration_cache_minutes: int = Field(
        default=15, alias="POSTIZ_INTEGRATION_CACHE_MINUTES"
    )
    publishing_media_dir: str = Field(
        default="data/publishing_media", alias="KOL_PUBLISHING_MEDIA_DIR"
    )
    comment_daily_limit: int = Field(default=10, alias="KOL_COMMENT_DAILY_LIMIT")
    comment_hourly_limit: int = Field(default=3, alias="KOL_COMMENT_HOURLY_LIMIT")
    dm_daily_limit: int = Field(default=5, alias="KOL_DM_DAILY_LIMIT")
    dm_hourly_limit: int = Field(default=2, alias="KOL_DM_HOURLY_LIMIT")
    author_cooldown_days: int = Field(default=7, alias="KOL_AUTHOR_COOLDOWN_DAYS")
    global_kill_switch: bool = Field(default=False, alias="KOL_GLOBAL_KILL_SWITCH")

    max_user_queries: int = Field(default=8, alias="KOL_MAX_USER_QUERIES")
    max_post_queries: int = Field(default=8, alias="KOL_MAX_POST_QUERIES")
    max_candidates: int = Field(default=500, alias="KOL_MAX_CANDIDATES")
    max_enriched_candidates: int = Field(default=200, alias="KOL_MAX_ENRICHED_CANDIDATES")
    max_contact_accounts: int = Field(default=100, alias="KOL_MAX_CONTACT_ACCOUNTS")
    max_site_pages: int = Field(default=5, alias="KOL_MAX_SITE_PAGES")
    use_configured_seeds: bool = Field(default=True, alias="KOL_USE_CONFIGURED_SEEDS")

    kol_db_path: str = Field(default="data/kol_search.db", alias="KOL_DB_PATH")
    kol_output_dir: str = Field(default="output", alias="KOL_OUTPUT_DIR")

    def db_path(self) -> Path:
        p = Path(self.kol_db_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def output_dir(self) -> Path:
        p = Path(self.kol_output_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def publishing_media_path(self) -> Path:
        p = Path(self.publishing_media_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def backend_ready(self, name: str) -> tuple[bool, str | None]:
        if name == "mock":
            return (
                self.enable_mock_backend,
                None
                if self.enable_mock_backend
                else "Mock 后端仅限测试；如确需使用，请显式设置 KOL_ENABLE_MOCK_BACKEND=true",
            )
        if name == "official":
            return (bool(self.x_bearer_token), None if self.x_bearer_token else "缺少 X_BEARER_TOKEN")
        if name == "third_party":
            ready = bool(self.twitter_tp_base_url and self.twitter_tp_api_key)
            return ready, None if ready else "缺少 TWITTER_TP_BASE_URL 或 TWITTER_TP_API_KEY"
        if name == "twitterapi_io":
            ready = bool(self.twitterapi_io_api_key)
            fallback_ready = False
            if not ready and self.twitter_fallback_backend == "opencli":
                fallback_ready, _ = self.backend_ready("opencli")
            ready = ready or fallback_ready
            return ready, None if ready else "缺少 TWITTERAPI_IO_API_KEY，且 OpenCLI fallback 未配置"
        if name == "twscrape":
            ready = bool(self.enable_twscrape and self.twscrape_accounts_file)
            return ready, None if ready else "需要 ENABLE_TWSCRAPE=true 和账号文件"
        if name == "opencli":
            if not self.opencli_command or not self.opencli_profile:
                return False, "缺少 OPENCLI_COMMAND 或 OPENCLI_PROFILE"
            if not shutil.which(self.opencli_command) and "/" not in self.opencli_command:
                return False, f"找不到 OpenCLI 命令：{self.opencli_command}"
            try:
                result = subprocess.run(
                    [self.opencli_command, "profile", "list"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, f"OpenCLI 状态检查失败：{exc}"
            output = f"{result.stdout}\n{result.stderr}"
            ready = (
                result.returncode == 0
                and self.opencli_profile in output
                and "connected" in output
            )
            return (
                ready,
                None if ready else f"OpenCLI profile {self.opencli_profile!r} 未连接",
            )
        return False, "未知后端"

    def xiaohongshu_ready(self) -> tuple[bool, str | None]:
        if not self.opencli_command or not self.xiaohongshu_opencli_profile:
            return False, "缺少 OPENCLI_COMMAND 或 XIAOHONGSHU_OPENCLI_PROFILE"
        if not shutil.which(self.opencli_command) and "/" not in self.opencli_command:
            return False, f"找不到 OpenCLI 命令：{self.opencli_command}"
        try:
            result = subprocess.run(
                [self.opencli_command, "profile", "list"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"OpenCLI 状态检查失败：{exc}"
        output = f"{result.stdout}\n{result.stderr}"
        ready = (
            result.returncode == 0
            and self.xiaohongshu_opencli_profile in output
            and "connected" in output
        )
        return (
            ready,
            None
            if ready
            else f"OpenCLI profile {self.xiaohongshu_opencli_profile!r} 未连接",
        )

    def product_domain_allowlist(self) -> set[str]:
        return {
            value.strip().lower()
            for value in self.approved_product_domains.split(",")
            if value.strip()
        }

    def postiz_ready(self) -> tuple[bool, str | None]:
        return (
            bool(self.postiz_api_key),
            None if self.postiz_api_key else "缺少 POSTIZ_API_KEY",
        )


def get_settings() -> Settings:
    return Settings()
