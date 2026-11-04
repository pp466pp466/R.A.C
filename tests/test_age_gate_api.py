"""age-confirmation 與 access-control 規格：年齡閘門的 HTTP 行為。

目前還沒有任何會呼叫 Gemini 的正式端點（客服是 C-3），所以用一個「只在測試應用程式中註冊」的端點代表它，
寫法與 C-3 的 AI 端點應有的寫法相同：先 `require_login()`，再 `require_chat_access(user)`，之後才做事。
這個端點也回報 `content_access`（偏好、年齡上限、生效模式），證明內容模式的上限在 HTTP 層同樣由當下的年齡決定。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.models.user import ROLE_ADMIN, ROLE_USER
from app.services.age_policy import AgePolicy
from tests.support.age import age_fields, birth_date_for_age
from tests.support.age_gate import ENDPOINT, add_ai_endpoint
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user, sign_in_as


def make_app(*, age_policy: AgePolicy | None = None, environ: dict | None = None, **kwargs):
    config = load_config(environ={"APP_ENV": "testing", **(environ or {})})
    application = create_app(config, clock=FakeClock(), age_policy=age_policy, **kwargs)
    Base.metadata.create_all(application.extensions["db_engine"])
    add_ai_endpoint(application)
    return application


@pytest.fixture()
def app():
    return make_app()


def seed(app, n: int, **overrides):
    with Session(app.extensions["db_engine"]) as session:
        return add_user(session, n, **overrides).id


def signed_in(app, n: int):
    """直接寫入登入狀態（管理員不能只靠密碼登入，所以不經過登入端點）。"""
    with Session(app.extensions["db_engine"]) as session:
        user_id = session.scalar(select(User.id).where(User.email == f"user{n}@example.com"))
    client = app.test_client()
    sign_in_as(client, user_id)
    return client


def verify(client, age: int = 30):
    assert client.post("/api/auth/verify-age", json=age_fields(age)).status_code == 200


# =============================================================================
# 尚未完成：403 與固定代碼；補填後同一個端點放行
# =============================================================================

def test_a_member_without_age_data_gets_403_with_the_dedicated_code_and_the_work_never_runs(app):
    seed(app, 1, age=None)
    response = signed_in(app, 1).post(ENDPOINT, json={})
    assert response.status_code == 403
    error = response.get_json()["error"]
    assert error["code"] == "age_confirmation_required"
    assert "生日" in error["message"] and "年齡確認" in error["message"]
    assert app.work_done == []


def test_verifying_later_opens_the_same_endpoint_without_logging_in_again(app):
    seed(app, 1, age=None)
    client = signed_in(app, 1)
    assert client.post(ENDPOINT, json={}).status_code == 403
    verify(client, 30)
    response = client.post(ENDPOINT, json={})
    assert response.status_code == 200 and response.get_json()["ok"] is True
    assert len(app.work_done) == 1


def test_a_member_who_fills_in_the_age_after_signing_in_passes_straight_away(app):
    seed(app, 1, age=None)
    client = signed_in(app, 1)
    verify(client, 30)
    assert client.post(ENDPOINT, json={}).status_code == 200


def test_being_signed_out_is_still_a_401_not_an_age_error(app):
    response = app.test_client().post(ENDPOINT, json={})
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


# =============================================================================
# 管理員與各會員等級都不豁免
# =============================================================================

@pytest.mark.parametrize(("role", "tier"), [(ROLE_USER, "free"), (ROLE_USER, "vip"), (ROLE_ADMIN, "free"), (ROLE_ADMIN, "vip")])
def test_neither_admins_nor_vip_members_skip_the_age_gate(app, role, tier):
    seed(app, 1, role=role, member_tier=tier, age=None)
    client = signed_in(app, 1)
    response = client.post(ENDPOINT, json={})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"
    verify(client, 30)
    assert client.post(ENDPOINT, json={}).status_code == 200


def test_verifying_one_member_does_not_open_the_gate_for_another(app):
    seed(app, 1, age=None)
    seed(app, 2, age=None)
    verify(signed_in(app, 1), 30)
    assert signed_in(app, 2).post(ENDPOINT, json={}).status_code == 403
    assert app.work_done == []


# =============================================================================
# 13 至 17 歲可用但固定安全模式；成年者上限為一般模式（成人模式預設關閉）
# =============================================================================

@pytest.mark.parametrize("age", [13, 15, 17])
def test_a_teen_can_use_the_endpoint_but_only_in_safe_mode(app, age):
    seed(app, 1, age=age)
    response = signed_in(app, 1).post(ENDPOINT, json={})
    assert response.status_code == 200
    assert response.get_json()["content"] == {"preference": "safe", "age_cap": "safe", "effective": "safe", "reason": "not_adult"}


@pytest.mark.parametrize("age", [18, 30, 70])
def test_an_adult_reaches_the_standard_mode_and_never_the_adult_mode_by_default(app, age):
    seed(app, 1, age=age)
    assert signed_in(app, 1).post(ENDPOINT, json={}).get_json()["content"] == {
        "preference": "standard", "age_cap": "standard", "effective": "standard", "reason": "disabled",
    }


def test_an_admin_who_is_a_teen_is_still_in_safe_mode(app):
    seed(app, 1, age=15, role=ROLE_ADMIN)
    assert signed_in(app, 1).post(ENDPOINT, json={}).get_json()["content"]["effective"] == "safe"


def test_a_teen_who_turns_18_is_released_without_logging_in_again():
    clock = FakeClock()
    config = load_config(environ={"APP_ENV": "testing"})
    application = create_app(config, clock=clock)
    Base.metadata.create_all(application.extensions["db_engine"])
    add_ai_endpoint(application)
    with Session(application.extensions["db_engine"]) as session:
        tomorrow_18 = birth_date_for_age(18).replace(day=birth_date_for_age(18).day + 1)
        add_user(session, 1, age=17, birth_date=tomorrow_18)
    client = signed_in(application, 1)
    assert client.post(ENDPOINT, json={}).get_json()["content"]["age_cap"] == "safe"
    clock.advance(days=1)
    released = client.post(ENDPOINT, json={}).get_json()["content"]
    # 年齡上限自動解除，但偏好不會自動調高：確認當時未成年，所以仍是安全模式
    assert released == {"preference": "safe", "age_cap": "standard", "effective": "safe", "reason": "disabled"}


# =============================================================================
# 年齡規則被提高：只改規則，其他不動
# =============================================================================

def test_raising_the_minimum_age_to_18_blocks_existing_teens_with_their_own_code_and_leaves_adults_alone():
    strict = make_app(age_policy=AgePolicy(min_register_age=18, min_use_age=18, version="test-18-only"))
    seed(strict, 1, age=15)
    adult_id = seed(strict, 2, age=40)
    teen = signed_in(strict, 1).post(ENDPOINT, json={})
    assert teen.status_code == 403 and teen.get_json()["error"]["code"] == "age_restricted"
    assert "帳號設定" not in teen.get_json()["error"]["message"]                  # 補填也沒有用，所以不引導
    assert signed_in(strict, 2).post(ENDPOINT, json={}).status_code == 200
    assert strict.work_done == [adult_id]                                        # 只有成年者的請求真的做了事
    seed(strict, 3, age=None)                                                   # 新的玩家補填 15 歲：低於最低註冊年齡，帳號被抹除
    refused = signed_in(strict, 3).post("/api/auth/verify-age", json=age_fields(15))
    assert refused.status_code == 400 and refused.get_json()["error"]["code"] == "age_not_eligible"


# =============================================================================
# 日誌
# =============================================================================

# 沒有年齡資料的玩家在登入檢查就被擋下（受限登入狀態，不經過 AccessPolicy），所以記錄拒絕事件的是管理員這一條路
@pytest.mark.parametrize(
    ("age", "role", "code"), [(None, ROLE_ADMIN, "age_confirmation_required"), (12, ROLE_USER, "age_restricted")]
)
def test_a_denial_is_logged_with_the_internal_user_id_and_nothing_personal(app, log_lines, age, role, code):
    user_id = seed(app, 1, age=age, role=role)
    response = signed_in(app, 1).post(ENDPOINT, json={})
    assert response.get_json()["error"]["code"] == code
    denials = [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"]
    assert len(denials) == 1 and denials[0]["user_id"] == user_id and denials[0]["level"] == "INFO"
    raw = log_lines.raw()
    assert "user1@example.com" not in raw and GOOD_PASSWORD not in raw
    assert birth_date_for_age(12).isoformat() not in raw and "birth" not in raw


def test_an_allowed_request_leaves_no_denial_in_the_log(app, log_lines):
    seed(app, 1, age=30)
    assert signed_in(app, 1).post(ENDPOINT, json={}).status_code == 200
    assert [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"] == []


# =============================================================================
# 信箱不是閘門：驗證時間為空的帳號直接被引導補填年齡
# =============================================================================

def test_an_account_without_a_verification_time_is_asked_to_fill_in_the_age_not_refused_for_the_email(log_lines):
    """信箱驗證已退役：玩家的信箱由 Google 驗證，遺留的驗證時間為空的帳號也不被信箱條件擋下。
    （玩家沒有生日時在登入檢查就被導向年齡確認頁，所以用不受該受限狀態影響的管理員代表。）"""
    app = make_app()
    seed(app, 1, role=ROLE_ADMIN, age=None)

    response = signed_in(app, 1).post(ENDPOINT, json={})

    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"
    assert app.work_done == []
    assert [line["event"] for line in log_lines() if line.get("event") == "auth.age_gate_denied"] == ["auth.age_gate_denied"]
