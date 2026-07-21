from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class OutboundError(RuntimeError):
    pass


class OutboundConfirmationRequired(OutboundError):
    pass


class TargetNotMessageable(OutboundError):
    pass


_X_DM_POSTED_QUERIES = (
    ("get", "text", "[data-testid='messageEntry']:last-of-type"),
    ("get", "text", "[data-testid='messageEntry']"),
)
_X_DM_COMPOSER_QUERIES = (
    ("get", "text", "[data-testid='dmComposerTextInput']"),
    ("get", "value", "[data-testid='dmComposerTextInput']"),
)
_XHS_COMMENT_POSTED_QUERIES = (
    ("get", "text", "[class*='comment-item']:last-of-type"),
    ("get", "text", "[class*='commentItem']:last-of-type"),
)
_XHS_COMMENT_COMPOSER_QUERIES = (
    ("get", "value", "textarea"),
    ("get", "text", "textarea"),
)
_XHS_DM_POSTED_QUERIES = (
    ("get", "text", "[class*='message-item']:last-of-type"),
    ("get", "text", "[class*='messageItem']:last-of-type"),
)
_XHS_DM_COMPOSER_QUERIES = (
    ("get", "value", "textarea"),
    ("get", "text", "[contenteditable='true']"),
)


@dataclass
class OutboundReceipt:
    status: str
    url: str | None = None
    external_id: str | None = None
    raw: dict[str, Any] | None = None


class _OpenCliWriter:
    """Shared OpenCLI mechanics; subclasses own all platform selectors and commands."""

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
            raise OutboundConfirmationRequired(
                "浏览器操作超时；请人工确认平台是否已写入"
            ) from exc
        except OSError as exc:
            raise OutboundError(f"OpenCLI 启动失败：{exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()[-1200:]
            lowered = detail.lower()
            if any(token in lowered for token in ("captcha", "verify", "风控", "验证")):
                raise OutboundError(f"平台要求验证，账号必须暂停：{detail}")
            if any(
                token in lowered
                for token in ("not messageable", "cannot message", "无法私信")
            ):
                raise TargetNotMessageable(detail)
            raise OutboundError(detail)
        if not expect_json:
            return result.stdout
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return {"stdout": result.stdout.strip()}

    def _whoami(self, action: dict[str, Any], *, site: str) -> None:
        payload = self._run(action["browser_profile"], site, "whoami", "-f", "json")
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
            except OutboundConfirmationRequired:
                # A timed-out click may already have taken effect. Even for
                # navigation controls, do not issue another click blindly.
                raise
            except OutboundError as exc:
                errors.append(str(exc))
        message = "找不到目标操作按钮：" + " | ".join(errors[-2:])
        if not_messageable:
            raise TargetNotMessageable(message)
        raise OutboundError(message)

    def _click_write_once(
        self,
        profile: str,
        session: str,
        selector: tuple[str, ...],
    ) -> None:
        """Issue exactly one irreversible click and never try another selector."""

        try:
            self._run(profile, "browser", session, "click", *selector)
        except OutboundConfirmationRequired:
            raise
        except OutboundError as exc:
            # Browser bridges may report an error after the page accepted the
            # click. Treat every send-click failure as uncertain, not retryable.
            raise OutboundConfirmationRequired(
                f"发送按钮未返回明确结果；请人工确认平台是否已写入：{exc}"
            ) from exc

    def _read_first(
        self,
        profile: str,
        session: str,
        queries: tuple[tuple[str, ...], ...],
    ) -> str:
        errors: list[str] = []
        for query in queries:
            try:
                payload = self._run(profile, "browser", session, *query)
            except OutboundError as exc:
                errors.append(str(exc))
                continue
            if isinstance(payload, dict):
                return str(payload.get("value") or payload.get("text") or "")
            return str(payload or "")
        raise OutboundError("找不到可验证的回执元素：" + " | ".join(errors[-2:]))

    def _verify_posted_text(
        self,
        action: dict[str, Any],
        receipt: OutboundReceipt,
        *,
        posted_queries: tuple[tuple[str, ...], ...],
        composer_queries: tuple[tuple[str, ...], ...],
    ) -> bool:
        session = (receipt.raw or {}).get("browser_session")
        expected = str(action.get("final_text") or "").strip()
        if not session or not expected:
            return False
        posted_before = (receipt.raw or {}).get("posted_text_before")
        if not isinstance(posted_before, str):
            # Without a read-before-write baseline, an older identical message
            # cannot be distinguished from this action's receipt.
            return False
        posted_text = self._read_first(
            action["browser_profile"], session, posted_queries
        )
        composer_text = self._read_first(
            action["browser_profile"], session, composer_queries
        )
        # Require a new occurrence relative to the read-before-write snapshot,
        # as well as a cleared composer. False negatives remain unconfirmed and
        # are safe to inspect manually; an old same-text node is never accepted.
        return (
            expected not in composer_text
            and posted_text != posted_before
            and posted_text.count(expected) > posted_before.count(expected)
        )

    def _snapshot_posted_text(
        self,
        profile: str,
        session: str,
        posted_queries: tuple[tuple[str, ...], ...],
    ) -> str | None:
        """Capture a read-only baseline; failure keeps the write unconfirmed."""

        try:
            return self._read_first(profile, session, posted_queries)
        except OutboundError:
            return None


class XOpenCliWriter(_OpenCliWriter):
    """X-owned browser commands and receipt verification."""

    def reply(self, action: dict[str, Any]) -> OutboundReceipt:
        self._whoami(action, site="twitter")
        if not action.get("target_url"):
            raise OutboundError("评论任务缺少目标链接")
        payload = self._run(
            action["browser_profile"],
            "twitter",
            "reply",
            action["target_url"],
            str(action.get("final_text") or ""),
            "--window",
            "foreground",
            "-f",
            "json",
        )
        row = payload[0] if isinstance(payload, list) and payload else payload
        url = row.get("url") if isinstance(row, dict) else None
        external_id = None
        if url:
            match = re.search(r"/status/([^/?#]+)", str(url))
            external_id = match.group(1) if match else None
        confirmation_row: dict[str, Any] | None = None
        if not external_id or not url:
            target_id = str(action.get("target_external_id") or "").strip()
            expected_text = str(action.get("final_text") or "").strip()
            managed_username = str(action.get("managed_username") or "").lstrip("@").lower()
            if target_id and expected_text and managed_username:
                try:
                    thread_payload = self._run(
                        action["browser_profile"],
                        "twitter",
                        "thread",
                        target_id,
                        "-f",
                        "json",
                    )
                except OutboundError:
                    # The reply call has already returned success. A failed
                    # read-back is uncertain and must never trigger a write retry.
                    thread_payload = []
                thread_rows = thread_payload if isinstance(thread_payload, list) else []
                matches = [
                    item
                    for item in thread_rows
                    if isinstance(item, dict)
                    and str(item.get("author") or item.get("username") or "")
                    .lstrip("@")
                    .lower()
                    == managed_username
                    and str(item.get("in_reply_to") or "") == target_id
                    and expected_text in str(item.get("text") or "")
                    and str(item.get("id") or "") != target_id
                    and re.search(r"/status/[^/?#]+", str(item.get("url") or ""))
                ]
                if len(matches) == 1:
                    confirmation_row = matches[0]
                    url = str(confirmation_row["url"])
                    external_id = str(confirmation_row["id"])
        return OutboundReceipt(
            status="sent",
            url=url,
            external_id=external_id,
            raw={"opencli": payload, "thread_confirmation": confirmation_row},
        )

    def send_dm(self, action: dict[str, Any]) -> OutboundReceipt:
        self._whoami(action, site="twitter")
        target_url = action.get("target_url")
        if not target_url:
            raise OutboundError("私信任务缺少目标用户主页或会话链接")
        session = f"kol-x-dm-{action['id']}"
        self._run(action["browser_profile"], "browser", session, "open", target_url)
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
        posted_before = self._snapshot_posted_text(
            action["browser_profile"], session, _X_DM_POSTED_QUERIES
        )
        self._fill_first(
            action["browser_profile"],
            session,
            str(action.get("final_text") or ""),
            (
                ("--role", "textbox", "--name", "Message"),
                ("[data-testid='dmComposerTextInput']",),
            ),
        )
        self._click_write_once(
            action["browser_profile"],
            session,
            ("[data-testid='dmComposerSendButton']",),
        )
        return _session_receipt(
            session, target_url, posted_text_before=posted_before
        )

    def verify_receipt(self, action: dict[str, Any], receipt: OutboundReceipt) -> bool:
        if action["kind"] == "comment":
            target_id = str(action.get("target_external_id") or "")
            return bool(
                receipt.external_id
                and receipt.url
                and re.search(r"/status/[^/?#]+", receipt.url)
                and receipt.external_id != target_id
            )
        return self._verify_posted_text(
            action,
            receipt,
            posted_queries=_X_DM_POSTED_QUERIES,
            composer_queries=_X_DM_COMPOSER_QUERIES,
        )


class XiaohongshuOpenCliWriter(_OpenCliWriter):
    """Xiaohongshu-owned DOM commands and receipt verification."""

    def reply(self, action: dict[str, Any]) -> OutboundReceipt:
        self._whoami(action, site="xiaohongshu")
        target_url = action.get("target_url")
        if not target_url:
            raise OutboundError("评论任务缺少目标链接")
        session = f"kol-xhs-comment-{action['id']}"
        self._run(action["browser_profile"], "browser", session, "open", target_url)
        posted_before = self._snapshot_posted_text(
            action["browser_profile"], session, _XHS_COMMENT_POSTED_QUERIES
        )
        self._fill_first(
            action["browser_profile"],
            session,
            str(action.get("final_text") or ""),
            (("--role", "textbox", "--name", "说点什么"), ("textarea",)),
        )
        self._click_write_once(
            action["browser_profile"],
            session,
            ("--role", "button", "--name", "发送"),
        )
        return _session_receipt(
            session, target_url, posted_text_before=posted_before
        )

    def send_dm(self, action: dict[str, Any]) -> OutboundReceipt:
        self._whoami(action, site="xiaohongshu")
        target_url = action.get("target_url")
        if not target_url:
            raise OutboundError("私信任务缺少目标用户主页或会话链接")
        session = f"kol-xiaohongshu-dm-{action['id']}"
        self._run(action["browser_profile"], "browser", session, "open", target_url)
        self._click_first(
            action["browser_profile"],
            session,
            (("--role", "button", "--name", "私信"), ("--text", "私信")),
            not_messageable=True,
        )
        posted_before = self._snapshot_posted_text(
            action["browser_profile"], session, _XHS_DM_POSTED_QUERIES
        )
        self._fill_first(
            action["browser_profile"],
            session,
            str(action.get("final_text") or ""),
            (
                ("--role", "textbox", "--name", "发送消息"),
                ("textarea",),
                ("[contenteditable='true']",),
            ),
        )
        self._click_write_once(
            action["browser_profile"],
            session,
            ("--role", "button", "--name", "发送"),
        )
        return _session_receipt(
            session, target_url, posted_text_before=posted_before
        )

    def verify_receipt(self, action: dict[str, Any], receipt: OutboundReceipt) -> bool:
        if action["kind"] == "comment":
            posted_queries = _XHS_COMMENT_POSTED_QUERIES
            composer_queries = _XHS_COMMENT_COMPOSER_QUERIES
        else:
            posted_queries = _XHS_DM_POSTED_QUERIES
            composer_queries = _XHS_DM_COMPOSER_QUERIES
        return self._verify_posted_text(
            action,
            receipt,
            posted_queries=posted_queries,
            composer_queries=composer_queries,
        )


def _session_receipt(
    session: str,
    target_url: str,
    *,
    posted_text_before: str | None,
) -> OutboundReceipt:
    raw: dict[str, Any] = {"browser_session": session}
    if posted_text_before is not None:
        raw["posted_text_before"] = posted_text_before
    return OutboundReceipt(
        status="sent",
        url=target_url,
        # A local digest is not a platform receipt. Leave the external ID empty
        # unless a platform command returns a native identifier explicitly.
        external_id=None,
        raw=raw,
    )


__all__ = [
    "OutboundConfirmationRequired",
    "OutboundError",
    "OutboundReceipt",
    "TargetNotMessageable",
    "XOpenCliWriter",
    "XiaohongshuOpenCliWriter",
]
