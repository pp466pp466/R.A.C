"""cs-conversations 與 cs-conversation-memory 規格：對話資料存取的隔離、原子性與刪除。

一般測試以 `member_session` 同時在記憶體 SQLite 與 SQL Server（標記 sqlserver）上執行；並行測試另用
`concurrent_engine`（檔案型 SQLite 與 SQL Server），以真實的執行緒與獨立連線證明「並發只有一個成功」，
而不是靠循序呼叫推論。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChatConversation, ChatMessage, LlmCallLog
from app.models.chat import DEFAULT_CONVERSATION_TITLE, MSG_STATUS_FALLBACK
from app.repositories.chat_repository import ChatRepository
from app.repositories.errors import DuplicateConversationError, DuplicateMessageError
from tests.support.auth import add_user

T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
STATE = {"member_tier": "free", "last_intent": None, "pending_entities": {}, "open_issue": None}
MSG_A, MSG_B, MSG_C = ("a" * 36, "b" * 36, "c" * 36)


def uuid_like(n: int) -> str:
    return f"{n:036d}"


@pytest.fixture()
def repo(member_session):
    return ChatRepository(member_session)


@pytest.fixture()
def alice(member_session):
    return add_user(member_session, 1).id


@pytest.fixture()
def bob(member_session):
    return add_user(member_session, 2).id


def new_conversation(repo, user_id, n=1, *, now=T0):
    return repo.create_conversation(user_id, client_id=uuid_like(n), state=STATE, now=now)


def say(repo, user_id, conversation, client_msg_id, *, role="user", content="你好", now=T0, **extra):
    return repo.append_message(user_id, conversation, client_msg_id=client_msg_id, role=role, content=content, now=now, **extra)


# ---- 建立與讀取：擁有者條件 ------------------------------------------------------------

def test_a_new_conversation_starts_with_the_documented_defaults(repo, alice):
    conversation = new_conversation(repo, alice)
    assert conversation.id and conversation.public_id and len(conversation.public_id) == 36
    assert (conversation.title, conversation.summary, conversation.summary_upto_seq) == (DEFAULT_CONVERSATION_TITLE, None, 0)
    assert (conversation.state_version, conversation.processing_since) == (1, None)
    assert conversation.state_json == STATE


def test_the_same_client_id_cannot_create_two_conversations_but_another_user_may_use_it(repo, member_session, alice, bob):
    first = new_conversation(repo, alice)
    member_session.commit()
    with pytest.raises(DuplicateConversationError):
        new_conversation(repo, alice)
    member_session.rollback()
    assert repo.find_conversation_by_client_id(alice, uuid_like(1)).public_id == first.public_id
    assert new_conversation(repo, bob).user_id == bob                       # 別人用同一個識別碼是另一個對話


def test_a_conversation_is_only_visible_to_its_owner(repo, alice, bob):
    mine = new_conversation(repo, alice)
    assert repo.get_conversation(alice, mine.public_id).id == mine.id
    assert repo.get_conversation(bob, mine.public_id) is None                # 別人的
    assert repo.get_conversation(alice, "00000000-0000-4000-8000-000000000000") is None   # 不存在的
    assert repo.find_conversation_by_client_id(bob, uuid_like(1)) is None


def test_conversations_are_listed_newest_activity_first_and_only_the_users_own(repo, alice, bob):
    old = new_conversation(repo, alice, 1, now=T0)
    new = new_conversation(repo, alice, 2, now=T0 + timedelta(minutes=5))
    new_conversation(repo, bob, 3, now=T0 + timedelta(minutes=9))
    assert [c.id for c in repo.list_conversations(alice, limit=10, offset=0)] == [new.id, old.id]
    say(repo, alice, old, MSG_A, now=T0 + timedelta(minutes=20))             # 有新活動的對話排到最前
    assert [c.id for c in repo.list_conversations(alice, limit=10, offset=0)] == [old.id, new.id]


def test_listing_pages_are_continuous_and_do_not_repeat(repo, alice):
    ids = [new_conversation(repo, alice, n, now=T0 + timedelta(minutes=n)).id for n in range(1, 8)]
    pages = [repo.list_conversations(alice, limit=3, offset=offset) for offset in (0, 3, 6, 9)]
    assert [len(p) for p in pages] == [3, 3, 1, 0]
    assert [c.id for page in pages for c in page] == list(reversed(ids))


# ---- 改名與版本 ---------------------------------------------------------------------

def test_renaming_needs_the_current_version_and_bumps_it(repo, alice, bob):
    conversation = new_conversation(repo, alice)
    assert repo.rename_conversation(alice, conversation, title="帳號問題", expected_version=1, now=T0) is True
    assert (conversation.title, conversation.state_version) == ("帳號問題", 2)
    assert repo.rename_conversation(alice, conversation, title="過期的改名", expected_version=1, now=T0) is False
    assert (conversation.title, conversation.state_version) == ("帳號問題", 2)       # 沒被覆寫
    assert repo.rename_conversation(bob, conversation, title="別人改", expected_version=2, now=T0) is False
    assert conversation.title == "帳號問題"


def test_the_default_title_is_replaced_only_once_and_never_after_a_rename(repo, alice):
    first = new_conversation(repo, alice, 1)
    assert repo.set_title_if_default(alice, first, title="我想問密碼", now=T0) is True
    assert repo.set_title_if_default(alice, first, title="第二次", now=T0) is False
    assert first.title == "我想問密碼"
    renamed = new_conversation(repo, alice, 2)
    repo.rename_conversation(alice, renamed, title="自己取的", expected_version=1, now=T0)
    assert repo.set_title_if_default(alice, renamed, title="不該覆寫", now=T0) is False
    assert renamed.title == "自己取的"


# ---- 處理標記 --------------------------------------------------------------------------

def test_the_processing_marker_allows_one_holder_until_it_goes_stale(repo, alice, bob):
    conversation = new_conversation(repo, alice)
    stale = lambda now: now - timedelta(seconds=60)
    assert repo.acquire_processing(alice, conversation, now=T0, stale_before=stale(T0)) is True
    assert repo.acquire_processing(alice, conversation, now=T0 + timedelta(seconds=30), stale_before=stale(T0 + timedelta(seconds=30))) is False
    later = T0 + timedelta(seconds=61)                                          # 標記是 61 秒前設定的：可接手
    assert repo.acquire_processing(alice, conversation, now=later, stale_before=stale(later)) is True
    assert repo.acquire_processing(bob, conversation, now=later, stale_before=stale(later)) is False   # 別人的對話永遠拿不到


def test_the_marker_is_exactly_stale_after_60_seconds_not_before(repo, alice):
    conversation = new_conversation(repo, alice)
    repo.acquire_processing(alice, conversation, now=T0, stale_before=T0 - timedelta(seconds=60))
    at = T0 + timedelta(seconds=60)                                             # 剛好 60 秒：標記不早於門檻，還不算過期
    assert repo.acquire_processing(alice, conversation, now=at, stale_before=at - timedelta(seconds=60)) is False
    at = T0 + timedelta(seconds=60, microseconds=1)
    assert repo.acquire_processing(alice, conversation, now=at, stale_before=at - timedelta(seconds=60)) is True


def test_releasing_clears_only_the_marker_that_this_holder_set(repo, alice):
    conversation = new_conversation(repo, alice)
    repo.acquire_processing(alice, conversation, now=T0, stale_before=T0 - timedelta(seconds=60))
    takeover = T0 + timedelta(seconds=61)
    repo.acquire_processing(alice, conversation, now=takeover, stale_before=takeover - timedelta(seconds=60))
    assert repo.release_processing(alice, conversation, held_since=T0) is False          # 原持有者晚到：不清除接手者的標記
    assert conversation.processing_since == takeover
    assert repo.release_processing(alice, conversation, held_since=takeover) is True
    assert conversation.processing_since is None
    assert repo.acquire_processing(alice, conversation, now=takeover, stale_before=takeover - timedelta(seconds=60)) is True


# ---- 狀態與摘要的條件式寫入 -------------------------------------------------------------------

def test_state_is_written_only_against_the_expected_version(repo, alice):
    conversation = new_conversation(repo, alice)
    state = {**STATE, "last_intent": "account_help"}
    assert repo.write_state(alice, conversation, state=state, expected_version=1, now=T0) is True
    assert (conversation.state_json, conversation.state_version) == (state, 2)
    assert repo.write_state(alice, conversation, state=STATE, expected_version=1, now=T0) is False
    assert conversation.state_json == state and conversation.state_version == 2


def test_the_summary_marker_only_moves_forward_and_only_from_the_expected_value(repo, alice):
    conversation = new_conversation(repo, alice)
    assert repo.write_summary(alice, conversation, summary="第一輪", expected_upto_seq=0, new_upto_seq=2) is True
    assert (conversation.summary, conversation.summary_upto_seq) == ("第一輪", 2)
    assert repo.write_summary(alice, conversation, summary="晚到的", expected_upto_seq=0, new_upto_seq=2) is False   # 條件不符
    assert repo.write_summary(alice, conversation, summary="倒退", expected_upto_seq=2, new_upto_seq=1) is False     # 不會倒退
    assert repo.write_summary(alice, conversation, summary="原地踏步", expected_upto_seq=2, new_upto_seq=2) is False
    assert (conversation.summary, conversation.summary_upto_seq) == ("第一輪", 2)


# ---- 訊息 --------------------------------------------------------------------------------

def test_messages_get_increasing_sequence_numbers_and_move_the_conversation_up(repo, alice):
    conversation = new_conversation(repo, alice)
    first = say(repo, alice, conversation, MSG_A, now=T0 + timedelta(seconds=1))
    second = say(repo, alice, conversation, MSG_B, role="assistant", content="您好", now=T0 + timedelta(seconds=2),
                 intent="greeting", entities={"topic": None}, suggested_actions=[{"type": "quick_reply", "label": "x", "text": "y"}],
                 confidence=0.9)
    assert (first.seq, second.seq) == (1, 2)
    assert (second.intent, second.confidence, second.msg_status) == ("greeting", 0.9, "ok")
    assert second.entities_json == {"topic": None} and second.suggested_actions_json[0]["label"] == "x"
    assert conversation.updated_at == T0 + timedelta(seconds=2)


def test_chinese_text_and_json_round_trip_without_loss(repo, member_session, alice):
    conversation = new_conversation(repo, alice)
    text = "忘記密碼怎麼辦？😀 第二行\n第三行"
    say(repo, alice, conversation, MSG_A, content=text, entities={"topic": "密碼"})
    member_session.commit()
    member_session.expire_all()
    stored = repo.latest_messages(alice, repo.get_conversation(alice, conversation.public_id), limit=5)[0]
    assert stored.content == text and stored.entities_json == {"topic": "密碼"}


def test_a_duplicate_client_msg_id_or_sequence_is_refused_by_the_database(repo, member_session, alice):
    conversation = new_conversation(repo, alice)
    say(repo, alice, conversation, MSG_A)
    member_session.commit()
    with pytest.raises(DuplicateMessageError):
        say(repo, alice, conversation, MSG_A, role="assistant", content="同識別碼")
    member_session.rollback()
    assert member_session.scalar(select(func.count()).select_from(ChatMessage)) == 1


def test_appending_to_someone_elses_conversation_is_refused(repo, alice, bob):
    conversation = new_conversation(repo, alice)
    with pytest.raises(LookupError):
        say(repo, bob, conversation, MSG_A)


def test_messages_after_a_sequence_page_in_order_and_report_whether_there_are_more(repo, alice, bob):
    conversation = new_conversation(repo, alice)
    for n in range(1, 8):
        say(repo, alice, conversation, uuid_like(100 + n), role="user" if n % 2 else "assistant")
    page, more = repo.messages_after(alice, conversation, after_seq=0, limit=3)
    assert ([m.seq for m in page], more) == ([1, 2, 3], True)
    page, more = repo.messages_after(alice, conversation, after_seq=3, limit=4)
    assert ([m.seq for m in page], more) == ([4, 5, 6, 7], False)               # 剛好用完：沒有更多
    assert repo.messages_after(alice, conversation, after_seq=7, limit=3) == ([], False)
    assert repo.messages_after(bob, conversation, after_seq=0, limit=3) == ([], False)      # 別人讀不到


def test_latest_messages_are_the_most_recent_ones_in_ascending_order(repo, alice, bob):
    conversation = new_conversation(repo, alice)
    for n in range(1, 8):
        say(repo, alice, conversation, uuid_like(200 + n))
    assert [m.seq for m in repo.latest_messages(alice, conversation, limit=3)] == [5, 6, 7]
    assert repo.latest_messages(bob, conversation, limit=3) == []


def test_a_message_can_be_found_by_its_client_id_only_by_its_owner(repo, alice, bob):
    conversation = new_conversation(repo, alice)
    say(repo, alice, conversation, MSG_A, role="assistant", content="回覆", msg_status=MSG_STATUS_FALLBACK)
    found = repo.find_message(alice, conversation, MSG_A)
    assert found.content == "回覆" and found.msg_status == "fallback"
    assert repo.find_message(alice, conversation, MSG_B) is None
    assert repo.find_message(bob, conversation, MSG_A) is None


# ---- 刪除 --------------------------------------------------------------------------------

def _ledger(session, user_id, conversation_id, status="ok"):
    row = LlmCallLog(
        user_id=user_id, conversation_id=conversation_id, kind="chat", provider="gemini", model="m", attempt=1,
        status=status, created_at=T0,
    )
    session.add(row)
    session.flush()
    return row


def test_deleting_a_conversation_removes_its_messages_but_keeps_the_ledger_without_the_reference(repo, member_session, alice):
    doomed = new_conversation(repo, alice, 1)
    kept = new_conversation(repo, alice, 2)
    say(repo, alice, doomed, MSG_A)
    say(repo, alice, doomed, MSG_B, role="assistant")
    say(repo, alice, kept, MSG_C)
    _ledger(member_session, alice, doomed.id)
    survivor_ledger = _ledger(member_session, alice, kept.id)
    member_session.commit()
    doomed_id, kept_id, survivor_id = doomed.id, kept.id, survivor_ledger.id

    assert repo.delete_conversation(alice, doomed) is True
    member_session.commit()

    assert member_session.scalar(select(func.count()).select_from(ChatConversation).where(ChatConversation.id == doomed_id)) == 0
    assert member_session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.conversation_id == doomed_id)) == 0
    assert member_session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.conversation_id == kept_id)) == 1
    ledgers = {row.id: row for row in member_session.scalars(select(LlmCallLog))}
    assert len(ledgers) == 2                                                    # 帳本一筆都沒少
    assert [r.conversation_id for r in ledgers.values() if r.id != survivor_id] == [None]    # 被刪對話的參照清除
    assert ledgers[survivor_id].conversation_id == kept_id                      # 其他對話的不受影響


def test_someone_else_cannot_delete_a_conversation(repo, member_session, alice, bob):
    conversation = new_conversation(repo, alice)
    say(repo, alice, conversation, MSG_A)
    member_session.commit()
    conversation_id = conversation.id
    assert repo.delete_conversation(bob, conversation) is False
    assert member_session.scalar(select(func.count()).select_from(ChatConversation).where(ChatConversation.id == conversation_id)) == 1
    assert member_session.scalar(select(func.count()).select_from(ChatMessage)) == 1


# ---- 並行：條件式 UPDATE 的原子性 ---------------------------------------------------------------

def run_in_threads(engine, worker, count: int = 8):
    """讓 count 個執行緒在同一個時間點開始，各自使用獨立的 Session（獨立連線）。"""
    barrier = Barrier(count)

    def task(index: int):
        with Session(engine) as s:
            barrier.wait(timeout=30)
            result = worker(s, index)
            s.commit()
            return result

    with ThreadPoolExecutor(max_workers=count) as pool:
        return [f.result(timeout=60) for f in [pool.submit(task, i) for i in range(count)]]


def _seed(engine):
    with Session(engine) as s:
        user = add_user(s, 1)
        conversation = ChatRepository(s).create_conversation(user.id, client_id=uuid_like(1), state=STATE, now=T0)
        s.commit()
        return user.id, conversation.id


def test_only_one_of_eight_parallel_requests_gets_the_processing_marker(concurrent_engine):
    user_id, conversation_id = _seed(concurrent_engine)

    def worker(s, _index):
        conversation = s.get(ChatConversation, conversation_id)
        return ChatRepository(s).acquire_processing(user_id, conversation, now=T0, stale_before=T0 - timedelta(seconds=60))

    assert sorted(run_in_threads(concurrent_engine, worker)) == [False] * 7 + [True]


def test_only_one_of_eight_parallel_summary_writes_wins_and_the_marker_never_goes_back(concurrent_engine):
    user_id, conversation_id = _seed(concurrent_engine)

    def worker(s, index):
        conversation = s.get(ChatConversation, conversation_id)
        return ChatRepository(s).write_summary(user_id, conversation, summary=f"結果 {index}", expected_upto_seq=0, new_upto_seq=2)

    assert sorted(run_in_threads(concurrent_engine, worker)) == [False] * 7 + [True]
    with Session(concurrent_engine) as s:
        assert s.get(ChatConversation, conversation_id).summary_upto_seq == 2


def test_only_one_of_eight_parallel_state_writes_wins_and_the_version_goes_up_once(concurrent_engine):
    user_id, conversation_id = _seed(concurrent_engine)

    def worker(s, index):
        conversation = s.get(ChatConversation, conversation_id)
        return ChatRepository(s).write_state(user_id, conversation, state={**STATE, "last_intent": f"i{index}"}, expected_version=1, now=T0)

    assert sorted(run_in_threads(concurrent_engine, worker)) == [False] * 7 + [True]
    with Session(concurrent_engine) as s:
        assert s.get(ChatConversation, conversation_id).state_version == 2


# ---- 用量限制的計數（usage-limits）-------------------------------------------------------------

def test_conversations_are_counted_per_user(repo, member_session, alice, bob):
    for n in (1, 2, 3):
        new_conversation(repo, alice, n)
    new_conversation(repo, bob, 1)
    assert (repo.count_conversations(alice), repo.count_conversations(bob)) == (3, 1)


def test_deleting_a_conversation_frees_its_slot(repo, alice):
    first = new_conversation(repo, alice, 1)
    new_conversation(repo, alice, 2)
    assert repo.delete_conversation(alice, first) is True
    assert repo.count_conversations(alice) == 1


def test_a_user_without_conversations_has_zero(repo, alice):
    assert repo.count_conversations(alice) == 0


def test_processing_counts_only_fresh_markers_and_can_exclude_one_conversation(repo, member_session, alice, bob):
    now = T0 + timedelta(minutes=10)
    fresh_since = now - timedelta(seconds=60)
    mine, other, stale, idle = (new_conversation(repo, alice, n) for n in (1, 2, 3, 4))
    theirs = new_conversation(repo, bob, 1)
    assert repo.acquire_processing(alice, mine, now=now, stale_before=fresh_since)
    assert repo.acquire_processing(alice, other, now=now - timedelta(seconds=30), stale_before=fresh_since)
    assert repo.acquire_processing(alice, stale, now=now - timedelta(seconds=61), stale_before=now - timedelta(seconds=120))
    assert repo.acquire_processing(bob, theirs, now=now, stale_before=fresh_since)
    assert idle.processing_since is None
    assert repo.count_processing(alice, fresh_since=fresh_since) == 2                           # 過期的與別人的不算
    assert repo.count_processing(alice, fresh_since=fresh_since, exclude_conversation_id=mine.id) == 1


def test_recent_user_message_times_return_only_accepted_user_messages_of_that_user_newest_first(repo, member_session, alice, bob):
    first, second = new_conversation(repo, alice, 1), new_conversation(repo, alice, 2)
    theirs = new_conversation(repo, bob, 1)
    base = T0 + timedelta(minutes=5)
    say(repo, alice, first, uuid_like(11), now=base)
    say(repo, alice, first, uuid_like(12), role="assistant", now=base + timedelta(seconds=2))      # 助理訊息不算
    say(repo, alice, second, uuid_like(13), now=base + timedelta(seconds=10))                        # 不同對話也算
    say(repo, bob, theirs, uuid_like(14), now=base + timedelta(seconds=11))                          # 別人的不算
    say(repo, alice, first, uuid_like(15), now=base - timedelta(minutes=5))                          # 視窗之前
    times = repo.recent_user_message_times(alice, since=base - timedelta(seconds=1), limit=10)
    assert times == [base + timedelta(seconds=10), base]


def test_recent_user_message_times_respect_the_limit_and_the_inclusive_start(repo, alice):
    conversation = new_conversation(repo, alice)
    base = T0 + timedelta(minutes=5)
    for n in range(8):
        say(repo, alice, conversation, uuid_like(20 + n), now=base + timedelta(seconds=n))
    newest_three = repo.recent_user_message_times(alice, since=base, limit=3)
    assert newest_three == [base + timedelta(seconds=n) for n in (7, 6, 5)]
    assert repo.recent_user_message_times(alice, since=base + timedelta(seconds=7), limit=3) == [base + timedelta(seconds=7)]
    assert repo.recent_user_message_times(alice, since=base + timedelta(seconds=8), limit=3) == []
