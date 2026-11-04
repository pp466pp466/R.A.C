"""privacy-lifecycle 端到端：Google 首次登入、年齡確認、對話、匯出、申請刪除（玩家不收任何信）、受限登入、撤銷、再申請、寬限期滿的排程刪除、期限清除。

整個應用程式以假的模型供應商與假寄信器組裝（沒有網路與金鑰），時鐘手動推進；結束後掃描整段日誌與指令輸出，
證明沒有任何 email、名稱、生日、密碼、確認文字、對話內容或失敗原文。
"""
import io
import json
import zipfile
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import (
    AuthToken, Base, ChatConversation, ChatMessage, DeletionTombstone, LlmCallLog, User, UserIdentity,
)
from app.observability.json_logging import ALLOWED_FIELDS
from tests.support.age import age_fields, birth_date_for_age
from tests.support.auth import FakeClock
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender
from tests.support.google import SUB, FakeProvider, google_sign_in
from tests.support.llm import FakeLLMProvider, Step
from app.services.google_oidc import GoogleProfile

pytestmark = pytest.mark.usefixtures("generous_free_limits")

EMAIL = "flow.person@example.com"
NAME = "流程測試的人"
SECRET_USER_TEXT = "我的私密訊息-SECRET-MSG-4411"
SECRET_REPLY = "回覆的私密字樣-SECRET-REPLY-5522"
SECRET_BAD_OUTPUT = "壞輸出的私密字樣-SECRET-BAD-6633"
CONFIRM = "刪除我的帳號"
BASE = "https://cs.example.com"
BASE_KEYS = {"time", "level", "logger", "message"}


def reply_json(text: str) -> dict:
    return {
        "intent": "account_help", "confidence": 0.9,
        "entities": {"topic": None, "membership_tier": None, "account_issue": "forgot_password", "feedback_type": None},
        "reply_text": text, "suggested_actions": [],
    }


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


@pytest.fixture()
def world():
    clock, sender = FakeClock(), FakeSender()
    provider = FakeLLMProvider(default=Step(lambda request: {"summary": "摘要"} if "累積摘要" in request.system_instruction else reply_json(SECRET_REPLY)))
    google = FakeProvider()
    google.profile = GoogleProfile(SUB, EMAIL, True, NAME)
    app = create_app(
        # 有寄信器與寄信設定：證明即使如此，玩家端也不寄任何信（含申請刪除的通知）
        load_config(environ={"APP_ENV": "testing", "ALERT_EMAIL_TO": "ops@example.com", **AUTH_ENV, "BASE_URL": BASE}),
        mail_sender=sender, clock=clock, llm_provider=provider, identity_provider=google,
    )
    Base.metadata.create_all(app.extensions["db_engine"])
    return app, clock, sender, provider


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def counts(app) -> dict[str, int]:
    with db(app) as s:
        return {
            "users": s.scalar(select(func.count(User.id))), "conversations": s.scalar(select(func.count(ChatConversation.id))),
            "messages": s.scalar(select(func.count(ChatMessage.id))), "ledger": s.scalar(select(func.count(LlmCallLog.id))),
            "tokens": s.scalar(select(func.count(AuthToken.id))), "tombstones": s.scalar(select(func.count(DeletionTombstone.id))),
        }


def test_the_whole_privacy_lifecycle_from_signup_to_erasure_and_expiry_without_leaking_anything(world, log_lines):
    app, clock, sender, provider = world
    runner = app.test_cli_runner()
    cli_output: list[str] = []

    # ---- Google 首次登入（勾選條款）、年齡確認 ----
    client = app.test_client()
    first = google_sign_in(app, client)
    assert first.status_code == 302 and first.headers["Location"] == "/age"
    assert client.post("/api/auth/verify-age", json=age_fields(30)).status_code == 200

    # ---- 對話（走真正的客服流程，含摘要）----
    public_id = client.post("/api/cs/conversations", json={"client_id": uid(999)}).get_json()["conversation"]["id"]
    for n in range(1, 13):
        sent = client.post(f"/api/cs/conversations/{public_id}/messages", json={"client_msg_id": uid(n), "content": SECRET_USER_TEXT})
        assert sent.status_code == 200, sent.get_data(as_text=True)
    before = counts(app)
    assert before["conversations"] == 1 and before["messages"] == 24 and before["ledger"] >= 12

    # ---- 匯出（申請刪除之前）----
    exported = client.post("/api/account/export", json={"format": "json"})
    data = json.loads(exported.get_data(as_text=True))
    assert exported.status_code == 200 and data["account"]["email"] == EMAIL
    assert data["account"]["birth_date"] == birth_date_for_age(30).isoformat()
    assert len(data["conversations"][0]["messages"]) == 24 and SECRET_USER_TEXT in data["conversations"][0]["messages"][0]["content"]
    archive = zipfile.ZipFile(io.BytesIO(client.post("/api/account/export", json={"format": "csv"}).get_data()))
    assert sorted(archive.namelist()) == ["account.csv", "appeals.csv", "messages.csv", "scripts.csv", "sessions.csv", "turns.csv"]
    assert data["appeals"] == []

    # ---- 申請刪除：所有裝置登出；玩家端不寄任何信 ----
    other_device = app.test_client()
    google_sign_in(app, other_device)
    assert other_device.get("/api/auth/me").status_code == 200
    requested = client.post("/api/account/delete", json={"confirm_text": CONFIRM})
    assert requested.status_code == 200 and requested.get_json()["scheduled_at"] == "2026-11-03T12:00:00Z"
    assert client.get("/api/auth/me").status_code == 401 and other_device.get("/api/auth/me").status_code == 401
    assert sender.sent == []
    assert counts(app) == before                                              # 寬限期內資料都還在

    # ---- 寬限期內：重新登入只能撤銷 ----
    clock.advance(days=2)
    returning = app.test_client()
    google_sign_in(app, returning)                                           # 已綁定 Google 的待刪除帳號可以登入（受限登入狀態）
    assert returning.get("/api/auth/me").get_json()["user"]["status"] == "deletion_pending"
    assert returning.get("/api/cs/conversations").status_code == 403
    assert returning.post("/api/account/export", json={"format": "json"}).status_code == 403
    assert returning.get("/cs").headers["Location"] == "/account"
    assert "撤銷刪除" in returning.get("/account").get_data(as_text=True)

    # ---- 排程在寬限期內不會刪 ----
    early = runner.invoke(args=["execute-account-deletions"])
    cli_output.append(early.output)
    assert early.exit_code == 0 and "已刪除 0 個" in early.output and counts(app) == before

    # ---- 撤銷：同一個登入狀態立刻恢復完整功能 ----
    cancelled = returning.post("/api/account/cancel-deletion", json={})
    assert cancelled.status_code == 200 and cancelled.get_json()["user"]["status"] == "active"
    assert returning.get("/api/cs/conversations").status_code == 200

    # ---- 再申請，寬限期滿，排程刪除 ----
    assert returning.post("/api/account/delete", json={"confirm_text": CONFIRM}).status_code == 200
    clock.advance(days=7)
    preview = runner.invoke(args=["execute-account-deletions", "--dry-run"])
    erased = runner.invoke(args=["execute-account-deletions"])
    again = runner.invoke(args=["execute-account-deletions"])
    cli_output += [preview.output, erased.output, again.output]
    assert "1 個" in preview.output and "已刪除 1 個" in erased.output and "已刪除 0 個" in again.output

    after = counts(app)
    assert after["users"] == 0 and after["conversations"] == 0 and after["messages"] == 0 and after["tokens"] == 0
    assert after["ledger"] == before["ledger"] and after["tombstones"] == 1                  # 帳本保留且匿名化
    with db(app) as s:
        assert {(row.user_id, row.conversation_id) for row in s.scalars(select(LlmCallLog))} == {(None, None)}
        tombstone = s.scalars(select(DeletionTombstone)).one()
        assert tombstone.reason == "user_requested"
        assert {c.name for c in DeletionTombstone.__table__.columns} == {"id", "user_id", "deleted_at", "reason"}
    assert sender.sent == []                                                  # 整段流程玩家都沒有收到信

    # ---- 同一個信箱可以重新以 Google 登入並建立新帳號 ----
    fresh = app.test_client()
    again_in = google_sign_in(app, fresh)
    assert again_in.status_code == 302 and again_in.headers["Location"] == "/age"

    # ---- 一年多之後的期限清除 ----
    with db(app) as s:
        user = s.scalars(select(User)).one()
        conv = ChatConversation(user_id=user.id, client_id=uid(500), state_json={}, created_at=clock(), updated_at=clock())
        s.add(conv)
        s.flush()
        s.add(ChatMessage(conversation_id=conv.id, client_msg_id="x", seq=1, role="user", content=SECRET_USER_TEXT))
        s.add(LlmCallLog(
            user_id=user.id, conversation_id=conv.id, kind="chat", provider="gemini", model="m", attempt=1, status="validation_failed",
            counted=False, latency_ms=1, created_at=clock(), error_detail=SECRET_BAD_OUTPUT,
        ))
        s.commit()
    clock.advance(days=400)
    dry = runner.invoke(args=["purge-expired-data", "--dry-run"])
    purged = runner.invoke(args=["purge-expired-data"])
    repeated = runner.invoke(args=["purge-expired-data"])
    cli_output += [dry.output, purged.output, repeated.output]
    assert dry.exit_code == purged.exit_code == repeated.exit_code == 0
    assert "對話與訊息：1 筆" in purged.output and "對話與訊息：0 筆" in repeated.output
    final = counts(app)
    assert final["conversations"] == 0 and final["ledger"] == 0 and final["tombstones"] == 1      # 刪除紀錄不被清除

    # ---- 掃描：日誌與指令輸出 ----
    raw = log_lines.raw() + "".join(cli_output)
    assert raw.strip()
    for secret in (
        EMAIL, NAME, CONFIRM, SECRET_USER_TEXT, SECRET_REPLY, SECRET_BAD_OUTPUT, SUB, public_id,
        birth_date_for_age(30).isoformat(), "25_34",
    ):
        assert secret not in raw, f"日誌或指令輸出洩漏了：{secret[:14]}…"
    lines = log_lines()
    for line in lines:
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"
    events = [line["event"] for line in lines if "event" in line]
    for expected in (
        "account.deletion_requested", "account.deletion_cancelled", "account.deleted_by_request", "account.exported", "retention.purged",
    ):
        assert expected in events, expected
    assert events.count("account.exported") == 2 and events.count("account.deletion_requested") == 2
    assert all("?" not in line.get("path", "") for line in lines)
