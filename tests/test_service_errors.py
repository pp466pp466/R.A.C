"""application-layering 規格：Service 的業務錯誤統一對應 HTTP 回應；API 請求本文的格式與大小。"""
import pytest
from flask import jsonify

from app import create_app
from app.config import load_config
from app.controllers.request_helpers import read_json_object
from datetime import datetime, timezone

from app.services.errors import (
    AgeConfirmationRequired, AgeRestricted, AlreadyCheckedIn, ChatSuspended, CheckinOverflow, Conflict, ConversationLimitReached,
    DailyQuotaExceeded, Forbidden, HoldAlreadyRefunded, HoldAlreadySettled, InsufficientCoins, NotFound, RateLimited, ServiceError,
    ServiceUnavailable, SystemBusy, TooManyRequests, Unauthenticated, ValidationError, WalletBusy,
)

TRACE_MARKERS = ("Traceback", 'File "', "app\\", "app/", "sqlalchemy", "SELECT ")


@pytest.fixture()
def app():
    app = create_app(load_config(environ={"APP_ENV": "testing"}))

    def raiser(error):
        def view():
            raise error
        return view

    cases = {
        "validation": ValidationError(fields={"password": "密碼長度須為 8 至 128 字"}),
        "validation-plain": ValidationError("請輸入內容。"),
        "unauthenticated": Unauthenticated(),
        "forbidden": Forbidden(),
        "age-required": AgeConfirmationRequired(),
        "age-restricted": AgeRestricted(),
        "age-mismatch": ValidationError("資料有誤，請重新註冊。", code="age_mismatch"),
        "age-not-eligible": ValidationError("本服務僅供 13 歲以上使用，無法註冊。", fields={"birth_date": "本服務僅供 13 歲以上使用，無法註冊。"}, code="age_not_eligible"),
        "notfound": NotFound(),
        "conflict": Conflict(),
        "toomany": TooManyRequests(),
        "unavailable": ServiceUnavailable(),
        "rate-limited": RateLimited(retry_after=17),
        "daily-quota": DailyQuotaExceeded(retry_after=3600),
        "conversation-limit": ConversationLimitReached(),
        "suspended": ChatSuspended(until=datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc), message="暫停至 17:30（台灣時間）。"),
        "system-busy": SystemBusy(),
        "custom": Unauthenticated("信箱或密碼不正確。", code="invalid_credentials"),
        "unknown-subclass": type("Odd", (ServiceError,), {})("特殊錯誤"),
        # game-wallet
        "insufficient-coins": InsufficientCoins(20, 30),
        "checkin-overflow": CheckinOverflow(100, 100),
        "already-checked-in": AlreadyCheckedIn(),
        "hold-settled": HoldAlreadySettled(),
        "hold-refunded": HoldAlreadyRefunded(),
        "wallet-busy": WalletBusy(),
    }
    for name, error in cases.items():
        app.add_url_rule(f"/api/_t/{name}", f"api_{name}", raiser(error))
        app.add_url_rule(f"/_t/{name}", f"page_{name}", raiser(error))

    @app.post("/api/_t/body")
    def body():
        return jsonify(received=read_json_object())

    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("validation", 400, "validation_error"), ("unauthenticated", 401, "unauthorized"),
        ("forbidden", 403, "forbidden"), ("notfound", 404, "not_found"),
        ("conflict", 409, "conflict"), ("toomany", 429, "too_many_requests"),
        ("unavailable", 503, "service_unavailable"),
    ],
)
def test_each_service_error_maps_to_its_status_with_a_json_body(client, name, status, code):
    response = client.get(f"/api/_t/{name}")
    assert response.status_code == status
    assert response.mimetype == "application/json"
    error = response.get_json()["error"]
    assert error["code"] == code
    assert any("一" <= ch <= "鿿" for ch in error["message"]), "訊息應為繁體中文"


def test_validation_error_carries_the_field_messages(client):
    error = client.get("/api/_t/validation").get_json()["error"]
    assert error["fields"] == {"password": "密碼長度須為 8 至 128 字"}


def test_validation_error_without_fields_has_no_fields_key(client):
    error = client.get("/api/_t/validation-plain").get_json()["error"]
    assert "fields" not in error and error["message"] == "請輸入內容。"


def test_an_error_can_override_its_code_and_message(client):
    response = client.get("/api/_t/custom")
    assert response.status_code == 401
    assert response.get_json()["error"] == {"code": "invalid_credentials", "message": "信箱或密碼不正確。"}


def test_an_unclassified_service_error_is_a_400_not_a_500(client):
    response = client.get("/api/_t/unknown-subclass")
    assert response.status_code == 400
    assert response.get_json()["error"]["message"] == "特殊錯誤"


def test_not_found_matches_a_genuinely_missing_route_in_status_and_shape(client):
    """非擁有者與不存在必須得到相同的回應：同樣的狀態碼、代碼與訊息。"""
    from_policy = client.get("/api/_t/notfound").get_json()
    from_router = client.get("/api/_t/this-route-does-not-exist").get_json()
    assert from_policy == from_router


@pytest.mark.parametrize(
    "name", ["validation", "unauthenticated", "forbidden", "notfound", "conflict", "toomany", "unavailable"]
)
def test_error_responses_never_leak_internals(client, name):
    text = client.get(f"/api/_t/{name}").get_data(as_text=True)
    for marker in TRACE_MARKERS:
        assert marker not in text


def test_pages_get_a_chinese_error_page_instead_of_json(client):
    response = client.get("/_t/notfound")
    assert response.status_code == 404
    assert response.mimetype == "text/html"
    html = response.get_data(as_text=True)
    assert "找不到您要的內容" in html and 'lang="zh-Hant-TW"' in html
    assert client.get("/_t/forbidden").status_code == 403
    assert client.get("/_t/unavailable").status_code == 503


def test_service_unavailable_says_so_in_chinese_without_blaming_the_user_or_naming_the_cause(client):
    error = client.get("/api/_t/unavailable").get_json()["error"]
    assert error == {"code": "service_unavailable", "message": "服務暫時無法使用，請稍後再試。"}
    assert not any(word in error["message"] for word in ("金鑰", "GEMINI", "API", "供應商"))


def test_every_service_error_type_maps_to_a_deliberate_status():
    """新增業務錯誤時必須同時決定它的 HTTP 狀態；不能默默落到「400」的預設。"""
    from app.controllers.errors import _status_for
    from app.services import errors

    defined = [
        value for value in vars(errors).values()
        if isinstance(value, type) and issubclass(value, ServiceError) and value is not ServiceError
    ]
    expected = {
        "ValidationError": 400, "Unauthenticated": 401, "Forbidden": 403, "AgeConfirmationRequired": 403, "AgeRestricted": 403,
        "NotFound": 404, "Conflict": 409, "ConversationBusy": 409, "MessageIdReused": 409, "VersionConflict": 409,
        "TooManyRequests": 429, "ServiceUnavailable": 503,
        "RateLimited": 429, "DailyQuotaExceeded": 429, "ConversationLimitReached": 409, "ChatSuspended": 403, "SystemBusy": 503,
        "AdminTargetForbidden": 403, "AccountDeletionPending": 403, "AccountLocked": 403, "UnderageAccountErased": 400,
        "AppealPending": 409, "AppealRateLimited": 429,
        "ContentModeRestricted": 403, "AdultModeUnavailable": 403, "ContentTierRestricted": 403, "SessionEnded": 409,
        # rpg-script-authoring
        "Unprocessable": 422, "ScriptAutoRejected": 422, "ScriptRejected": 422, "NicknameRequired": 409, "ScriptLimitReached": 409,
        "DraftChanged": 409, "NotDraft": 409, "NotPending": 409, "OpenVersionExists": 409, "AuthorNameLocked": 409, "NotSuspended": 409,
        "PublishSuspended": 403, "SubmissionLimit": 429, "ReviewUnavailable": 503,
        # rpg-character-creation
        "ProtagonistRejected": 422, "SuggestionLimit": 429, "SuggestionUnavailable": 503,
        # rpg-turn-engine
        "InputBlocked": 422, "OpeningPending": 409,
        # game-wallet
        "InsufficientCoins": 409, "CheckinOverflow": 409, "AlreadyCheckedIn": 409, "HoldAlreadySettled": 409,
        "HoldAlreadyRefunded": 409, "WalletBusy": 503,
    }
    needs_arguments = {
        errors.VersionConflict: ({"id": "x"},), errors.InsufficientCoins: (20, 30), errors.CheckinOverflow: (100, 100),
    }
    made = {cls.__name__: _status_for(cls(*needs_arguments.get(cls, ()))) for cls in defined}
    assert made == expected


# ---- 用量限制的錯誤（cs-usage-limits、global-usage-budget、cs-safety-suspension）---------------

@pytest.mark.parametrize(
    ("name", "status", "code", "message", "retry_after"),
    [
        ("rate-limited", 429, "rate_limited", "您傳送訊息的速度太快了，請稍候幾秒再試。", "17"),
        ("daily-quota", 429, "daily_quota_exceeded", "您今日的對話額度已用完，將於明日 00:00（台灣時間）重置。", "3600"),
        ("conversation-limit", 409, "conversation_limit_reached", "對話數已達上限，請先刪除不需要的對話。", None),
        ("system-busy", 503, "system_busy", "目前使用人數較多，請稍後再試。", "60"),
    ],
)
def test_usage_errors_have_their_status_code_message_and_retry_after(client, name, status, code, message, retry_after):
    response = client.get(f"/api/_t/{name}")
    assert response.status_code == status
    assert response.get_json()["error"] == {"code": code, "message": message}
    assert response.headers.get("Retry-After") == retry_after


def test_a_suspension_is_a_403_with_the_resume_time_and_no_retry_after(client):
    response = client.get("/api/_t/suspended")
    assert response.status_code == 403
    body = response.get_json()
    assert body["error"] == {"code": "chat_suspended", "message": "暫停至 17:30（台灣時間）。"}
    assert body["suspended_until"] == "2026-10-29T09:30:00+00:00"
    assert "Retry-After" not in response.headers


def test_a_retry_after_is_never_below_one_second():
    assert RateLimited(retry_after=0).retry_after == 1 and DailyQuotaExceeded(retry_after=-5).retry_after == 1


@pytest.mark.parametrize("name", ["rate-limited", "daily-quota", "conversation-limit", "suspended", "system-busy"])
def test_usage_error_responses_never_leak_internals_or_numbers(client, name):
    text = client.get(f"/api/_t/{name}").get_data(as_text=True)
    for marker in TRACE_MARKERS:
        assert marker not in text


def test_system_busy_does_not_reveal_a_limit_or_a_usage_figure(client):
    message = client.get("/api/_t/system-busy").get_json()["error"]["message"]
    assert not any(ch.isdigit() for ch in message) and not any(word in message for word in ("token", "上限", "額度"))


def test_usage_errors_keep_their_places_in_the_error_hierarchy():
    assert issubclass(RateLimited, TooManyRequests) and issubclass(DailyQuotaExceeded, TooManyRequests)
    assert issubclass(ChatSuspended, Forbidden) and issubclass(SystemBusy, ServiceUnavailable)
    assert not issubclass(ChatSuspended, (AgeConfirmationRequired, AgeRestricted))       # 不是年齡閘門：前端不引導去補填


# ---- API 請求本文 --------------------------------------------------------------

def test_a_json_object_body_is_read(client):
    response = client.post("/api/_t/body", json={"email": "a@example.com", "名稱": "王小明"})
    assert response.status_code == 200
    assert response.get_json()["received"] == {"email": "a@example.com", "名稱": "王小明"}


def test_a_form_body_is_a_415_json_error(client):
    response = client.post("/api/_t/body", data={"email": "a@example.com"})
    assert response.status_code == 415
    assert response.mimetype == "application/json"
    assert response.get_json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.parametrize("raw", ["[1, 2]", "42", '"text"', "null", "", "{not json"])
def test_json_that_is_not_an_object_is_a_400_json_error(client, raw):
    response = client.post("/api/_t/body", data=raw, content_type="application/json")
    assert response.status_code == 400
    assert response.mimetype == "application/json"
    assert response.get_json()["error"]["code"] == "bad_request"


def test_a_body_over_64_kb_is_a_413_json_error(client):
    huge = '{"x": "' + "a" * (64 * 1024 + 10) + '"}'
    response = client.post("/api/_t/body", data=huge, content_type="application/json")
    assert response.status_code == 413
    assert response.mimetype == "application/json"
    assert response.get_json()["error"]["code"] == "payload_too_large"


def test_a_body_just_under_the_limit_is_accepted(client):
    payload = '{"x": "' + "a" * (64 * 1024 - 100) + '"}'
    assert client.post("/api/_t/body", data=payload, content_type="application/json").status_code == 200


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [("age-required", 403, "age_confirmation_required"), ("age-restricted", 403, "age_restricted"),
     ("age-mismatch", 400, "age_mismatch"), ("age-not-eligible", 400, "age_not_eligible")],
)
def test_the_age_errors_map_to_their_own_codes_with_a_chinese_message(client, name, status, code):
    response = client.get(f"/api/_t/{name}")
    error = response.get_json()["error"]
    assert response.status_code == status and error["code"] == code
    assert any("\u4e00" <= ch <= "\u9fff" for ch in error["message"])


def test_age_restricted_is_a_forbidden_but_a_different_one_from_age_confirmation_required():
    assert issubclass(AgeRestricted, Forbidden) and issubclass(AgeConfirmationRequired, Forbidden)
    assert not issubclass(AgeRestricted, AgeConfirmationRequired) and not issubclass(AgeConfirmationRequired, AgeRestricted)
    assert AgeRestricted().code == "age_restricted" and AgeConfirmationRequired().code == "age_confirmation_required"
    assert "補填" not in AgeRestricted().message              # 補填也沒有用，所以不引導


# ---- 遊戲幣的錯誤（game-wallet）--------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("insufficient-coins", 409, "insufficient_coins"), ("checkin-overflow", 409, "checkin_overflow"),
        ("already-checked-in", 409, "already_checked_in"), ("hold-settled", 409, "hold_already_settled"),
        ("hold-refunded", 409, "hold_already_refunded"), ("wallet-busy", 503, "wallet_busy"),
    ],
)
def test_wallet_errors_have_their_status_and_code_with_a_chinese_message(client, name, status, code):
    response = client.get(f"/api/_t/{name}")
    assert response.status_code == status
    error = response.get_json()["error"]
    assert error["code"] == code and any("一" <= ch <= "鿿" for ch in error["message"])
    for marker in TRACE_MARKERS:
        assert marker not in response.get_data(as_text=True)


def test_insufficient_coins_carries_only_the_total_balance_and_the_price(client):
    body = client.get("/api/_t/insufficient-coins").get_json()
    assert (body["balance"], body["needed"]) == (20, 30)
    assert set(body) == {"error", "balance", "needed"}                  # 不洩漏兩個餘額的拆分


def test_checkin_overflow_carries_the_preview_and_changes_nothing(client):
    body = client.get("/api/_t/checkin-overflow").get_json()
    assert body["checkin"] == {"would_gain": 100, "would_lose": 100} and set(body) == {"error", "checkin"}


def test_wallet_busy_asks_the_caller_to_retry_soon(client):
    response = client.get("/api/_t/wallet-busy")
    assert response.headers["Retry-After"] == "1"

