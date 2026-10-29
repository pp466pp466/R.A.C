"""用量限制在並行下的行為，以及每次檢查的查詢數上限（design D5、Risks）。

檢查與提交之間有時間差：並行的請求可能一起通過，超額幅度不超過並行數；正在處理的其他對話已視為預留名額，
所以最常見的情形（先到的請求還在等模型）不會讓後到的請求也通過。這裡用真實的執行緒與獨立連線證明這兩件事，
而不是靠循序呼叫推論。
"""
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.models import ChatMessage, LlmCallLog
from app.repositories.chat_repository import ChatRepository
from app.services.errors import DailyQuotaExceeded
from tests.support.auth import add_user
from tests.test_chat_service import current, make_service, provider_with, uid
from tests.test_llm_service import Wall
from tests.test_usage_service import Env, send as check_send


def seed_counted(engine, user_id, n, at):
    with Session(engine) as session:
        for _ in range(n):
            session.add(LlmCallLog(
                user_id=user_id, conversation_id=None, kind="chat", provider="fake", model="m", attempt=1, status="ok",
                counted=True, latency_ms=10, created_at=at,
            ))
        session.commit()


def counted_rows(engine):
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(LlmCallLog).where(LlmCallLog.counted == True))   # noqa: E712


def test_a_message_being_processed_holds_its_slot_so_the_next_conversation_is_denied(concurrent_engine):
    """先到的請求在等模型時，處理標記已提交；後到的請求看得到它，扣掉名額後就沒有剩餘。"""
    wall = Wall()
    outcome = {}
    with Session(concurrent_engine) as setup:
        alice = current(setup, 1)
        first = make_service(setup, provider_with(), wall=wall).create_conversation(alice, uid(100))[0].id
        second = make_service(setup, provider_with(), wall=wall).create_conversation(alice, uid(101))[0].id
        setup.commit()
    seed_counted(concurrent_engine, alice.id, 29, wall())

    provider = provider_with()

    def while_the_first_is_with_the_model(request):
        with Session(concurrent_engine) as other:
            try:
                make_service(other, provider_with(), wall=wall).send_message(alice, second, uid(2), "第二個對話")
            except DailyQuotaExceeded as error:
                outcome["second"] = error

    provider.hooks.append(while_the_first_is_with_the_model)
    with Session(concurrent_engine) as session:
        result = make_service(session, provider, wall=wall).send_message(alice, first, uid(1), "第一個對話")

    assert result.assistant_message.msg_status == "ok"
    assert isinstance(outcome.get("second"), DailyQuotaExceeded)
    with Session(concurrent_engine) as check:
        stored = list(check.scalars(select(ChatMessage.content).where(ChatMessage.role == "user")))
    assert stored == ["第一個對話"]                                   # 被拒絕的那一則沒有留下
    assert counted_rows(concurrent_engine) == 30


@pytest.mark.parametrize("attempt", range(3))
def test_a_burst_over_the_daily_limit_overshoots_by_less_than_the_number_of_concurrent_requests(concurrent_engine, attempt):
    wall = Wall()
    workers = 6
    with Session(concurrent_engine) as setup:
        alice = current(setup, 1)
        conversations = [
            make_service(setup, provider_with(), wall=wall).create_conversation(alice, uid(100 + n))[0].id for n in range(workers)
        ]
        setup.commit()
    seed_counted(concurrent_engine, alice.id, 29, wall())

    gate = threading.Barrier(workers)
    results, lock = [], threading.Lock()

    def slow_model(request):
        time.sleep(0.05)                                                # 讓並行的請求有機會彼此重疊

    def worker(index):
        provider = provider_with()
        provider.hooks.append(slow_model)
        gate.wait(10)
        with Session(concurrent_engine) as session:
            try:
                make_service(session, provider, wall=wall).send_message(alice, conversations[index], uid(1), f"第 {index} 個")
                answer = "accepted"
            except DailyQuotaExceeded:
                answer = "denied"
            except Exception as error:                                   # noqa: BLE001 - 任何別的錯誤都是測試失敗
                answer = f"error:{type(error).__name__}"
        with lock:
            results.append(answer)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert len(results) == workers and not [r for r in results if r.startswith("error")], results
    accepted = results.count("accepted")
    assert 1 <= accepted <= workers                                      # 至少有一個通過；沒有人能讓總數超過 29 + 並行數
    assert counted_rows(concurrent_engine) == 29 + accepted
    assert accepted + results.count("denied") == workers


# ---- 每次檢查的查詢數 ------------------------------------------------------------------------

class StatementCounter:
    def __init__(self, engine):
        self.engine, self.statements = engine, []

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._record)


@pytest.fixture()
def counted_env(member_session):
    env = Env(member_session)
    user = env.user()
    conversation = SimpleNamespace(id=env.conversation(user).id)          # 只留編號：重新載入 ORM 物件也是一筆查詢，會混進計數
    member_session.commit()
    member_session.expire_all()                                           # 之後的查詢才是我們要數的
    return env, user, conversation


def test_checking_a_send_issues_at_most_five_statements(counted_env, member_session):
    env, user, conversation = counted_env
    with StatementCounter(member_session.get_bind()) as counter:
        check_send(env, user, conversation)
    assert len(counter.statements) <= 5, counter.statements


def test_checking_an_already_accepted_message_issues_at_most_four(counted_env, member_session):
    env, user, conversation = counted_env
    with StatementCounter(member_session.get_bind()) as counter:
        check_send(env, user, conversation, already_accepted=True)
    assert len(counter.statements) <= 4, counter.statements


def test_the_usage_snapshot_issues_at_most_three_statements(counted_env, member_session):
    env, user, _ = counted_env
    with StatementCounter(member_session.get_bind()) as counter:
        env.service.snapshot(user)
    assert len(counter.statements) <= 3, counter.statements


def test_checking_a_new_conversation_issues_one_statement(counted_env, member_session):
    env, user, _ = counted_env
    with StatementCounter(member_session.get_bind()) as counter:
        env.service.check_create_conversation(user)
    assert len(counter.statements) == 1, counter.statements


def test_the_background_budget_check_issues_one_statement(counted_env, member_session):
    env, _, _ = counted_env
    with StatementCounter(member_session.get_bind()) as counter:
        env.service.global_budget_exhausted()
    assert len(counter.statements) == 1, counter.statements
