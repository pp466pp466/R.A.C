"""寄信（SMTP）。見 AGENTS.md 第 8、9、10 節與 email-token-flows 規格。

- 信件一律是 UTF-8 的純文字（標題以 RFC 2047 編碼、內文以 base64 傳輸編碼），繁體中文不會亂碼。
- 連線方式：STARTTLS（預設）、SSL，或不加密（僅限 development 與 testing，由設定驗證把關）。
- 單次寄送的等待上限 5 秒。失敗時**拋出例外**，由呼叫端決定如何處理（註冊與忘記密碼不因寄信失敗而改變回應）。
- 收件人與標題含換行（CR、LF）時拒絕寄送，避免郵件標頭注入。
- 寄信密碼只在登入時使用，不會出現在任何例外訊息或日誌中。
"""
from __future__ import annotations

import smtplib
import ssl
from email import policy
from email.message import EmailMessage
from typing import Protocol

from app.config import MailSettings

SMTP_TIMEOUT_SECONDS = 5.0


class MailSender(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> None: ...


class MailHeaderError(ValueError):
    """收件人或標題含有換行等控制字元。"""


def _check_header(name: str, value: str) -> str:
    if not value or any(ch in value for ch in "\r\n\x00"):
        raise MailHeaderError(f"信件的{name}不合法")
    return value


class SmtpMailSender:
    def __init__(self, settings: MailSettings, *, timeout: float = SMTP_TIMEOUT_SECONDS) -> None:
        self._settings = settings
        self._timeout = timeout

    def build_message(self, *, to: str, subject: str, body: str) -> EmailMessage:
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = _check_header("寄件者", self._settings.from_address)
        message["To"] = _check_header("收件人", to)
        message["Subject"] = _check_header("標題", subject)
        message.set_content(body, subtype="plain", charset="utf-8", cte="base64")
        return message

    def send(self, *, to: str, subject: str, body: str) -> None:
        message = self.build_message(to=to, subject=subject, body=body)    # 先檢查，不合法就不連線
        settings = self._settings
        if settings.security == "ssl":
            connection: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.host, settings.port, timeout=self._timeout, context=ssl.create_default_context()
            )
        else:
            connection = smtplib.SMTP(settings.host, settings.port, timeout=self._timeout)
        with connection:
            connection.ehlo()
            if settings.security == "starttls":
                connection.starttls(context=ssl.create_default_context())
                connection.ehlo()
            if settings.username and settings.password is not None:
                connection.login(settings.username, settings.password.reveal())
            connection.send_message(message)
