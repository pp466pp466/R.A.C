"""personal-data-export 規格：匯出的內容範圍、JSON／CSV 格式與編碼、公式字元中和、分頁完整、跨使用者隔離。

以 `member_session` 同時在 SQLite 與真實 SQL Server 上執行。
"""
import csv
import io
import json
import zipfile
from datetime import date, timedelta

import pytest

from app.models import AuthToken, ChatConversation, ChatMessage, User, UserIdentity
from app.models.user import PROVIDER_GOOGLE, TOKEN_RESET_PASSWORD
from app.repositories.chat_repository import ChatRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services import personal_export_service
from app.services.current_user import CurrentUser
from app.services.errors import ValidationError
from app.services.personal_export_service import (
    ACCOUNT_COLUMNS, APPEAL_COLUMNS, MESSAGE_COLUMNS, PersonalExportService, csv_safe,
)
from tests.support.auth import FAST_HASHER, GOOD_PASSWORD, START, FakeClock, add_user
from tests.support.age import birth_date_for_age

UTF8_BOM = b"\xef\xbb\xbf"
SECRET_SUBJECT = "google-sub-should-not-leak"
SECRET_TOKEN_HASH = "f" * 64
SECRET_ENTITY = "ZZ-ENTITY-9"
SECRET_INTENT = "complaint_feedback"
TEXT = "繁體中文 😀 的訊息，含逗號、\"引號\"與\n換行"


def service_for(session) -> PersonalExportService:
    return PersonalExportService(
        users=UserRepository(session), identities=UserIdentityRepository(session), chats=ChatRepository(session),
        appeals=LockAppealRepository(session), clock=FakeClock(),
    )


def me(session, user: User) -> CurrentUser:
    session.expire_all()
    return CurrentUser.from_model(session.get(User, user.id))


def seed_conversation(session, user_id: int, n: int, *, title: str = "對話", contents=(TEXT, "第二則")) -> ChatConversation:
    chats = ChatRepository(session)
    conv = chats.create_conversation(user_id, client_id=f"{n:036d}", state={}, now=START + timedelta(minutes=n))
    conv.title = title
    conv.summary = f"摘要{n}"
    for position, content in enumerate(contents, start=1):
        chats.append_message(
            user_id, conv, client_msg_id=f"{n * 1000 + position:036d}", role="user" if position % 2 else "assistant", content=content,
            now=START + timedelta(minutes=n, seconds=position), intent=SECRET_INTENT, entities={"order": SECRET_ENTITY},
            suggested_actions=[{"text": "查看額度"}], confidence=0.9,
        )
    session.commit()
    return conv


def rich_user(session) -> User:
    user = add_user(
        session, 1, age=30, email="owner@example.com", email_canonical="owner@example.com", display_name="王小明",
        email_verified_at=START, last_login_at=START, terms_version="2026-10-28-draft-2", terms_accepted_at=START,
    )
    session.add(UserIdentity(user_id=user.id, provider=PROVIDER_GOOGLE, provider_subject=SECRET_SUBJECT))
    session.add(AuthToken(user_id=user.id, purpose=TOKEN_RESET_PASSWORD, token_hash=SECRET_TOKEN_HASH, expires_at=START))
    session.commit()
    return user


def read_json(result) -> dict:
    raw = result.handle.read()
    assert not raw.startswith(UTF8_BOM)                          # JSON 一律不加 BOM
    return json.loads(raw.decode("utf-8"))


def read_zip(result) -> dict[str, bytes]:
    with zipfile.ZipFile(result.handle) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def csv_rows(raw: bytes) -> list[list[str]]:
    assert raw.startswith(UTF8_BOM)                              # CSV 一律 UTF-8-sig，Excel 才不會把繁體中文開成亂碼
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))


# ---- JSON -------------------------------------------------------------------------

def test_json_round_trips_traditional_chinese_emoji_and_control_characters_without_a_bom(member_session):
    user = rich_user(member_session)
    seed_conversation(member_session, user.id, 1, title="我的第一個對話 🎉")
    result = service_for(member_session).build(me(member_session, user), "json")
    raw_probe = service_for(member_session).build(me(member_session, user), "json").handle.read()
    assert "繁體中文".encode("utf-8") in raw_probe and b"\\u7e41" not in raw_probe          # 原字元，不是跳脫碼

    data = read_json(result)
    (conversation,) = data["conversations"]
    assert conversation["title"] == "我的第一個對話 🎉" and conversation["summary"] == "摘要1"
    assert [m["content"] for m in conversation["messages"]] == [TEXT, "第二則"]
    assert [m["seq"] for m in conversation["messages"]] == [1, 2] and [m["role"] for m in conversation["messages"]] == ["user", "assistant"]
    assert data["export_version"] == 1 and data["exported_at"] == "2026-10-27T12:00:00Z"


def test_json_account_section_has_the_documented_fields_including_the_birthday_and_range(member_session):
    user = rich_user(member_session)
    account = read_json(service_for(member_session).build(me(member_session, user), "json"))["account"]
    assert list(account) == list(ACCOUNT_COLUMNS)
    assert account["email"] == "owner@example.com" and account["display_name"] == "王小明" and account["member_tier"] == "free"
    assert account["birth_date"] == birth_date_for_age(30).isoformat() and account["age_range"] == "25_34"
    assert account["google_linked"] is True and account["terms_version"] == "2026-10-28-draft-2"
    assert account["email_verified_at"] == "2026-10-27T12:00:00Z"


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_the_export_never_contains_credentials_internal_ids_or_model_internals(member_session, fmt):
    user = rich_user(member_session)
    seed_conversation(member_session, user.id, 1)
    result = service_for(member_session).build(me(member_session, user), fmt)
    everything = result.handle.read()
    if fmt == "csv":
        everything = b"".join(read_zip(type("R", (), {"handle": io.BytesIO(everything)})()).values())
    text = everything.decode("utf-8-sig", "ignore")
    password_hash = member_session.get(User, user.id).password_hash
    for secret in (
        password_hash, SECRET_TOKEN_HASH, SECRET_SUBJECT, SECRET_ENTITY, SECRET_INTENT, "token_version", "password", "provider_subject",
        "entities", "suggested_actions", "confidence", "state_json", "intent",
    ):
        assert secret not in text, secret
    assert GOOD_PASSWORD not in text


def test_a_user_with_no_conversations_still_gets_a_valid_file(member_session):
    user = add_user(member_session, 1, age=None)
    data = read_json(service_for(member_session).build(me(member_session, user), "json"))
    assert data["conversations"] == [] and data["account"]["birth_date"] is None and data["account"]["google_linked"] is False


def test_another_users_conversations_are_never_included(member_session):
    owner = add_user(member_session, 1)
    other = add_user(member_session, 2)
    seed_conversation(member_session, owner.id, 1, title="我的", contents=("我的內容",))
    seed_conversation(member_session, other.id, 2, title="別人的", contents=("別人的秘密內容",))
    for fmt in ("json", "csv"):
        result = service_for(member_session).build(me(member_session, owner), fmt)
        blob = result.handle.read()
        if fmt == "csv":
            blob = b"".join(read_zip(type("R", (), {"handle": io.BytesIO(blob)})()).values())
        assert "別人".encode("utf-8") not in blob and "我的內容".encode("utf-8") in blob


# ---- 分頁 -------------------------------------------------------------------------

@pytest.mark.parametrize("count", [1, 4, 5, 6, 10, 11])
def test_every_message_is_exported_in_order_across_page_boundaries(member_session, monkeypatch, count):
    monkeypatch.setattr(personal_export_service, "PAGE_SIZE", 5)
    user = add_user(member_session, 1)
    seed_conversation(member_session, user.id, 1, contents=tuple(f"內容{n}" for n in range(1, count + 1)))
    seed_conversation(member_session, user.id, 2, contents=("另一個對話",))
    service = service_for(member_session)

    conversations = read_json(service.build(me(member_session, user), "json"))["conversations"]
    assert [m["content"] for m in conversations[0]["messages"]] == [f"內容{n}" for n in range(1, count + 1)]
    assert [m["content"] for m in conversations[1]["messages"]] == ["另一個對話"]

    rows = csv_rows(read_zip(service.build(me(member_session, user), "csv"))["messages.csv"])
    assert len(rows) == 1 + count + 1 and [r[4] for r in rows[1:1 + count]] == [f"內容{n}" for n in range(1, count + 1)]


# ---- CSV --------------------------------------------------------------------------

def test_csv_is_a_zip_of_three_utf8_sig_files_that_excel_can_read(member_session):
    user = rich_user(member_session)
    seed_conversation(member_session, user.id, 1, title="含,逗號的標題")
    result = service_for(member_session).build(me(member_session, user), "csv")
    assert result.mimetype == "application/zip" and result.filename == "my-data-20261027.zip"
    files = read_zip(result)
    assert sorted(files) == ["account.csv", "appeals.csv", "messages.csv"]

    account_rows = csv_rows(files["account.csv"])
    assert account_rows[0] == list(ACCOUNT_COLUMNS) and len(account_rows) == 2
    assert dict(zip(account_rows[0], account_rows[1]))["birth_date"] == birth_date_for_age(30).isoformat()

    message_rows = csv_rows(files["messages.csv"])
    assert message_rows[0] == list(MESSAGE_COLUMNS)
    first = dict(zip(message_rows[0], message_rows[1]))
    assert first["conversation_title"] == "含,逗號的標題" and first["content"] == TEXT          # 逗號、雙引號、換行都往返無損
    assert first["seq"] == "1" and first["role"] == "user" and len(first["conversation_id"]) == 36


@pytest.mark.parametrize("dangerous", ["=HYPERLINK(\"http://evil\")", "+1+1", "-2+3", "@SUM(A1)", "\t=1", "\r=1"])
def test_cells_that_start_like_a_formula_are_neutralised(member_session, dangerous):
    user = add_user(member_session, 1, display_name=dangerous.strip() or "x")
    seed_conversation(member_session, user.id, 1, title=dangerous, contents=(dangerous,))
    files = read_zip(service_for(member_session).build(me(member_session, user), "csv"))
    message = csv_rows(files["messages.csv"])[1]
    assert message[1] == "'" + dangerous and message[4] == "'" + dangerous
    assert csv_rows(files["account.csv"])[1][1].startswith("'")


def test_csv_safe_leaves_ordinary_values_alone():
    assert csv_safe("你好") == "你好" and csv_safe(12) == 12 and csv_safe(None) is None and csv_safe("") == ""
    assert csv_safe("a=b") == "a=b" and csv_safe("'已經有引號") == "'已經有引號"


# ---- 輸入與回傳 --------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["", "xml", None, 1, ["json"], "JSON"])
def test_an_unknown_format_is_a_field_error(member_session, fmt):
    user = add_user(member_session, 1)
    with pytest.raises(ValidationError) as exc:
        service_for(member_session).build(me(member_session, user), fmt)
    assert list(exc.value.fields) == ["format"]


def test_the_result_is_a_rewound_temporary_file_with_an_ascii_name(member_session):
    user = add_user(member_session, 1)
    result = service_for(member_session).build(me(member_session, user), "json")
    assert result.handle.tell() == 0 and result.filename == "my-data-20261027.json"
    assert result.filename.isascii() and result.mimetype.startswith("application/json")
    result.handle.close()


def test_the_event_carries_the_id_and_the_format_only(member_session, log_lines):
    user = rich_user(member_session)
    seed_conversation(member_session, user.id, 1)
    service_for(member_session).build(me(member_session, user), "csv")
    (event,) = [line for line in log_lines() if line.get("event") == "account.exported"]
    assert event["user_id"] == user.id and event["reason"] == "csv"
    for secret in ("owner@example.com", "王小明", SECRET_SUBJECT, birth_date_for_age(30).isoformat(), "繁體中文"):
        assert secret not in log_lines.raw()


def test_a_failure_while_writing_closes_the_temporary_file(member_session, monkeypatch):
    user = add_user(member_session, 1)
    seed_conversation(member_session, user.id, 1)
    opened = []
    real = personal_export_service.tempfile.SpooledTemporaryFile

    def tracking(*args, **kwargs):
        handle = real(*args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(personal_export_service.tempfile, "SpooledTemporaryFile", tracking)
    monkeypatch.setattr(ChatRepository, "messages_after", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("資料庫暫時失敗")))
    with pytest.raises(RuntimeError):
        service_for(member_session).build(me(member_session, user), "json")
    assert len(opened) == 1 and opened[0].closed


# ---- 申訴（google-first-login）--------------------------------------------------------------

APPEAL_TEXT = "我是不小心連按了很多次 😀，請解除鎖定。\n謝謝，=SUM(A1)"


def seed_appeals(session, user: User, other: User) -> None:
    repo = LockAppealRepository(session)
    first = repo.create(user.id, APPEAL_TEXT, START)
    first.handled_at = START + timedelta(hours=1)
    repo.create(user.id, "第二則", START + timedelta(hours=2))
    repo.create(other.id, "別人的申訴，不該出現", START)
    session.commit()


def test_the_json_export_has_my_appeals_oldest_first_with_their_status_and_nobody_elses(member_session):
    user, other = rich_user(member_session), add_user(member_session, 2)
    seed_appeals(member_session, user, other)
    data = read_json(service_for(member_session).build(me(member_session, user), "json"))
    assert [(a["content"], a["status"]) for a in data["appeals"]] == [(APPEAL_TEXT, "handled"), ("第二則", "pending")]
    assert set(data["appeals"][0]) == {"id", "content", "created_at", "status"} and len(data["appeals"][0]["id"]) == 36
    assert data["appeals"][0]["created_at"] == "2026-10-27T12:00:00Z"
    assert "別人的申訴" not in json.dumps(data, ensure_ascii=False)


def test_the_json_has_an_empty_appeals_list_when_there_are_none(member_session):
    user = rich_user(member_session)
    assert read_json(service_for(member_session).build(me(member_session, user), "json"))["appeals"] == []


def test_the_csv_export_has_an_appeals_file_with_neutralised_formulas_and_nobody_elses(member_session):
    user, other = rich_user(member_session), add_user(member_session, 2)
    seed_appeals(member_session, user, other)
    files = read_zip(service_for(member_session).build(me(member_session, user), "csv"))
    rows = csv_rows(files["appeals.csv"])
    assert rows[0] == list(APPEAL_COLUMNS) and len(rows) == 3
    first = dict(zip(rows[0], rows[1]))
    assert first["content"] == APPEAL_TEXT and first["status"] == "handled"           # 開頭不是公式字元；換行與表情符號往返無損
    assert dict(zip(rows[0], rows[2]))["status"] == "pending"
    assert b"\xe5\x88\xa5\xe4\xba\xba\xe7\x9a\x84\xe7\x94\xb3\xe8\xa8\xb4" not in files["appeals.csv"]    # 「別人的申訴」
    assert files["appeals.csv"].startswith(UTF8_BOM)


def test_an_appeal_that_starts_like_a_formula_is_neutralised_in_the_csv(member_session):
    user = rich_user(member_session)
    LockAppealRepository(member_session).create(user.id, "=HYPERLINK(\"http://evil\")", START)
    member_session.commit()
    rows = csv_rows(read_zip(service_for(member_session).build(me(member_session, user), "csv"))["appeals.csv"])
    assert rows[1][1] == "'=HYPERLINK(\"http://evil\")"


def test_lock_strikes_and_lock_state_are_not_part_of_the_export(member_session):
    from app.models import LockStrike

    user = add_user(member_session, 1, locked_at=START, lock_reason="rate_limited")
    member_session.add(LockStrike(user_id=user.id, kind="rate_limited", created_at=START))
    member_session.commit()
    for fmt in ("json", "csv"):
        result = service_for(member_session).build(me(member_session, user), fmt)
        raw = result.handle.read()
        if fmt == "csv":
            raw = b"".join(read_zip(type("R", (), {"handle": io.BytesIO(raw)})()).values())
        text = raw.decode("utf-8-sig", "ignore")
        for hidden in ("lock_strikes", "locked_at", "lock_reason", "rate_limited", "identity_rejected", "age_deadline_at"):
            assert hidden not in text, (fmt, hidden)
