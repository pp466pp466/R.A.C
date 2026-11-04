"""lockout-appeals 規格（rpg-script-authoring 擴充）：被暫停發佈的玩家也能使用申訴頁與申訴 API，管理員的申訴清單涵蓋鎖定與暫停發佈兩種。

共用同一套上限（內容 1000 字、每小時 3 則、同時 1 則待處理）；沒有被鎖定、也沒有被暫停發佈的人（含管理員）得到與不存在相同的 404；
管理員只看得到這兩種帳號送出的申訴，仍不能讀任何對話；申訴內容不進稽核與日誌；兩種狀態都解除後申訴不再出現在管理員的檢視。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, LockAppeal, User
from app.repositories.lock_appeal_repository import LockAppealRepository
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user
from tests.support.idor import logged_in_client

SECRET = "只該出現在申訴裡的私密內容SECRET-SUSPENDED-APPEAL"
STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)


class World:
    def __init__(self):
        self.clock = FakeClock()
        self.app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=self.clock)
        self.engine = self.app.extensions["db_engine"]
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            self.admin = self._ref(add_user(s, 1, role="admin"))
            self.suspended = self._ref(add_user(s, 2, publish_suspended_at=START))
            self.other_suspended = self._ref(add_user(s, 3, publish_suspended_at=START))
            self.locked = self._ref(add_user(s, 4, locked_at=START, lock_reason="rate_limited"))
            self.both = self._ref(add_user(s, 5, locked_at=START, lock_reason="rate_limited", publish_suspended_at=START))
            self.free = self._ref(add_user(s, 6))

    @staticmethod
    def _ref(user: User):
        return SimpleNamespace(id=user.id, token_version=user.token_version, public_id=user.public_id)

    def client(self, who):
        if who == "anonymous":
            return self.app.test_client()
        return logged_in_client(self.app, getattr(self, who))

    def post(self, who, content):
        return self.client(who).post("/api/appeals", json={"content": content})

    def seed(self, who, *, minutes_ago, handled=False, content="之前的申訴"):
        with Session(self.engine) as s:
            appeal = LockAppealRepository(s).create(getattr(self, who).id, content, self.clock() - timedelta(minutes=minutes_ago))
            if handled:
                appeal.handled_at = self.clock()
            s.commit()

    def set_columns(self, who, **values):
        with Session(self.engine) as s:
            row = s.get(User, getattr(self, who).id)
            for name, value in values.items():
                setattr(row, name, value)
            s.commit()

    def appeals(self):
        with Session(self.engine) as s:
            return list(s.scalars(select(LockAppeal).order_by(LockAppeal.id)))

    def admin_appeals(self):
        response = self.client("admin").get("/api/admin/appeals")
        assert response.status_code == 200
        return response.get_json()["appeals"]


@pytest.fixture()
def world():
    return World()


# ---- 被暫停發佈的玩家可以申訴 --------------------------------------------------------------------------


def test_a_suspended_player_submits_an_appeal_and_reads_it_back(world):
    response = world.post("suspended", "我沒有違規，請協助確認。")
    assert response.status_code == 201
    appeal = response.get_json()["appeal"]
    assert set(appeal) == {"id", "content", "status", "created_at"} and appeal["status"] == "pending"
    mine = world.client("suspended").get("/api/appeals/mine").get_json()["appeals"]
    assert [item["id"] for item in mine] == [appeal["id"]]
    (stored,) = world.appeals()
    assert stored.user_id == world.suspended.id and stored.handled_at is None


def test_the_same_limits_apply_to_a_suspended_player(world):
    assert world.post("suspended", "第一則").status_code == 201
    pending = world.post("suspended", "第二則")
    assert pending.status_code == 409 and pending.get_json()["error"]["code"] == "appeal_pending"
    assert world.post("other_suspended", "另一位的").status_code == 201                       # 上限以帳號計，互不影響
    too_long = world.post("suspended", "字" * 1001)
    assert too_long.status_code == 400 and set(too_long.get_json()["error"]["fields"]) == {"content"}
    assert len(world.appeals()) == 2


def test_the_fourth_appeal_within_an_hour_is_a_429_even_when_earlier_ones_are_handled(world):
    for minutes in (50, 40, 30):
        world.seed("suspended", minutes_ago=minutes, handled=True)
    response = world.post("suspended", "第四則")
    assert response.status_code == 429 and response.get_json()["error"]["code"] == "appeal_rate_limited"
    assert len(world.appeals()) == 3


def test_a_player_who_is_neither_locked_nor_suspended_gets_the_same_404_as_a_missing_resource(world):
    for who in ("free", "admin"):
        for response in (world.post(who, "我沒有被限制"), world.client(who).get("/api/appeals/mine")):
            assert response.status_code == 404 and response.get_json()["error"]["code"] == "not_found", who
    assert world.appeals() == []


def test_once_the_suspension_is_lifted_the_player_gets_the_404_again(world):
    assert world.post("suspended", "請解除").status_code == 201
    world.set_columns("suspended", publish_suspended_at=None)
    assert world.post("suspended", "再申訴一次").status_code == 404
    assert world.client("suspended").get("/api/appeals/mine").status_code == 404


def test_signed_out_requests_are_401(world):
    assert world.post("anonymous", "x").status_code == 401
    assert world.client("anonymous").get("/api/appeals/mine").status_code == 401


# ---- /appeal 頁面 -----------------------------------------------------------------------------------------


def state_of(response) -> dict:
    return json.loads(STATE_PATTERN.search(response.get_data(as_text=True)).group(1))


def test_a_suspended_player_opens_the_appeal_page_with_the_suspended_kind_and_nothing_personal(world):
    response = world.client("suspended").get("/appeal")
    assert response.status_code == 200
    state = state_of(response)
    assert state["appeal"] == {"kind": "suspended", "maxChars": 1000, "hourlyLimit": 3} and "user" not in state
    html = response.get_data(as_text=True)
    assert "user2@example.com" not in html and "送審權限目前被暫停" in html


def test_an_account_that_is_both_locked_and_suspended_is_treated_as_locked(world):
    response = world.client("both").get("/appeal")
    assert response.status_code == 200 and state_of(response)["appeal"]["kind"] == "locked"


def test_a_player_with_no_restriction_is_sent_to_the_default_page(world):
    for who in ("free", "admin"):
        response = world.client(who).get("/appeal")
        assert response.status_code == 302 and not response.headers["Location"].startswith("/appeal"), who


def test_the_appeal_page_explains_both_situations_in_plain_text():
    from app.config import PROJECT_ROOT

    html = (PROJECT_ROOT / "app" / "templates" / "auth" / "appeal.html").read_text(encoding="utf-8-sig")
    assert "您的帳號目前被鎖定" in html and "您的送審權限目前被暫停" in html
    assert 'v-if="isSuspended"' in html and "v-html" not in html and "data-bs-" not in html
    script = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "appeal.js").read_text(encoding="utf-8-sig")
    assert "limits.kind === 'suspended'" in script and "isSuspended: isSuspended" in script


# ---- 管理員的檢視 -------------------------------------------------------------------------------------------


def test_the_admin_sees_appeals_of_locked_and_suspended_accounts_and_says_which_one(world):
    world.seed("locked", minutes_ago=5, content="鎖定者的申訴")
    world.seed("suspended", minutes_ago=4, content="暫停者的申訴")
    world.seed("both", minutes_ago=3, content="兩者的申訴")
    world.seed("free", minutes_ago=2, content="沒有被限制的人")
    rows = {row["content"]: row for row in world.admin_appeals()}
    assert set(rows) == {"鎖定者的申訴", "暫停者的申訴", "兩者的申訴"}
    assert rows["鎖定者的申訴"]["restriction"] == "locked"
    assert rows["暫停者的申訴"]["restriction"] == "suspended"
    assert rows["兩者的申訴"]["restriction"] == "locked"                                  # 鎖定優先
    assert set(rows["暫停者的申訴"]) == {"id", "user_public_id", "display_name", "content", "created_at", "status", "restriction"}


def test_the_appeals_disappear_from_the_admin_view_only_when_every_restriction_is_gone(world):
    world.seed("both", minutes_ago=3, content="兩者的申訴")
    world.set_columns("both", locked_at=None, lock_reason=None)
    assert [row["restriction"] for row in world.admin_appeals()] == ["suspended"]          # 仍被暫停：還看得到
    world.set_columns("both", publish_suspended_at=None)
    assert world.admin_appeals() == []
    assert len(world.appeals()) == 1                                                        # 資料仍隨帳號保存


def test_lifting_the_suspension_through_the_admin_api_marks_the_appeal_handled_and_hides_it(world):
    world.seed("suspended", minutes_ago=5, content="請解除暫停")
    response = world.client("admin").post(
        f"/api/admin/users/{world.suspended.public_id}/lift-publish-suspension", json={"password": GOOD_PASSWORD}
    )
    assert response.status_code == 200
    assert all(appeal.handled_at is not None for appeal in world.appeals())
    assert world.admin_appeals() == []
    assert world.post("suspended", "再申訴").status_code == 404


def test_a_regular_member_cannot_read_the_admin_appeal_list(world):
    world.seed("suspended", minutes_ago=5)
    assert world.client("suspended").get("/api/admin/appeals").status_code == 404
    assert world.client("anonymous").get("/api/admin/appeals").status_code == 401


def test_the_admin_endpoint_exposes_no_conversation_or_personal_data(world):
    world.seed("suspended", minutes_ago=5, content="內容")
    text = json.dumps(world.admin_appeals(), ensure_ascii=False)
    assert "user2@example.com" not in text and "birth" not in text.lower() and "conversation" not in text.lower()


def test_reading_the_appeals_audits_only_the_count(world):
    world.seed("suspended", minutes_ago=5, content=SECRET)
    world.admin_appeals()
    with Session(world.engine) as s:
        (audit,) = s.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "appeal.read"))
        assert audit.detail_json == {"count": 1} and SECRET not in repr((audit.detail_json, audit.reason))


# ---- 個資不進稽核與日誌 ---------------------------------------------------------------------------------------


def test_a_suspended_players_appeal_text_never_reaches_the_audit_rows_or_the_logs(world, log_lines):
    assert world.post("suspended", SECRET).status_code == 201
    world.post("suspended", SECRET + "第二則")                                              # 409
    world.post("free", SECRET)                                                              # 404
    world.admin_appeals()
    with Session(world.engine) as s:
        rows = list(s.scalars(select(AdminAuditLog).where(AdminAuditLog.action.in_(("appeal.submit", "appeal.read")))))
        assert {row.action for row in rows} == {"appeal.submit", "appeal.read"}
        for row in rows:
            assert SECRET not in repr((row.detail_json, row.reason, row.action, row.target_user_public_id))
        submit = next(row for row in rows if row.action == "appeal.submit")
        assert submit.detail_json == {"length": len(SECRET)} and submit.admin_user_id is None
    assert SECRET not in log_lines.raw()
