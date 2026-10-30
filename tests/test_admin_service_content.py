"""admin-content-review 規格：清單只有中繼資料、只有兩類對話可讀全文、讀取與刪除的稽核、模型失敗的原文。"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import ChatConversation, ChatMessage, LlmCallLog
from app.services.errors import NotFound, ValidationError
from tests.support.admin import GOOD_PASSWORD, WRONG_PASSWORD, AdminKit

SECRET_WORDS = ("使用者說的話", "客服的回覆")


@pytest.fixture()
def kit(member_session):
    return AdminKit(member_session)


def audit_actions(kit):
    return [row.action for row in kit.audit_rows()]


def count(kit, model):
    kit.session.expire_all()
    return kit.session.scalar(select(func.count()).select_from(model))


# ---- 清單 -----------------------------------------------------------------------------------

def test_the_feedback_list_covers_feedback_and_handoff_but_not_unknown_and_has_no_content(kit):
    admin = kit.admin()
    alice, bob, carol = kit.member(2), kit.member(3), kit.member(4)
    feedback = kit.conversation(alice, turns=("general_qa", "complaint_feedback"))
    handoff = kit.conversation(bob, turns=("human_handoff", "unknown"))
    kit.conversation(carol, turns=("unknown", "unknown", None, "greeting"))
    items = kit.service.feedback_conversations(admin)
    assert {(i["conversation"], i["user"], i["count"]) for i in items} == {
        (feedback.public_id, alice.public_id, 1), (handoff.public_id, bob.public_id, 1),
    }
    assert all(set(i) == {"conversation", "user", "count", "latest_at"} for i in items)
    assert not any(word in json.dumps(items, ensure_ascii=False) for word in SECRET_WORDS)


def test_the_lists_carry_no_email_or_name(kit):
    admin = kit.admin()
    member = kit.member(2)
    conv = kit.conversation(member, turns=("complaint_feedback",))
    kit.block(member, conv)
    text = json.dumps([kit.service.feedback_conversations(admin), kit.service.flagged_conversations(admin)], ensure_ascii=False)
    assert member.email not in text and member.display_name not in text and "user_id" not in text


def test_the_flagged_list_says_how_serious_the_warning_is_for_each_user(kit):
    admin = kit.admin()
    noisy, mild = kit.member(2), kit.member(3)
    noisy_conv, mild_conv = kit.conversation(noisy, turns=(None,)), kit.conversation(mild, turns=(None,))
    now = kit.clock()
    for minutes in (30, 20, 10):
        kit.block(noisy, noisy_conv, at=now - timedelta(minutes=minutes))
    kit.block(mild, mild_conv, at=now - timedelta(minutes=5))
    by_user = {i["user"]: i for i in kit.service.flagged_conversations(admin)}
    assert by_user[noisy.public_id]["blocked_count"] == 3 and by_user[noisy.public_id]["user_blocked_today"] == 3
    assert by_user[noisy.public_id]["user_chat_suspended_until"] == (now - timedelta(minutes=10) + timedelta(hours=1)).isoformat(timespec="seconds")
    assert by_user[mild.public_id]["user_blocked_today"] == 1 and by_user[mild.public_id]["user_chat_suspended_until"] is None
    assert set(by_user[noisy.public_id]) == {
        "conversation", "user", "blocked_count", "latest_at", "user_blocked_today", "user_chat_suspended_until",
    }


def test_the_flagged_suspension_rule_is_the_one_the_chat_uses(kit):
    """被標記清單算出的暫停截止時間，必須與客服實際使用的 `UsageService` 一致。"""
    admin = kit.admin()
    member = kit.member(2)
    conv = kit.conversation(member, turns=(None,))
    for minutes in (50, 40, 3):
        kit.block(member, conv, at=kit.clock() - timedelta(minutes=minutes))
    shown = kit.service.flagged_conversations(admin)[0]["user_chat_suspended_until"]
    actual = kit.service._usage.suspended_until(kit.current(member))
    assert shown == actual.isoformat(timespec="seconds")


def test_the_list_pages_are_validated(kit):
    admin = kit.admin()
    for method in (kit.service.feedback_conversations, kit.service.flagged_conversations):
        with pytest.raises(ValidationError):
            method(admin, limit=101)
        with pytest.raises(ValidationError):
            method(admin, limit=0)
        with pytest.raises(ValidationError):
            method(admin, offset=-1)


def test_reading_the_lists_writes_no_audit(kit):
    admin = kit.admin()
    kit.service.feedback_conversations(admin)
    kit.service.flagged_conversations(admin)
    assert kit.audit_rows() == []


# ---- 讀取全文 -------------------------------------------------------------------------------

def test_a_feedback_conversation_can_be_read_in_full_and_the_read_is_audited_without_text(kit):
    admin = kit.admin()
    member = kit.member(2)
    conv = kit.conversation(member, turns=("complaint_feedback", "general_qa"))
    result = kit.service.read_conversation(admin, conv.public_id)
    assert result["conversation"] == {"id": conv.public_id, "user": member.public_id, "is_feedback": True, "is_flagged": False}
    assert [(m["seq"], m["role"]) for m in result["messages"]] == [(1, "user"), (2, "assistant"), (3, "user"), (4, "assistant")]
    assert result["messages"][0]["content"] == "使用者說的話0" and result["has_more"] is False
    [entry] = kit.audit_rows()
    assert (entry.action, entry.target_user_public_id, entry.target_conversation_public_id) == ("conversation.read", member.public_id, conv.public_id)
    assert not any(word in repr(entry) for word in SECRET_WORDS)


def test_a_flagged_conversation_can_be_read_and_says_it_was_flagged(kit):
    admin = kit.admin()
    member = kit.member(2)
    conv = kit.conversation(member, turns=("general_qa", None))
    kit.block(member, conv)
    conversation = kit.service.read_conversation(admin, conv.public_id)["conversation"]
    assert (conversation["is_feedback"], conversation["is_flagged"]) == (False, True)


def test_the_messages_do_not_expose_internal_fields_or_the_users_identity(kit):
    admin = kit.admin()
    member = kit.member(2)
    conv = kit.conversation(member, turns=("complaint_feedback",))
    result = kit.service.read_conversation(admin, conv.public_id)
    assert all(set(m) == {"seq", "role", "content", "msg_status", "created_at"} for m in result["messages"])
    text = json.dumps(result, ensure_ascii=False)
    for private in (member.email, member.display_name, "intent", "entities", "confidence", "state_json"):
        assert private not in text


def test_an_ordinary_conversation_is_a_404_identical_to_a_missing_one_and_leaves_no_trace(kit):
    admin = kit.admin()
    member = kit.member(2)
    ordinary = kit.conversation(member, turns=("general_qa", "unknown"))
    with pytest.raises(NotFound) as ordinary_error:
        kit.service.read_conversation(admin, ordinary.public_id)
    with pytest.raises(NotFound) as missing_error:
        kit.service.read_conversation(admin, "00000000-0000-4000-8000-000000000000")
    assert (ordinary_error.value.code, ordinary_error.value.message) == (missing_error.value.code, missing_error.value.message)
    assert kit.audit_rows() == []


@pytest.mark.parametrize("public_id", [None, "", 7, "x" * 300])
def test_odd_identifiers_are_a_404(kit, public_id):
    with pytest.raises(NotFound):
        kit.service.read_conversation(kit.admin(), public_id)


def test_a_long_conversation_is_cut_at_200_messages_and_says_there_is_more(kit):
    admin = kit.admin()
    member = kit.member(2)
    conv = kit.conversation(member, turns=("complaint_feedback",) * 101)            # 202 則
    result = kit.service.read_conversation(admin, conv.public_id)
    assert len(result["messages"]) == 200 and result["has_more"] is True


def test_when_the_audit_cannot_be_written_no_content_is_returned(kit, monkeypatch):
    admin = kit.admin()
    member = kit.member(2)
    conv = kit.conversation(member, turns=("complaint_feedback",))

    def broken(**kwargs):
        raise RuntimeError("資料庫寫入失敗")

    monkeypatch.setattr(kit.audit, "add", broken)
    monkeypatch.setattr(kit.admins, "read_messages", lambda *a, **k: pytest.fail("稽核寫入失敗時不應讀取內容"))
    with pytest.raises(RuntimeError):
        kit.service.read_conversation(admin, conv.public_id)


# ---- 刪除 -----------------------------------------------------------------------------------

def test_deleting_a_flagged_conversation_removes_it_keeps_the_ledger_and_records_who_why_and_what(kit, log_lines):
    admin = kit.admin()
    member = kit.member(2)
    conv = kit.conversation(member, turns=("general_qa", None))
    kit.block(member, conv)
    public_id, user_public_id, conv_id = conv.public_id, member.public_id, conv.id
    ledger_rows = count(kit, LlmCallLog)
    kit.service.delete_conversation(admin, public_id, reason="  違規內容  ", password=GOOD_PASSWORD)
    assert count(kit, ChatConversation) == 0 and count(kit, ChatMessage) == 0
    assert count(kit, LlmCallLog) == ledger_rows
    assert kit.session.scalar(select(func.count()).select_from(LlmCallLog).where(LlmCallLog.conversation_id == conv_id)) == 0
    [entry] = kit.audit_rows()
    assert (entry.action, entry.target_user_public_id, entry.target_conversation_public_id, entry.reason) == (
        "conversation.delete", user_public_id, public_id, "違規內容",
    )
    assert kit.service.flagged_conversations(admin) == []
    events = [line for line in log_lines() if line.get("event") == "admin.action"]
    assert [e["reason"] for e in events] == ["conversation.delete"] and "違規內容" not in log_lines.raw()


def test_deleting_a_feedback_conversation_is_allowed_too(kit):
    admin = kit.admin()
    conv = kit.conversation(kit.member(2), turns=("complaint_feedback",))
    kit.service.delete_conversation(admin, conv.public_id, reason="含個資", password=GOOD_PASSWORD)
    assert count(kit, ChatConversation) == 0


def test_an_ordinary_conversation_cannot_be_deleted_and_nothing_changes(kit):
    admin = kit.admin()
    conv = kit.conversation(kit.member(2), turns=("general_qa",))
    with pytest.raises(NotFound):
        kit.service.delete_conversation(admin, conv.public_id, reason="想刪", password=GOOD_PASSWORD)
    assert count(kit, ChatConversation) == 1 and kit.audit_rows() == []


@pytest.mark.parametrize("reason", [None, "", "   ", "x" * 201])
def test_deleting_needs_a_reason_of_1_to_200_characters(kit, reason):
    admin = kit.admin()
    conv = kit.conversation(kit.member(2), turns=("complaint_feedback",))
    with pytest.raises(ValidationError) as exc:
        kit.service.delete_conversation(admin, conv.public_id, reason=reason, password=GOOD_PASSWORD)
    assert set(exc.value.fields) == {"reason"} and count(kit, ChatConversation) == 1


@pytest.mark.parametrize("password", [None, "", WRONG_PASSWORD])
def test_deleting_needs_the_right_password_and_a_failed_attempt_is_audited_with_the_target(kit, password):
    admin = kit.admin()
    member = kit.member(2)
    conv = kit.conversation(member, turns=("complaint_feedback",))
    with pytest.raises(ValidationError) as exc:
        kit.service.delete_conversation(admin, conv.public_id, reason="違規", password=password)
    assert set(exc.value.fields) == {"password"} and count(kit, ChatConversation) == 1
    [entry] = kit.audit_rows()
    assert (entry.action, entry.target_user_public_id, entry.target_conversation_public_id, entry.detail) == (
        "reauth.failed", member.public_id, conv.public_id, {"attempted": "conversation.delete"},
    )
    assert kit.persisted.calls >= 1


def test_there_is_no_way_to_delete_a_single_message():
    from app.services.admin_service import AdminService

    public = [name for name in dir(AdminService) if not name.startswith("_")]
    assert not [name for name in public if "message" in name and "delete" in name]


# ---- 模型失敗的原文 -------------------------------------------------------------------------

def test_recent_failures_are_listed_newest_first_with_only_the_documented_fields_and_the_read_is_audited(kit):
    admin = kit.admin()
    now = kit.clock()
    kit.call(at=now - timedelta(days=2), status="validation_failed", error_detail="前天的壞輸出", model="m1")
    kit.call(at=now - timedelta(hours=1), status="validation_failed", error_detail="一小時前的壞輸出", kind="summary", model="m2")
    kit.call(at=now - timedelta(hours=2), status="timeout")                                    # 沒有原文
    kit.call(at=now - timedelta(days=20), status="validation_failed", error_detail="太舊")
    items = kit.service.failures(admin)
    assert [i["detail"] for i in items] == ["一小時前的壞輸出", "前天的壞輸出"]
    assert set(items[0]) == {"created_at", "kind", "model", "detail"} and (items[0]["kind"], items[0]["model"]) == ("summary", "m2")
    [entry] = kit.audit_rows()
    assert (entry.action, entry.detail) == ("failures.read", {"count": 2}) and "壞輸出" not in repr(entry)


def test_the_window_is_exactly_14_days_and_does_not_depend_on_any_purge(kit):
    admin = kit.admin()
    now = kit.clock()
    kit.call(at=now - timedelta(days=14), status="validation_failed", error_detail="剛好 14 天")
    kit.call(at=now - timedelta(days=14, seconds=1), status="validation_failed", error_detail="超過 14 天")
    assert [i["detail"] for i in kit.service.failures(admin)] == ["剛好 14 天"]


def test_an_empty_failure_list_is_still_audited_with_a_zero_count(kit):
    assert kit.service.failures(kit.admin()) == []
    assert kit.audit_rows()[0].detail == {"count": 0}


def test_failures_carry_no_user_or_conversation_identity(kit):
    admin = kit.admin()
    member = kit.member(2)
    kit.call(user=member, status="validation_failed", error_detail="壞輸出")
    text = json.dumps(kit.service.failures(admin), ensure_ascii=False)
    assert member.public_id not in text and member.email not in text


def test_the_failure_list_is_capped_at_100_rows(kit):
    admin = kit.admin()
    for minute in range(105):
        kit.call(at=kit.clock() - timedelta(minutes=minute), status="validation_failed", error_detail=f"第{minute}筆")
    assert len(kit.service.failures(admin)) == 100


# ---- 非管理員 -------------------------------------------------------------------------------

def test_a_non_admin_gets_a_404_for_every_content_operation_and_nothing_changes(kit):
    member = kit.member(2)
    conv = kit.conversation(member, turns=("complaint_feedback",))
    intruder = kit.current(kit.member(3, member_tier="vip"))
    public_id = conv.public_id
    calls = (
        lambda: kit.service.feedback_conversations(intruder),
        lambda: kit.service.flagged_conversations(intruder),
        lambda: kit.service.read_conversation(intruder, public_id),
        lambda: kit.service.delete_conversation(intruder, public_id, reason="違規", password=GOOD_PASSWORD),
        lambda: kit.service.failures(intruder),
    )
    for call in calls:
        with pytest.raises(NotFound):
            call()
    assert count(kit, ChatConversation) == 1 and kit.audit_rows() == [] and kit.persisted.calls == 0
