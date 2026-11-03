"""data-retention 規格：排程指令 `purge-expired-data`（預覽、重複執行、失敗類別的結束代碼、輸出與日誌不含任何內容）。"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import (
    AdminAuditLog, AuthToken, Base, ChatConversation, ChatMessage, DeletionTombstone, LlmCallLog, LockStrike,
)
from app.models.user import TOKEN_VERIFY_EMAIL
from app.repositories.retention_repository import RetentionRepository
from tests.support.auth import START, FakeClock, add_user

SECRET_TEXT = "機密的對話全文與失敗原文 secret-body"
YEAR_AND_A_BIT = timedelta(days=400)


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


def seed(app) -> None:
    """六類各有過期與未過期的資料；過期的內容都含機密文字，用來證明輸出與日誌不含任何內容。"""
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, 1, email="gone.person@example.com", email_canonical="gone.person@example.com").id
        admin = add_user(s, 2, role="admin").id
        old, fresh = START - YEAR_AND_A_BIT, START - timedelta(days=1)
        for n, when in ((1, old), (2, fresh)):
            conv = ChatConversation(user_id=user, client_id=f"{n:036d}", state_json={}, created_at=when, updated_at=when)
            s.add(conv)
            s.flush()
            s.add(ChatMessage(conversation_id=conv.id, client_msg_id=f"m{n}", seq=1, role="user", content=SECRET_TEXT))
            s.add(LlmCallLog(
                user_id=user, conversation_id=conv.id, kind="chat", provider="gemini", model="m", attempt=1, status="validation_failed",
                counted=False, latency_ms=1, created_at=when, error_detail=SECRET_TEXT,
            ))
        for n, expires in ((1, START - timedelta(days=10)), (2, START + timedelta(hours=1))):
            s.add(AuthToken(user_id=user, purpose=TOKEN_VERIFY_EMAIL, token_hash=f"{n:064d}", expires_at=expires))
        for created in (START - timedelta(days=800), START - timedelta(days=10)):
            s.add(AdminAuditLog(admin_user_id=admin, action="user.lookup", detail_json={}, created_at=created))
        for created in (START - timedelta(days=10), START - timedelta(days=1)):
            s.add(LockStrike(user_id=user, kind="rate_limited", created_at=created))
        s.add(DeletionTombstone(user_id=99, reason="user_requested", deleted_at=START - timedelta(days=900)))
        s.commit()


def totals(app) -> dict[str, int]:
    with Session(app.extensions["db_engine"]) as s:
        return {
            "conversations": s.scalar(select(func.count(ChatConversation.id))),
            "messages": s.scalar(select(func.count(ChatMessage.id))),
            "ledger": s.scalar(select(func.count(LlmCallLog.id))),
            "error_detail": s.scalar(select(func.count(LlmCallLog.id)).where(LlmCallLog.error_detail.is_not(None))),
            "tokens": s.scalar(select(func.count(AuthToken.id))),
            "audit": s.scalar(select(func.count(AdminAuditLog.id))),
            "strikes": s.scalar(select(func.count(LockStrike.id))),
            "tombstones": s.scalar(select(func.count(DeletionTombstone.id))),
        }


def test_the_dry_run_lists_the_counts_per_category_and_changes_nothing(app, runner, log_lines):
    seed(app)
    before = totals(app)
    result = runner.invoke(args=["purge-expired-data", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "預覽" in result.output and "未改變任何資料" in result.output
    for line in ("對話與訊息：1 筆", "用量帳本：1 筆", "失敗的模型原文：1 筆", "驗證權杖：1 筆", "管理稽核紀錄：1 筆", "帳號鎖定計次：1 筆"):
        assert line in result.output, line
    assert totals(app) == before
    assert [line for line in log_lines() if line.get("event", "").startswith("retention.")] == []


def test_the_purge_removes_exactly_the_expired_data_and_keeps_the_rest(app, runner):
    seed(app)
    result = runner.invoke(args=["purge-expired-data"])
    assert result.exit_code == 0, result.output
    assert "期限清除結果" in result.output and "對話與訊息：1 筆" in result.output
    assert totals(app) == {
        "conversations": 1, "messages": 1, "ledger": 1, "error_detail": 1, "tokens": 1, "audit": 1, "strikes": 1, "tombstones": 1,
    }


def test_running_it_twice_processes_nothing_the_second_time(app, runner):
    seed(app)
    runner.invoke(args=["purge-expired-data"])
    second = runner.invoke(args=["purge-expired-data"])
    assert second.exit_code == 0
    for label in ("對話與訊息", "用量帳本", "失敗的模型原文", "驗證權杖", "管理稽核紀錄", "帳號鎖定計次"):
        assert f"{label}：0 筆" in second.output


def test_the_output_and_the_logs_carry_only_counts(app, runner, log_lines):
    seed(app)
    dry = runner.invoke(args=["purge-expired-data", "--dry-run"])
    real = runner.invoke(args=["purge-expired-data"])
    everything = dry.output + real.output + log_lines.raw()
    for secret in ("secret-body", "機密的對話全文", "gone.person@example.com", "使用者1"):
        assert secret not in everything, secret
    purged = [(line["reason"], line["count"]) for line in log_lines() if line.get("event") == "retention.purged"]
    assert purged == [("conversations", 1), ("error_detail", 1), ("ledger", 1), ("auth_tokens", 1), ("admin_audit", 1), ("lock_strikes", 1)]


def test_a_failing_category_is_reported_by_name_with_a_nonzero_exit_code_and_the_others_still_run(app, runner, monkeypatch):
    seed(app)

    def broken(self, ids):
        raise RuntimeError("資料庫暫時失敗 secret-body")

    monkeypatch.setattr(RetentionRepository, "purge_ledger", broken)
    result = runner.invoke(args=["purge-expired-data"])

    assert result.exit_code != 0
    assert "用量帳本：失敗" in result.output and "以下類別清除失敗：用量帳本" in result.output
    assert "secret-body" not in result.output
    after = totals(app)
    assert after["tokens"] == 1 and after["audit"] == 1 and after["conversations"] == 1        # 其餘類別仍完成
