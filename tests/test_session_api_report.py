"""`POST /api/sessions/catalog/<版本>/report`（rpg-script-reports 的 script-reports 規格）的 HTTP 層：成功、各種狀態碼與錯誤代碼、閘門、
CSRF 與 Origin、不可快取、回應沒有統計、不呼叫模型也不動錢包，以及檢舉後詳情的 `report_state`。

檢查順序與每日上限的細節（含 SQL Server 與並行）在 `test_script_report_service.py`；這裡用測試應用程式（SQLite）、直接寫入的登入狀態與假的 RPG
供應商（用來證明「沒有任何模型呼叫」）。
"""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import LlmCallLog, RuntimeLimit, ScriptReport, User, Wallet, WalletEntry
from app.models.script import VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REMOVED, VERSION_SUPERSEDED
from app.models.script_report import REPORT_REASONS
from app.models.user import ROLE_ADMIN, STATUS_DELETION_PENDING
from app.repositories.script_repository import ScriptRepository
from tests.support.auth import START, FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.llm import FakeLLMProvider
from tests.support.script_data import validated
from tests.support.script_review import put_in_status
from tests.test_llm_providers_by_purpose import BOTH, make_app

GHOST = "00000000-0000-4000-8000-000000000000"
SPECIAL_TEXT = "檢舉文字：<img src=x onerror=alert(1)> 'x'; DROP TABLE users -- 😀"
DETAIL = "/api/sessions/catalog/"


@pytest.fixture()
def provider():
    return FakeLLMProvider()


@pytest.fixture()
def app(provider):
    return make_app({**BOTH}, rpg_llm_provider=provider)


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def signed_in(app, n=1, **overrides):
    with db(app) as s:
        user = add_user(s, n, **overrides)
        uid, tv = user.id, user.token_version
    client = app.test_client()
    sign_in_as(client, uid, tv)
    return client


def player_script(app, owner_n=2, status=VERSION_PUBLISHED, **changes):
    """玩家（不是內建）的劇本放進指定狀態；回傳版本的對外識別碼。擁有者在這裡建立（作者不需要登入）。"""
    with db(app) as s:
        owner = s.scalar(select(User).where(User.email == f"user{owner_n}@example.com")) or add_user(s, owner_n)
        version = put_in_status(ScriptRepository(s), owner, status, **changes)
        s.commit()
        return version.public_id


def builtin(app, **changes):
    result = validated(**changes)
    with db(app) as s:
        version, _ = ScriptRepository(s).publish_builtin(
            "seed-report-http", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=FakeClock()(),
        )
        s.commit()
        return version.public_id


def count(app, model) -> int:
    with db(app) as s:
        return s.scalar(select(func.count()).select_from(model))


def report_path(version_id) -> str:
    return f"{DETAIL}{version_id}/report"


def report(client, version_id, reasons=("other",), text=None, **extra):
    body = {"reasons": list(reasons), **({} if text is None else {"text": text}), **extra}
    return client.post(report_path(version_id), json=body)


# ---- 成功 ---------------------------------------------------------------------------------------


def test_a_report_is_201_with_only_a_success_flag_and_nothing_is_unpublished(app):
    client = signed_in(app, 1)
    version = player_script(app)
    response = report(client, version, ["plagiarism", "other"], SPECIAL_TEXT)
    assert response.status_code == 201 and response.get_json() == {"ok": True}
    with db(app) as s:
        [stored] = s.scalars(select(ScriptReport)).all()
        assert stored.reasons_json == ["plagiarism", "other"] and stored.note == SPECIAL_TEXT and stored.closed_at is None
    listed = client.get("/api/sessions/catalog").get_json()["scripts"]
    assert [card["id"] for card in listed] == [version]                          # 檢舉不下架：仍在公開目錄


def test_the_response_never_carries_statistics_or_review_state(app):
    client = signed_in(app, 1)
    version = player_script(app)
    signed_in(app, 3)
    text = report(client, version).get_data(as_text=True)
    for word in ("count", "total", "report", "closed", "status", "reviewed", "number"):
        assert word not in text.lower().replace('"ok"', ""), word


def test_all_responses_are_not_cacheable(app):
    client = signed_in(app, 1)
    version = player_script(app)
    responses = [report(client, version), report(client, version), report(client, GHOST), report(client, version, [])]
    assert [r.status_code for r in responses] == [201, 409, 404, 400]
    assert [r.headers.get("Cache-Control") for r in responses] == ["no-store"] * 4


def test_reporting_calls_no_model_and_touches_no_wallet_or_ledger(app, provider):
    client = signed_in(app, 1)
    version = player_script(app)
    assert report(client, version, list(REPORT_REASONS), SPECIAL_TEXT).status_code == 201
    assert provider.calls == []
    assert (count(app, LlmCallLog), count(app, Wallet), count(app, WalletEntry)) == (0, 0, 0)


def test_the_detail_changes_to_reported_for_the_reporter_only(app):
    reporter, bystander = signed_in(app, 1), signed_in(app, 4)
    version = player_script(app)
    assert reporter.get(f"{DETAIL}{version}").get_json()["script"]["report_state"] == "available"
    assert report(reporter, version).status_code == 201
    assert reporter.get(f"{DETAIL}{version}").get_json()["script"]["report_state"] == "reported"
    assert bystander.get(f"{DETAIL}{version}").get_json()["script"]["report_state"] == "available"


def test_the_authors_detail_is_unavailable_and_the_authors_report_is_403(app):
    author = signed_in(app, 2)
    version = player_script(app, 2)
    assert author.get(f"{DETAIL}{version}").get_json()["script"]["report_state"] == "unavailable"
    response = report(author, version)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "report_own_script"
    assert count(app, ScriptReport) == 0


# ---- 登入、閘門、CSRF ---------------------------------------------------------------------------


def test_an_anonymous_request_is_401_and_creates_nothing(app):
    version = player_script(app)
    response = report(app.test_client(), version)
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"
    assert count(app, ScriptReport) == 0


def test_a_player_who_has_not_confirmed_their_age_is_403_age_confirmation_required(app):
    client = signed_in(app, 1, age=None)
    version = player_script(app)
    response = report(client, version)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"
    assert count(app, ScriptReport) == 0


def test_an_admin_who_has_not_confirmed_their_age_is_refused_by_the_service(app):
    client = signed_in(app, 1, age=None, role=ROLE_ADMIN)
    version = player_script(app)
    response = report(client, version)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"
    assert count(app, ScriptReport) == 0


def test_a_confirmed_admin_can_report(app):
    client = signed_in(app, 1, role=ROLE_ADMIN)
    assert report(client, player_script(app)).status_code == 201


def test_a_locked_account_is_refused_before_anything_else(app):
    client = signed_in(app, 1, locked_at=START, lock_reason="rate_limited")
    version = player_script(app)
    response = report(client, version)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_locked"
    assert count(app, ScriptReport) == 0


def test_an_account_waiting_for_deletion_is_refused(app):
    client = signed_in(app, 1, status=STATUS_DELETION_PENDING)
    response = report(client, player_script(app))
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_deletion_pending"
    assert count(app, ScriptReport) == 0


def test_a_post_without_the_csrf_token_is_refused_and_creates_nothing(app):
    version = player_script(app)
    with db(app) as s:
        user = add_user(s, 1)
        uid, tv = user.id, user.token_version
    client = raw_client(app)
    sign_in_as(client, uid, tv)
    response = client.post(report_path(version), json={"reasons": ["other"]}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403
    assert count(app, ScriptReport) == 0


def test_a_post_from_another_origin_is_refused_and_creates_nothing(app):
    client = signed_in(app, 1)
    version = player_script(app)
    response = client.post(report_path(version), json={"reasons": ["other"]}, headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403
    assert count(app, ScriptReport) == 0


def test_a_non_json_body_is_415_and_a_non_object_is_400(app):
    client = signed_in(app, 1)
    version = player_script(app)
    assert client.post(report_path(version), data="x", content_type="text/plain").status_code == 415
    assert client.post(report_path(version), json=["other"]).status_code == 400
    assert count(app, ScriptReport) == 0


# ---- 404：只有上架中的非內建版本 ------------------------------------------------------------------


@pytest.mark.parametrize("status", [VERSION_DRAFT, VERSION_PENDING, VERSION_SUPERSEDED, VERSION_REMOVED])
def test_a_version_that_is_not_published_is_404_identical_to_a_missing_one(app, status):
    client = signed_in(app, 1)
    version = player_script(app, 2, status)
    hidden, missing = report(client, version), report(client, GHOST)
    assert hidden.status_code == 404 and hidden.get_json() == missing.get_json()
    assert count(app, ScriptReport) == 0


def test_a_builtin_script_is_404_identical_to_a_missing_one(app):
    client = signed_in(app, 1)
    version = builtin(app)
    response, missing = report(client, version), report(client, GHOST)
    assert response.status_code == 404 and response.get_json() == missing.get_json()
    assert count(app, ScriptReport) == 0


@pytest.mark.parametrize("version_id", ["not-a-uuid", "x" * 300, "%27%3B%20DROP%20TABLE", "0"])
def test_a_malformed_id_is_404(app, version_id):
    client = signed_in(app, 1)
    assert report(client, version_id).status_code == 404


def test_the_404_comes_before_the_body_is_validated(app):
    """無效的識別碼不會洩漏欄位規則：本文再不合法，也先得到 404。"""
    client = signed_in(app, 1)
    response = client.post(report_path(GHOST), json={"reasons": [], "text": "字" * 999})
    assert response.status_code == 404 and "fields" not in response.get_data(as_text=True)


# ---- 400、409、429 -------------------------------------------------------------------------------


@pytest.mark.parametrize("reasons", [[], ["spam"], ["other", "other"], "other", None])
def test_invalid_reasons_are_400_naming_the_field_without_echoing_the_value(app, reasons):
    client = signed_in(app, 1)
    version = player_script(app)
    response = client.post(report_path(version), json={"reasons": reasons})
    body = response.get_json()
    assert response.status_code == 400 and set(body["error"]["fields"]) == {"reasons"}
    assert "spam" not in response.get_data(as_text=True)
    assert count(app, ScriptReport) == 0


def test_a_missing_reasons_key_is_400(app):
    client = signed_in(app, 1)
    response = client.post(report_path(player_script(app)), json={"text": "只有文字"})
    assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"reasons"}


def test_a_text_over_200_characters_is_400_naming_the_text_field(app):
    client = signed_in(app, 1)
    version = player_script(app)
    response = report(client, version, ["other"], "字" * 201)
    assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"text"}
    assert report(client, version, ["other"], "字" * 200).status_code == 201               # 恰好 200 字可以
    assert count(app, ScriptReport) == 1


def test_a_second_report_on_the_same_version_is_409_already_reported(app):
    client = signed_in(app, 1)
    version = player_script(app)
    assert report(client, version).status_code == 201
    response = report(client, version, ["plagiarism"], "第二次")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "already_reported"
    assert count(app, ScriptReport) == 1


def test_the_sixth_report_of_the_day_is_429_with_a_retry_after_header(app):
    client = signed_in(app, 1)
    versions = [player_script(app, 2, title=f"劇本{index}") for index in range(6)]
    assert [report(client, version).status_code for version in versions[:5]] == [201] * 5
    response = report(client, versions[5])
    assert response.status_code == 429 and response.get_json()["error"]["code"] == "report_limit"
    assert response.headers["Retry-After"] == str(4 * 3600)                                # 假時鐘在台灣時間 20:00，距明日 00:00 四小時
    assert count(app, ScriptReport) == 5


def test_a_limit_of_zero_pauses_reporting_and_a_change_takes_effect_on_the_next_request(app):
    client = signed_in(app, 1)
    first, second = player_script(app, 2, title="一"), player_script(app, 2, title="二")
    with db(app) as s:
        s.add(RuntimeLimit(key="script_report_daily", int_value=0, updated_at=START))
        s.commit()
    assert report(client, first).status_code == 429
    with db(app) as s:
        s.scalar(select(RuntimeLimit).where(RuntimeLimit.key == "script_report_daily")).int_value = 1
        s.commit()
    assert report(client, first).status_code == 201                                        # 不必重新部署，下一個請求就用新值
    assert report(client, second).status_code == 429


def test_the_error_responses_never_echo_the_text_the_player_typed(app):
    client = signed_in(app, 1)
    version = player_script(app)
    texts = [
        report(client, GHOST, ["other"], SPECIAL_TEXT).get_data(as_text=True),
        report(client, version, ["spam"], SPECIAL_TEXT).get_data(as_text=True),
        report(client, version, ["other"], SPECIAL_TEXT * 10).get_data(as_text=True),
    ]
    for text in texts:
        assert "DROP TABLE" not in text and "onerror" not in text
