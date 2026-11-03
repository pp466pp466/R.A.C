"""一位虛擬會員的行為（load-testing 規格「使用者行為情境」）——不依賴 Locust。

註冊（含年齡確認與條款同意）→ 登入 → 開新對話 → 週期性送訊息，並混入列表與 `since_seq` 增量拉取。
每位虛擬使用者有自己的帳號（保留的測試網域），每個非 GET 請求都帶同源的 `Origin` 與 CSRF 權杖，所以會通過伺服器的 CSRF 檢查。
Locust 與 Flask 測試客戶端各用一個薄的轉接器實作 `Session`，同一組動作兩邊都能跑。
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Protocol

from app.services.age_policy import DEFAULT_AGE_POLICY, age_on, range_for_age, taipei_today
from loadtest.guard import TEST_EMAIL_DOMAIN

PASSWORD = "loadtest-password-1"
AGE = 30
MESSAGES = (
    "請問如何重設密碼？", "我想知道會員等級有什麼差別。", "每天可以送幾則訊息？", "登入失敗幾次會被鎖定？",
    "我想留下一則意見回饋。", "怎麼修改我的顯示名稱？",
)
CSRF_META = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')


@dataclass
class Reply:
    status: int
    body: dict[str, Any] | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


class Session(Protocol):
    origin: str

    def get(self, path: str, *, name: str | None = None) -> Reply: ...

    def post(
        self, path: str, *, json: dict[str, Any], headers: dict[str, str], name: str | None = None,
        fail_if: Callable[[Reply], str | None] | None = None,
    ) -> Reply: ...


FALLBACK_STATUS = "fallback"


def degraded_reply(reply: Reply) -> str | None:
    """降級文案的回覆 HTTP 仍是 200（模型暫時性錯誤、備援也失敗時的設計），但對使用者就是失敗，所以負載測試要把它算進錯誤率。"""
    message = (reply.body or {}).get("assistant_message") or {}
    return "降級回覆（模型暫時性錯誤）" if message.get("msg_status") == FALLBACK_STATUS else None


def birth_date_for(age: int, today: date) -> date:
    return date(today.year - age, 6, 15) if (today.month, today.day) >= (6, 15) else date(today.year - age - 1, 6, 15)


@dataclass
class Member:
    session: Session
    run_id: str
    number: int
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    conversation_id: str | None = None
    last_seq: int = 0
    sent: int = 0
    degraded: int = 0           # 收到降級文案（模型暫時性錯誤且備援也失敗）的次數
    csrf: str = ""

    @property
    def email(self) -> str:
        return f"loadtest-{self.run_id}-{self.number}@{TEST_EMAIL_DOMAIN}"

    # ---- CSRF ----
    def refresh_csrf(self) -> bool:
        # 用永遠公開的頁面：已登入者打首頁會被導向預設頁面，不是這裡要的內容
        page = self.session.get("/terms", name="GET /terms")
        match = CSRF_META.search(page.text)
        self.csrf = match.group(1) if match else ""
        return bool(self.csrf)

    def _post(self, path: str, body: dict[str, Any], *, name: str | None = None, fail_if: Callable[[Reply], str | None] | None = None) -> Reply:
        return self.session.post(path, json=body, headers={"Origin": self.session.origin, "X-CSRF-Token": self.csrf}, name=name, fail_if=fail_if)

    # ---- 行為 ----
    def register_and_login(self) -> bool:
        if not self.refresh_csrf():
            return False
        birth = birth_date_for(AGE, taipei_today(self.now))
        chosen = range_for_age(age_on(birth, taipei_today(self.now)), DEFAULT_AGE_POLICY)
        registered = self._post("/api/auth/register", {
            "email": self.email, "password": PASSWORD, "display_name": f"負載測試{self.number}", "accept_terms": True,
            "birth_date": birth.isoformat(), "age_range": chosen.code,
        }, name="POST /api/auth/register")
        if not registered.ok:
            return False
        logged_in = self._post("/api/auth/login", {"email": self.email, "password": PASSWORD}, name="POST /api/auth/login")
        if not logged_in.ok:
            return False
        return self.refresh_csrf()     # 登入後 cookie 與權杖可能更新，重新取得

    def open_conversation(self) -> bool:
        reply = self._post("/api/cs/conversations", {"client_id": str(uuid.uuid4())}, name="POST /api/cs/conversations")
        if not reply.ok or not reply.body:
            return False
        self.conversation_id = reply.body["conversation"]["id"]
        return True

    def send_message(self) -> Reply:
        text = MESSAGES[self.sent % len(MESSAGES)]
        self.sent += 1
        reply = self._post(f"/api/cs/conversations/{self.conversation_id}/messages", {"client_msg_id": str(uuid.uuid4()), "content": text},
                           name="POST /api/cs/conversations/[id]/messages", fail_if=degraded_reply)
        if reply.ok and reply.body:
            self.last_seq = max(self.last_seq, int(reply.body["assistant_message"]["seq"]))
            self.degraded += int(degraded_reply(reply) is not None)
        return reply

    def list_conversations(self) -> Reply:
        return self.session.get("/api/cs/conversations", name="GET /api/cs/conversations")

    def pull_since(self) -> Reply:
        return self.session.get(f"/api/cs/conversations/{self.conversation_id}/messages?since_seq={max(self.last_seq - 2, 0)}",
                                name="GET /api/cs/conversations/[id]/messages?since_seq")
