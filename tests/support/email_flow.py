"""信箱驗證與重設密碼測試的共用輔助：記錄用的寄信器、延後動作佇列、EmailFlowService 工廠。"""
from __future__ import annotations

import re
from typing import Callable

from sqlalchemy.orm import Session

from app.repositories.auth_token_repository import AuthTokenRepository
from app.repositories.user_repository import UserRepository
from app.services.email_flow_service import EmailFlowService
from tests.support.auth import FAST_HASHER, FakeClock

BASE_URL = "https://cs.example.com"
TOKEN_PATTERN = re.compile(r"#token=([A-Za-z0-9_\-]+)")


class FakeSender:
    """代替 SMTP 寄信器：記錄寄出的信；`fail` 為真時模擬寄信服務失敗。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.fail = False

    def send(self, *, to: str, subject: str, body: str) -> None:
        if self.fail:
            raise ConnectionError("SMTP 伺服器無回應：秘密內容 s3cret-should-never-be-logged")
        self.sent.append({"to": to, "subject": subject, "body": body})

    def tokens(self) -> list[str]:
        return [m.group(1) for mail in self.sent for m in [TOKEN_PATTERN.search(mail["body"])] if m]

    def last_token(self) -> str:
        found = self.tokens()
        assert found, "沒有任何信件含有權杖連結"
        return found[-1]


class DeferQueue:
    """代替「提交成功後才執行」的機制：先登記，測試決定何時執行或丟棄（模擬交易回復）。"""

    def __init__(self) -> None:
        self.actions: list[Callable[[], None]] = []

    def __call__(self, action: Callable[[], None]) -> None:
        self.actions.append(action)

    def run(self) -> None:
        actions, self.actions = self.actions, []
        for action in actions:
            action()

    def discard(self) -> None:
        self.actions = []


def make_flow(
    session: Session,
    *,
    clock: FakeClock | None = None,
    sender: FakeSender | None = None,
    queue: DeferQueue | None = None,
    request_id: str | None = None,
) -> EmailFlowService:
    return EmailFlowService(
        users=UserRepository(session),
        tokens=AuthTokenRepository(session),
        hasher=FAST_HASHER,
        clock=clock or FakeClock(),
        base_url=BASE_URL,
        mail_sender=sender or FakeSender(),
        defer=queue or DeferQueue(),
        request_id=lambda: request_id,
    )
