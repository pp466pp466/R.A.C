"""script-submission 規格：送審——固定的檢查順序、自動拒絕只告知類別、送審紀錄只有類別與時間、每日次數與台灣日界、
連續被拒的暫停發佈（條件式、管理員不被暫停、成功一次即中斷）、審查不可用不計次。

端到端：測試應用程式、假的 RPG 模型供應商（`create_app(rpg_llm_provider=...)`）與真實的資料庫；不打真實 API。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.llm.types import LLMTransientError
from app.models import AdminAuditLog, Base, LlmCallLog, RuntimeLimit, ScriptSubmission, ScriptVersion, User
from app.repositories.script_repository import ScriptRepository
from app.repositories.user_repository import UserRepository
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.llm import FakeLLMProvider, Step
from tests.support.script_data import valid, validated

UNIQUE = "獨特的送審文字SUBMIT-SECRET-TEXT"
NO_CALL = object()


def reply(verdict="pass", categories=()):
    return json.dumps({"verdict": verdict, "categories": list(categories)}, ensure_ascii=False)


class Env:
    """一個測試應用程式、它的假時鐘與假的 RPG 模型供應商。"""

    def __init__(self, *steps, default=None):
        self.clock = FakeClock()
        self.provider = FakeLLMProvider(default=Step(default) if default is not None else None).script(*steps)
        self.app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=self.clock, rpg_llm_provider=self.provider)
        Base.metadata.create_all(self.app.extensions["db_engine"])

    def db(self) -> Session:
        return Session(self.app.extensions["db_engine"])

    def author(self, n: int = 1, **overrides):
        with self.db() as s:
            user = add_user(s, n, nickname=f"作者{n}", nickname_code=f"AB{n:02d}", **overrides)
            uid, tv = user.id, user.token_version
        client = self.app.test_client()
        sign_in_as(client, uid, tv)
        return client

    def limit(self, key: str, value: int) -> None:
        with self.db() as s:
            s.add(RuntimeLimit(key=key, int_value=value, updated_at=self.clock()))
            s.commit()

    def rows(self, model):
        with self.db() as s:
            return list(s.scalars(select(model).order_by(model.id)))

    def user(self, n: int = 1) -> User:
        with self.db() as s:
            return s.scalar(select(User).where(User.email == f"user{n}@example.com"))


def create(client, **changes) -> dict:
    response = client.post("/api/scripts", json={"content": valid(**changes)})
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()["version"]


def submit(client, version):
    return client.post(f"/api/scripts/versions/{version['id']}/submit")


def edit(client, version, **changes) -> dict:
    response = client.put(f"/api/scripts/versions/{version['id']}", json={"content": valid(**changes), "base_hash": version["content_hash"]})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["version"]


def outcomes(env: Env, n: int = 1) -> list[str]:
    return [row.outcome for row in env.rows(ScriptSubmission) if row.user_id == env.user(n).id]


def insert_unchecked_draft(env: Env, n: int = 1, **changes) -> str:
    """繞過儲存時的規則層直接寫入一份草稿（模擬規則詞表更新之前就存下的內容），回傳版本的對外識別碼。"""
    owner = env.user(n)
    with env.db() as s:
        v = validated(**changes)
        version = ScriptRepository(s).create_script(
            owner.id, author_name="甲", author_code="AAAA", content=v.content, content_hash=v.content_hash, total_chars=v.total_chars,
        )
        s.commit()
        return version.public_id


# ---- 成功 ------------------------------------------------------------------------------------------------


def test_a_clean_draft_becomes_pending_and_leaves_one_sent_record_and_one_ledger_row():
    env = Env(reply())
    client = env.author()
    version = create(client)
    response = submit(client, version)
    assert response.status_code == 200
    sent = response.get_json()["version"]
    assert sent["status"] == "pending_review" and sent["id"] == version["id"] and sent["content_hash"] == version["content_hash"]
    assert outcomes(env) == ["sent"]
    (record,) = env.rows(ScriptSubmission)
    assert record.categories_json is None
    (stored,) = env.rows(ScriptVersion)
    assert stored.review_verdict == "pass" and stored.review_hash == stored.content_hash and stored.open_script_id is not None
    ledger = env.rows(LlmCallLog)
    assert [row.kind for row in ledger] == ["script_review"] and ledger[0].user_id == env.user().id


def test_a_flag_from_the_model_lets_the_submission_through_and_the_categories_stay_on_the_version_only():
    env = Env(reply("flag", ["tier_mismatch"]))
    client = env.author()
    version = create(client)
    response = submit(client, version)
    assert response.status_code == 200 and response.get_json()["version"]["status"] == "pending_review"
    text = response.get_data(as_text=True)
    assert "tier_mismatch" not in text and "review_verdict" not in text and "review_categories" not in text
    (stored,) = env.rows(ScriptVersion)
    assert stored.review_verdict == "flag" and list(stored.review_categories_json) == ["tier_mismatch"]
    (record,) = env.rows(ScriptSubmission)
    assert record.outcome == "sent" and list(record.categories_json) == ["tier_mismatch"]


def test_flags_from_the_rules_layer_and_the_model_are_merged():
    env = Env(reply("flag", ["other_concern"]))
    client = env.author()
    version = create(client, content_tier="safe", background="兩人情慾難耐。")
    assert submit(client, version).status_code == 200
    (stored,) = env.rows(ScriptVersion)
    assert stored.review_verdict == "flag" and sorted(stored.review_categories_json) == ["other_concern", "tier_mismatch"]


def test_a_second_submission_of_the_same_unchanged_draft_after_withdrawing_uses_the_recorded_review():
    env = Env(reply())
    client = env.author()
    version = create(client)
    assert submit(client, version).status_code == 200
    assert client.post(f"/api/scripts/versions/{version['id']}/withdraw").status_code == 200
    assert submit(client, version).status_code == 200
    assert len(env.provider.calls) == 1 and outcomes(env) == ["sent", "sent"]


# ---- 檢查順序：在第一個不通過處停止，後面的步驟都不執行 -----------------------------------------------------------


def test_someone_elses_version_is_a_404_and_nothing_is_called_or_written():
    env = Env()
    owner, intruder = env.author(1), env.author(2)
    version = create(owner)
    response = submit(intruder, version)
    assert response.status_code == 404 and env.provider.calls == [] and env.rows(ScriptSubmission) == []
    assert submit(intruder, {"id": "00000000-0000-4000-8000-000000000000"}).get_data() == response.get_data()


def test_a_version_that_is_not_a_draft_is_refused_before_anything_else():
    env = Env(reply())
    client = env.author()
    version = create(client)
    assert submit(client, version).status_code == 200
    again = submit(client, version)
    assert again.status_code == 409 and again.get_json()["error"]["code"] == "not_draft"
    assert len(env.provider.calls) == 1 and outcomes(env) == ["sent"]


def test_a_suspended_author_is_refused_without_calling_the_model_or_writing_a_record():
    env = Env()
    client = env.author(publish_suspended_at=FakeClock()())
    version = create(client)
    response = submit(client, version)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "publish_suspended"
    assert env.provider.calls == [] and env.rows(ScriptSubmission) == []


def test_the_daily_limit_is_checked_before_the_rules_layer_and_the_model():
    env = Env(reply())
    env.limit("script_submit_daily.free", 1)
    client = env.author()
    first = create(client, title="一")
    assert submit(client, first).status_code == 200
    client.post(f"/api/scripts/versions/{first['id']}/withdraw")
    response = submit(client, first)
    assert response.status_code == 429 and response.get_json()["error"]["code"] == "submission_limit"
    assert int(response.headers["Retry-After"]) > 0
    assert len(env.provider.calls) == 1 and outcomes(env) == ["sent"]


def test_an_explicit_red_line_in_a_stored_draft_is_rejected_by_the_rules_layer_without_calling_the_model():
    env = Env()
    client = env.author()
    version_id = insert_unchecked_draft(env, background="十歲的少女與他做愛。" + UNIQUE)
    response = client.post(f"/api/scripts/versions/{version_id}/submit")
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "script_auto_rejected" and response.get_json()["categories"] == ["minor_sexual"]
    body = response.get_data(as_text=True)
    assert UNIQUE not in body and "做愛" not in body
    assert env.provider.calls == [] and outcomes(env) == ["auto_rejected"]
    (record,) = env.rows(ScriptSubmission)
    assert list(record.categories_json) == ["minor_sexual"]
    (stored,) = env.rows(ScriptVersion)
    assert stored.status == "draft" and stored.review_verdict is None


def test_a_model_rejection_is_recorded_even_though_the_response_is_a_4xx():
    env = Env(reply("reject", ["non_consensual"]))
    client = env.author()
    version = create(client)
    response = submit(client, version)
    assert response.status_code == 422 and response.get_json()["categories"] == ["non_consensual"]
    assert outcomes(env) == ["auto_rejected"]                                   # 4xx 也保留（persist）
    (stored,) = env.rows(ScriptVersion)
    assert stored.status == "draft" and stored.review_verdict == "reject"


def test_the_submission_record_has_only_the_user_the_outcome_the_categories_and_the_time():
    env = Env(reply("reject", ["minor_sexual", "real_person_sexual"]))
    client = env.author()
    submit(client, create(client, title="獨特標題SUBMIT-TITLE", rules=UNIQUE))
    (record,) = env.rows(ScriptSubmission)
    assert {column.name for column in ScriptSubmission.__table__.columns} == {"id", "user_id", "outcome", "categories_json", "created_at"}
    dumped = json.dumps([record.user_id, record.outcome, record.categories_json, str(record.created_at)], ensure_ascii=False)
    assert "SUBMIT" not in dumped and UNIQUE not in dumped


# ---- 模型層不可用：一律擋下，不計次 --------------------------------------------------------------------------------


def test_when_the_model_layer_is_unavailable_nothing_is_written_and_nothing_is_counted():
    env = Env(Step(LLMTransientError("timeout")), Step(LLMTransientError("server_error")))
    env.limit("script_submit_daily.free", 1)
    client = env.author()
    version = create(client)
    response = submit(client, version)
    assert response.status_code == 503 and response.get_json()["error"]["code"] == "review_unavailable"
    assert env.rows(ScriptSubmission) == []
    (stored,) = env.rows(ScriptVersion)
    assert stored.status == "draft" and stored.review_verdict is None
    env.provider.script(reply())
    assert submit(client, version).status_code == 200                              # 每日上限 1：不可用那次沒有計入
    assert outcomes(env) == ["sent"]


def test_without_an_rpg_provider_the_submission_is_refused_the_same_way():
    clock = FakeClock()
    app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=clock)
    Base.metadata.create_all(app.extensions["db_engine"])
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, 1, nickname="作者", nickname_code="AB01")
        uid, tv = user.id, user.token_version
    client = app.test_client()
    sign_in_as(client, uid, tv)
    version = create(client)
    assert submit(client, version).status_code == 503


# ---- 每日次數與台灣日界 ----------------------------------------------------------------------------------------------


def test_the_daily_limit_counts_sent_and_rejected_submissions_and_resets_at_midnight_in_taipei():
    env = Env(reply(), reply("reject", ["non_consensual"]), reply(), reply())
    env.limit("script_submit_daily.free", 2)
    client = env.author()
    env.clock.advance(hours=3, minutes=59)                                         # 台灣時間 23:59
    first = create(client, title="一")
    second = create(client, title="二")
    third = create(client, title="三")
    assert submit(client, first).status_code == 200
    assert submit(client, second).status_code == 422                               # 被拒絕也算一次
    blocked = submit(client, third)
    assert blocked.status_code == 429 and 0 < int(blocked.headers["Retry-After"]) <= 61
    env.clock.advance(minutes=2)                                                   # 台灣時間 00:01，新的一天
    assert submit(client, third).status_code == 200


def test_the_limit_depends_on_the_members_tier():
    env = Env(reply(), reply(), reply())
    env.limit("script_submit_daily.free", 1)
    env.limit("script_submit_daily.vip", 2)
    free, vip = env.author(1), env.author(2, member_tier="vip")
    assert submit(free, create(free, title="一")).status_code == 200
    assert submit(free, create(free, title="二")).status_code == 429
    assert submit(vip, create(vip, title="一")).status_code == 200
    assert submit(vip, create(vip, title="二")).status_code == 200


# ---- 連續被拒與暫停發佈 ------------------------------------------------------------------------------------------------


def test_three_consecutive_automatic_rejections_suspend_publishing_and_write_one_system_audit_row():
    env = Env(reply("reject", ["non_consensual"]))
    client = env.author()
    version = create(client)
    codes = [submit(client, version).status_code for _ in range(3)]
    assert codes == [422, 422, 422] and outcomes(env) == ["auto_rejected"] * 3
    assert env.user().publish_suspended_at is not None
    audits = [row for row in env.rows(AdminAuditLog) if row.action == "publish.suspend"]
    assert len(audits) == 1 and audits[0].admin_user_id is None
    assert audits[0].target_user_public_id == env.user().public_id and audits[0].detail_json == {"streak": 3}
    blocked = submit(client, version)
    assert blocked.status_code == 403 and blocked.get_json()["error"]["code"] == "publish_suspended"
    assert len(env.provider.calls) == 1                                           # 第二、三次用記錄的審查結果，沒有再呼叫模型


def test_the_threshold_comes_from_the_database_limit():
    env = Env(reply("reject", ["non_consensual"]))
    env.limit("script_reject_streak", 2)
    client = env.author()
    version = create(client)
    submit(client, version)
    assert env.user().publish_suspended_at is None
    submit(client, version)
    assert env.user().publish_suspended_at is not None


def test_a_successful_submission_breaks_the_streak():
    env = Env(reply("reject", ["non_consensual"]), reply(), reply("reject", ["non_consensual"]))
    env.limit("script_submit_daily.free", 10)
    client = env.author()
    version = create(client, title="一")
    submit(client, version)                                                         # 拒絕
    submit(client, version)                                                         # 拒絕（記錄的結果）
    edited = edit(client, version, title="二")
    assert submit(client, edited).status_code == 200                                 # 通過：連續中斷
    client.post(f"/api/scripts/versions/{edited['id']}/withdraw")
    again = edit(client, {**edited, "content_hash": edited["content_hash"]}, title="三")
    assert submit(client, again).status_code == 422                                  # 又一次拒絕，但前面是 sent
    assert env.user().publish_suspended_at is None and outcomes(env) == ["auto_rejected", "auto_rejected", "sent", "auto_rejected"]


def test_an_administrator_is_never_suspended():
    env = Env(reply("reject", ["non_consensual"]))
    env.limit("script_submit_daily.free", 10)
    admin = env.author(role="admin")
    version = create(admin)
    for _ in range(4):
        assert submit(admin, version).status_code == 422
    assert env.user().publish_suspended_at is None
    assert [row for row in env.rows(AdminAuditLog) if row.action == "publish.suspend"] == []


def test_suspending_is_a_conditional_update_so_only_one_caller_wins(member_session):
    user = add_user(member_session, 1)
    repo = UserRepository(member_session)
    now = FakeClock()()
    assert repo.suspend_publish_if_clear(user, now) is True
    assert repo.suspend_publish_if_clear(user, now + timedelta(minutes=1)) is False
    member_session.refresh(user)
    assert user.publish_suspended_at is not None
    admin = add_user(member_session, 2, role="admin")
    assert repo.suspend_publish_if_clear(admin, now) is False


def test_a_suspended_author_can_still_edit_log_in_and_see_the_state():
    env = Env()
    client = env.author(publish_suspended_at=FakeClock()())
    version = create(client)
    assert edit(client, version, title="暫停中照常編輯")["title"] == "暫停中照常編輯"
    assert client.get("/api/auth/me").get_json()["user"]["publish_suspended"] is True
    assert client.get("/api/scripts/limits").get_json()["limits"]["publish_suspended"] is True


def test_the_automatic_suspension_does_not_touch_other_accounts():
    env = Env(default=reply("reject", ["non_consensual"]))
    one, two = env.author(1), env.author(2)
    version = create(one)
    for _ in range(3):
        submit(one, version)
    assert env.user(1).publish_suspended_at is not None and env.user(2).publish_suspended_at is None
    assert submit(two, create(two)).status_code in (200, 422)


# ---- 日誌 --------------------------------------------------------------------------------------------------------------------


def test_the_submission_flow_leaves_no_script_text_and_only_enumerated_event_fields(log_lines):
    env = Env(reply("reject", ["non_consensual"]))
    client = env.author()
    version = create(client, rules=UNIQUE, title="獨特標題SUBMIT-TITLE")
    for _ in range(3):
        submit(client, version)
    raw = log_lines.raw()
    assert "SUBMIT-SECRET-TEXT" not in raw and "SUBMIT-TITLE" not in raw and "作者1" not in raw
    events = [line for line in log_lines() if line.get("event", "").startswith("script.")]
    names = {event["event"] for event in events}
    assert {"script.created", "script.auto_rejected", "script.publish_suspended", "script.review"} <= names
    allowed = {"time", "level", "logger", "message", "request_id", "event", "user_id", "count", "kind", "outcome", "reason", "model", "attempt"}
    assert all(set(event) <= allowed for event in events)
