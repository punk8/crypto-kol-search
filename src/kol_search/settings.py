from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BackendName = Literal["mock", "official", "third_party", "twitterapi_io", "twscrape", "opencli"]
RuntimeMode = Literal["combined", "web", "worker"]

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
    enabled_platforms: str = Field(
        default="x,xiaohongshu", alias="KOL_ENABLED_PLATFORMS"
    )

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
    database_keychain_service: str | None = Field(
        default=None, alias="KOL_DATABASE_URL_KEYCHAIN_SERVICE"
    )
    runtime_mode: RuntimeMode = Field(default="combined", alias="KOL_RUNTIME_MODE")
    web_read_only: bool = Field(default=False, alias="KOL_WEB_READ_ONLY")
    worker_id: str = Field(default="mac-worker", alias="KOL_WORKER_ID")

    def db_path(self) -> Path:
        p = Path(self.kol_db_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def database_target(self) -> str | Path:
        if self.database_url:
            return self.database_url
        if self.database_keychain_service:
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/security",
                        "find-generic-password",
                        "-w",
                        "-s",
                        self.database_keychain_service,
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
                    "无法从 macOS 钥匙串读取数据库连接；请检查 "
                    "KOL_DATABASE_URL_KEYCHAIN_SERVICE"
                ) from exc
            value = result.stdout.strip()
            if not value:
                raise RuntimeError("macOS 钥匙串中的数据库连接为空")
            return value
        return self.db_path()

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
