from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from kol_search.db import Store
from kol_search.settings import Settings


class OutboundError(RuntimeError):
    pass


class OutboundConfirmationRequired(OutboundError):
    pass


class TargetNotMessageable(OutboundError):
    pass


@dataclass
class OutboundReceipt:
    status: str
    url: str | None = None
    external_id: str | None = None
    raw: dict[str, Any] | None = None


class PlatformWriter(Protocol):
    def reply(self, action: dict[str, Any]) -> OutboundReceipt: ...

    def send_dm(self, action: dict[str, Any]) -> OutboundReceipt: ...

    def verify_receipt(self, action: dict[str, Any], receipt: OutboundReceipt) -> bool: ...


def outbound_idempotency_key(
    *, platform: str, kind: str, target_external_id: str, text: str
) -> str:
    material = "\x1f".join(
        (platform, kind, target_external_id, " ".join(text.split()).lower())
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class OpenCliBrowserWriter:
    """Visible, exact-target browser writer. It never retries a write operation."""

    def __init__(
        self,
        *,
        command: str = "opencli",
        timeout: float = 90.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.command = command
        self.timeout = timeout
        self._runner = runner

    def _run(self, profile: str, *args: str, expect_json: bool = True) -> Any:
        if not shutil.which(self.command) and "/" not in self.command:
            raise OutboundError(f"OpenCLI command not found: {self.command}")
        command = [self.command, "--profile", profile, *args]
        try:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OutboundConfirmationRequired("浏览器操作超时；请人工确认平台是否已写入") from exc
        except OSError as exc:
            raise OutboundError(f"OpenCLI 启动失败：{exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()[-1200:]
            lowered = detail.lower()
            if any(token in lowered for token in ("captcha", "verify", "风控", "验证")):
                raise OutboundError(f"平台要求验证，账号必须暂停：{detail}")
            if any(token in lowered for token in ("not messageable", "cannot message", "无法私信")):
                raise TargetNotMessageable(detail)
            raise OutboundError(detail)
        if not expect_json:
            return result.stdout
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return {"stdout": result.stdout.strip()}

    def _whoami(self, action: dict[str, Any]) -> None:
        site = "twitter" if action["platform"] == "x" else "xiaohongshu"
        payload = self._run(
            action["browser_profile"], site, "whoami", "-f", "json"
        )
        rows = payload if isinstance(payload, list) else [payload]
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        observed = str(
            row.get("screen_name")
            or row.get("username")
            or row.get("userId")
            or row.get("nickname")
            or row.get("name")
            or ""
        ).lstrip("@")
        expected = {
            str(action.get("managed_username") or "").lstrip("@").lower(),
            str(action.get("managed_external_account_id") or "").lower(),
        }
        if not observed or observed.lower() not in expected:
            raise OutboundError(
                f"登录账号不匹配：期望 {action.get('managed_username')}，实际 {observed or '未知'}"
            )

    def reply(self, action: dict[str, Any]) -> OutboundReceipt:
        self._whoami(action)
        if not action.get("target_url"):
            raise OutboundError("评论任务缺少目标链接")
        text = str(action.get("final_text") or "")
        if action["platform"] == "x":
            payload = self._run(
                action["browser_profile"],
                "twitter",
                "reply",
                action["target_url"],
                text,
                "--window",
                "foreground",
                "-f",
                "json",
            )
            row = payload[0] if isinstance(payload, list) and payload else payload
            url = row.get("url") if isinstance(row, dict) else None
            return OutboundReceipt(status="sent", url=url, raw={"opencli": payload})
        session = f"kol-xhs-comment-{action['id']}"
        self._run(
            action["browser_profile"], "browser", session, "open", action["target_url"]
        )
        self._fill_first(
            action["browser_profile"],
            session,
            text,
            (("--role", "textbox", "--name", "说点什么"), ("textarea",)),
        )
        self._click_first(
            action["browser_profile"],
            session,
            (("--role", "button", "--name", "发送"), ("--text", "发送")),
        )
        return OutboundReceipt(
            status="sent",
            url=action["target_url"],
            external_id=hashlib.sha256(
                f"{action['target_external_id']}:{text}".encode("utf-8")
            ).hexdigest()[:20],
            raw={"browser_session": session},
        )

    def send_dm(self, action: dict[str, Any]) -> OutboundReceipt:
        self._whoami(action)
        target_url = action.get("target_url")
        if not target_url:
            raise OutboundError("私信任务缺少目标用户主页或会话链接")
        text = str(action.get("final_text") or "")
        session = f"kol-{action['platform']}-dm-{action['id']}"
        self._run(action["browser_profile"], "browser", session, "open", target_url)
        if action["platform"] == "x":
            self._click_first(
                action["browser_profile"],
                session,
                (
                    ("--role", "button", "--name", "Message"),
                    ("--role", "button", "--name", "私信"),
                    ("--text", "Message"),
                ),
                not_messageable=True,
            )
            fields = (
                ("--role", "textbox", "--name", "Message"),
                ("[data-testid='dmComposerTextInput']",),
            )
            sends = (
                ("--role", "button", "--name", "Send"),
                ("--role", "button", "--name", "发送"),
                ("[data-testid='dmComposerSendButton']",),
            )
        else:
            self._click_first(
                action["browser_profile"],
                session,
                (("--role", "button", "--name", "私信"), ("--text", "私信")),
                not_messageable=True,
            )
            fields = (
                ("--role", "textbox", "--name", "发送消息"),
                ("textarea",),
                ("[contenteditable='true']",),
            )
            sends = (("--role", "button", "--name", "发送"), ("--text", "发送"))
        self._fill_first(action["browser_profile"], session, text, fields)
        self._click_first(action["browser_profile"], session, sends)
        return OutboundReceipt(
            status="sent",
            url=target_url,
            external_id=hashlib.sha256(
                f"{action['target_external_id']}:{text}".encode("utf-8")
            ).hexdigest()[:20],
            raw={"browser_session": session},
        )

    def verify_receipt(self, action: dict[str, Any], receipt: OutboundReceipt) -> bool:
        if action["platform"] == "x" and action["kind"] == "comment":
            return bool(receipt.url and re.search(r"/status/\d+", receipt.url))
        session = (receipt.raw or {}).get("browser_session")
        if not session:
            return False
        payload = self._run(
            action["browser_profile"], "browser", session, "get", "text", "body"
        )
        page_text = str(payload.get("value") if isinstance(payload, dict) else payload)
        return str(action.get("final_text") or "") in page_text

    def _fill_first(
        self,
        profile: str,
        session: str,
        text: str,
        selectors: tuple[tuple[str, ...], ...],
    ) -> None:
        errors: list[str] = []
        for selector in selectors:
            try:
                self._run(profile, "browser", session, "fill", *selector, text)
                return
            except OutboundError as exc:
                errors.append(str(exc))
        raise OutboundError("找不到消息输入框：" + " | ".join(errors[-2:]))

    def _click_first(
        self,
        profile: str,
        session: str,
        selectors: tuple[tuple[str, ...], ...],
        *,
        not_messageable: bool = False,
    ) -> None:
        errors: list[str] = []
        for selector in selectors:
            try:
                self._run(profile, "browser", session, "click", *selector)
                return
            except OutboundError as exc:
                errors.append(str(exc))
        message = "找不到目标操作按钮：" + " | ".join(errors[-2:])
        if not_messageable:
            raise TargetNotMessageable(message)
        raise OutboundError(message)


class OutboundService:
    def __init__(self, store: Store, settings: Settings, writer: PlatformWriter | None = None) -> None:
        self.store = store
        self.settings = settings
        self.writer = writer or OpenCliBrowserWriter(
            command=settings.opencli_command,
            timeout=settings.opencli_timeout_seconds,
        )

    def execute(self, action_id: int, *, actor: str = "admin") -> OutboundReceipt:
        action = self.store.get_outbound_action(action_id)
        if not action:
            raise KeyError(action_id)
        self._validate(action)
        action = self.store.claim_outbound_action(action_id)
        try:
            receipt = (
                self.writer.reply(action)
                if action["kind"] == "comment"
                else self.writer.send_dm(action)
            )
            if not self.writer.verify_receipt(action, receipt):
                raise OutboundConfirmationRequired("平台没有返回可确认的写入回执")
            self.store.finish_outbound_action(
                action_id,
                status="sent",
                receipt_url=receipt.url,
                receipt_external_id=receipt.external_id,
                receipt=receipt.raw,
                actor=actor,
            )
            self.store.save_managed_account_health(action["managed_account_id"], ready=True)
            return receipt
        except TargetNotMessageable as exc:
            self.store.finish_outbound_action(
                action_id, status="target_not_messageable", error=str(exc), actor=actor
            )
            raise
        except OutboundConfirmationRequired as exc:
            self.store.finish_outbound_action(
                action_id, status="confirmation_required", error=str(exc), actor=actor
            )
            raise
        except Exception as exc:
            self.store.finish_outbound_action(
                action_id, status="failed", error=str(exc), actor=actor
            )
            self.store.save_managed_account_health(
                action["managed_account_id"], ready=False, error=str(exc)
            )
            raise

    def _validate(self, action: dict[str, Any]) -> None:
        if not self.settings.live_write_enabled:
            raise OutboundError("真实写入已关闭；设置 KOL_LIVE_WRITE_ENABLED=true 后才可执行")
        db_switch = self.store.get_control("global_kill_switch", "false").lower() == "true"
        if self.settings.global_kill_switch or db_switch:
            raise OutboundError("全局 Kill Switch 已开启")
        if action["status"] != "approved":
            raise OutboundError("任务尚未逐条审批")
        if action["managed_account_status"] != "active":
            raise OutboundError("发送账号未启用")
        text = str(action.get("final_text") or "").strip()
        if not text:
            raise OutboundError("最终文案不能为空")
        if action["kind"] == "comment" and re.search(r"https?://", text):
            raise OutboundError("推广评论默认禁止链接")
        if action["kind"] == "comment" and self.store.has_sent_target(
            platform=action["platform"],
            kind="comment",
            target_external_id=action["target_external_id"],
        ):
            raise OutboundError("同一帖子已经由一个授权账号评论")
        if action["kind"] == "dm":
            self._validate_dm_links(text)
            if not action.get("conversation_id") and self.store.has_sent_target(
                platform=action["platform"],
                kind="dm",
                target_external_id=action["target_external_id"],
            ):
                raise OutboundError("该目标已有未标记会话的首次触达")
        usage = self.store.outbound_usage(action["managed_account_id"], action["kind"])
        prefix = "comment" if action["kind"] == "comment" else "dm"
        hourly = int(action[f"{prefix}_hourly_limit"])
        daily = int(action[f"{prefix}_daily_limit"])
        if usage["hourly"] >= hourly or usage["daily"] >= daily:
            raise OutboundError(
                f"账号频控已达上限：过去1小时 {usage['hourly']}/{hourly}，"
                f"过去24小时 {usage['daily']}/{daily}"
            )
        author = str(action.get("target_author_id") or action["target_external_id"])
        if self.store.has_recent_author_contact(
            platform=action["platform"],
            target_author_id=author,
            days=self.settings.author_cooldown_days,
            kind=action["kind"],
        ):
            raise OutboundError(f"目标仍在 {self.settings.author_cooldown_days} 天冷却期")
        if self.store.is_do_not_contact(action["platform"], author):
            raise OutboundError("目标已拒绝联系")

    def _validate_dm_links(self, text: str) -> None:
        urls = re.findall(r"https?://[^\s<>()]+", text)
        if not urls:
            return
        allowed = self.settings.product_domain_allowlist()
        if not allowed:
            raise OutboundError("私信包含链接，但未配置 KOL_APPROVED_PRODUCT_DOMAINS")
        for url in urls:
            host = (urlparse(url).hostname or "").lower()
            if not any(host == domain or host.endswith(f".{domain}") for domain in allowed):
                raise OutboundError(f"私信链接域名未获批准：{host}")
