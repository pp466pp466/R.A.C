"""script-submission 規格「管理員檢視與解除暫停發佈」：`GET /api/admin/publish-suspended` 與
`POST /api/admin/users/<public_id>/lift-publish-suspension`——身分矩陣、重新輸入密碼、條件式解除、`lifted` 紀錄使連續被拒中斷、
待處理的申訴標為已處理、稽核與事件、回應不含 email 與暱稱。
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, LockAppeal, ScriptSubmission, User
from app.repositories.lock_appeal_repository import LockAppealRepository
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user
from tests.support.client import raw_client
from tests.support.idor import logged_in_client
from tests.support.llm import FakeLLMProvider


class World:
    def __init__(self):
        self.clock = FakeClock()
        self.provider = FakeLLMProvider(default=None)
        self.app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=self.clock, rpg_llm_provider=self.provider)
        self.engine = self.app.extensions["db_engine"]
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            self.admin = self._ref(add_user(s, 1, role="admin"))
            self.suspended = self._ref(add_user(s, 2, nickname="暫停者暱稱", nickname_code="ZZ99", publish_suspended_at=START))
            self.other_suspended = self._ref(add_user(s, 3, publish_suspended_at=START))
            self.normal = self._ref(add_user(s, 4))
            self.member = self._ref(add_user(s, 5))
            LockAppealRepository(s).create(self.suspended.id, "我不是故意的", START)
            s.commit()

    @staticmethod
    def _ref(user: User):
        return SimpleNamespace(id=user.id, token_version=user.token_version, public_id=user.public_id)

    def client(self, who):
        if who == "anonymous":
            return self.app.test_client()
        return logged_in_client(self.app, getattr(self, who))

    def row(self, ref) -> User:
        with Session(self.engine) as s:
            return s.get(User, ref.id)

    def all(self, model):
        with Session(self.engine) as s:
            return list(s.scalars(select(model).order_by(model.id)))

    def lift(self, who, target, password=GOOD_PASSWORD):
        return self.client(who).post(f"/api/admin/users/{target.public_id}/lift-publish-suspension", json={"password": password})


@pytest.fixture()
def world():
    return World()


# ---- 清單 ------------------------------------------------------------------------------------------------


def test_the_admin_sees_the_suspended_accounts_with_only_public_id_name_and_time(world):
    response = world.client("admin").get("/api/admin/publish-suspended")
    assert response.status_code == 200
    accounts = response.get_json()["accounts"]
    assert {a["public_id"] for a in accounts} == {world.suspended.public_id, world.other_suspended.public_id}
    assert all(set(a) == {"public_id", "display_name", "suspended_at"} for a in accounts)
    text = response.get_data(as_text=True)
    assert "暫停者暱稱" not in text and "ZZ99" not in text and "@example.com" not in text and "nickname" not in text
    assert "no-store" in response.headers["Cache-Control"]


def test_the_list_does_not_include_accounts_that_are_not_suspended(world):
    accounts = world.client("admin").get("/api/admin/publish-suspended").get_json()["accounts"]
    assert world.normal.public_id not in {a["public_id"] for a in accounts}


def test_the_list_is_paged(world):
    page = world.client("admin").get("/api/admin/publish-suspended?limit=1&offset=1")
    assert page.status_code == 200 and len(page.get_json()["accounts"]) == 1


# ---- 身分矩陣：未登入 401，非管理員與不存在的端點逐字相同的 404 ----------------------------------------------


@pytest.mark.parametrize("method, path", [("GET", "/api/admin/publish-suspended"), ("POST", "/api/admin/users/{}/lift-publish-suspension")])
def test_an_anonymous_visitor_gets_401(world, method, path):
    response = world.client("anonymous").open(path.format(world.suspended.public_id), method=method, json={"password": GOOD_PASSWORD})
    assert response.status_code == 401


@pytest.mark.parametrize("method, path", [("GET", "/api/admin/publish-suspended"), ("POST", "/api/admin/users/{}/lift-publish-suspension")])
def test_a_non_admin_gets_the_same_404_as_a_missing_endpoint_and_nothing_changes(world, method, path):
    response = world.client("member").open(path.format(world.suspended.public_id), method=method, json={"password": GOOD_PASSWORD})
    missing = world.client("member").get("/api/admin/this-endpoint-does-not-exist")
    assert response.status_code == 404 and response.get_json() == missing.get_json()
    assert world.row(world.suspended).publish_suspended_at is not None and world.all(AdminAuditLog) == []


def test_a_post_without_the_csrf_token_is_refused(world):
    raw = raw_client(world.app)
    with raw.session_transaction() as session:
        session["uid"], session["tv"] = world.admin.id, world.admin.token_version
    response = raw.post(f"/api/admin/users/{world.suspended.public_id}/lift-publish-suspension", json={"password": GOOD_PASSWORD})
    assert response.status_code == 403 and world.row(world.suspended).publish_suspended_at is not None


# ---- 解除 ------------------------------------------------------------------------------------------------------


def test_lifting_needs_the_admins_password(world):
    wrong = world.lift("admin", world.suspended, password="definitely-not-the-password")
    assert wrong.status_code == 400 and "password" in wrong.get_json()["error"]["fields"]
    assert world.row(world.suspended).publish_suspended_at is not None
    missing = world.client("admin").post(f"/api/admin/users/{world.suspended.public_id}/lift-publish-suspension", json={})
    assert missing.status_code == 400 and world.row(world.suspended).publish_suspended_at is not None
    assert [row for row in world.all(AdminAuditLog) if row.action == "publish.lift"] == [] and world.all(ScriptSubmission) == []


def test_lifting_clears_the_flag_writes_one_lifted_record_marks_appeals_handled_and_audits(world):
    response = world.lift("admin", world.suspended)
    assert response.status_code == 200
    assert response.get_json()["user"] == {"public_id": world.suspended.public_id, "display_name": world.row(world.suspended).display_name, "publish_suspended": False}
    assert world.row(world.suspended).publish_suspended_at is None
    (record,) = world.all(ScriptSubmission)
    assert record.user_id == world.suspended.id and record.outcome == "lifted" and record.categories_json is None
    (appeal,) = world.all(LockAppeal)
    assert appeal.handled_at is not None
    audit = [row for row in world.all(AdminAuditLog) if row.action == "publish.lift"]
    assert len(audit) == 1 and audit[0].admin_user_id == world.admin.id and audit[0].target_user_public_id == world.suspended.public_id
    assert audit[0].detail_json == {"handled_appeals": 1}
    assert world.row(world.other_suspended).publish_suspended_at is not None


def test_lifting_an_account_that_is_not_suspended_is_a_409_without_an_audit_row(world):
    response = world.lift("admin", world.normal)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "not_suspended"
    assert [row for row in world.all(AdminAuditLog) if row.action == "publish.lift"] == [] and world.all(ScriptSubmission) == []


def test_a_second_lift_of_the_same_account_is_a_409_and_writes_no_second_record(world):
    assert world.lift("admin", world.suspended).status_code == 200
    again = world.lift("admin", world.suspended)
    assert again.status_code == 409 and len(world.all(ScriptSubmission)) == 1


def test_the_target_must_be_a_regular_user_and_must_exist(world):
    admin_target = world.lift("admin", world.admin)
    assert admin_target.status_code == 403 and admin_target.get_json()["error"]["code"] == "admin_target_forbidden"
    ghost = world.client("admin").post(
        "/api/admin/users/00000000-0000-4000-8000-000000000000/lift-publish-suspension", json={"password": GOOD_PASSWORD},
    )
    assert ghost.status_code == 404


def test_the_repository_lift_is_conditional_so_only_one_of_two_concurrent_lifts_wins(world):
    from app.repositories.admin_repository import AdminRepository

    with Session(world.engine) as s:
        repo, user = AdminRepository(s), s.get(User, world.suspended.id)
        assert repo.lift_publish_suspension(user, now=START) == 1
        assert repo.lift_publish_suspension(user, now=START) is None
        s.commit()
    assert len(world.all(ScriptSubmission)) == 1


# ---- 解除之後 ----------------------------------------------------------------------------------------------------


def test_after_lifting_the_streak_is_broken_so_one_more_rejection_does_not_suspend_again(world):
    """三次自動拒絕 -> 暫停 -> 解除 -> 再被拒絕一次：最近三筆是（拒絕、解除、拒絕）而不是三個拒絕，所以不再暫停。"""
    from app.models.script_submission import SUBMISSION_AUTO_REJECTED
    from app.repositories.script_submission_repository import ScriptSubmissionRepository

    with Session(world.engine) as s:
        repo = ScriptSubmissionRepository(s)
        for _ in range(3):
            repo.record(world.suspended.id, SUBMISSION_AUTO_REJECTED, ["non_consensual"], START)
        s.commit()
    assert world.lift("admin", world.suspended).status_code == 200
    with Session(world.engine) as s:
        repo = ScriptSubmissionRepository(s)
        repo.record(world.suspended.id, SUBMISSION_AUTO_REJECTED, ["non_consensual"], START)
        assert repo.recent_outcomes(world.suspended.id, 3) == ["auto_rejected", "lifted", "auto_rejected"]


def test_a_lifted_account_can_submit_again_end_to_end(world):
    from tests.support.llm import Step
    from tests.support.script_data import valid

    world.provider.script(Step(json.dumps({"verdict": "pass", "categories": []})))
    client = world.client("suspended")
    created = client.post("/api/scripts", json={"content": valid()})
    assert created.status_code == 201, created.get_data(as_text=True)
    version = created.get_json()["version"]
    assert client.post(f"/api/scripts/versions/{version['id']}/submit").status_code == 403
    assert world.lift("admin", world.suspended).status_code == 200
    assert client.post(f"/api/scripts/versions/{version['id']}/submit").status_code == 200


# ---- 日誌 ----------------------------------------------------------------------------------------------------------


def test_lifting_logs_only_the_event_name_and_the_internal_id(world, log_lines):
    world.lift("admin", world.suspended)
    events = [line for line in log_lines() if line.get("event") in ("script.publish_lifted", "admin.action")]
    assert {e["event"] for e in events} == {"script.publish_lifted", "admin.action"}
    raw = log_lines.raw()
    assert "暫停者暱稱" not in raw and "我不是故意的" not in raw and "@example.com" not in raw
    admin_action = next(e for e in events if e["event"] == "admin.action")
    assert admin_action["reason"] == "publish.lift"


# ---- 後台頁面 ---------------------------------------------------------------------------------------------------------


def test_the_console_has_a_suspended_accounts_section_with_a_password_confirmed_lift(world):
    from app.config import PROJECT_ROOT

    html = world.client("admin").get("/admin").get_data(as_text=True)
    assert "v-if=\"tab === 'suspended'\"" in html and "被暫停發佈的帳號" in html and "askLift(row.public_id, row.display_name)" in html
    script = (PROJECT_ROOT / "app" / "static" / "js" / "admin" / "admin.js").read_text(encoding="utf-8")
    assert "{ id: 'suspended', label: '被暫停發佈' }" in script and "path: '/publish-suspended'" in script
    block = script.split("lift: {", 1)[1].split("remove: {", 1)[0]
    assert "needsReason: false" in block and "/lift-publish-suspension" in block and "ADMIN_LIFT_PUBLISH_SUSPENSION" in block
    assert "confirmState.password" in script                                   # 與其他危險操作共用的重新輸入密碼流程
