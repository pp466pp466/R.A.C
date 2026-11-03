"""service-observability 規格「完整流程後掃描日誌」：註冊、驗證、登入、忘記與重設密碼、Google 登入、登出、建立管理員，
全程收集日誌，確認找不到任何 email、密碼、權杖、授權碼、state、Cookie 值。"""
import json

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.observability.json_logging import ALLOWED_FIELDS
from app.services.google_oidc import GoogleProfile
from tests.support.age import age_fields
from tests.support.age_gate import add_ai_endpoint
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender
from tests.support.google import FakeProvider

pytestmark = pytest.mark.usefixtures("generous_free_limits")      # 掃描日誌用的流程不是用量限制（見 conftest）

MEMBER_EMAIL = "scan.member@example.com"
GOOGLE_EMAIL = "scan.google@example.com"
ADMIN_EMAIL = "scan.admin@example.com"
STALE_EMAIL = "scan.stale@example.com"
LOCKED_EMAIL = "scan.locked@example.com"
CHILD_EMAIL = "scan.child@example.com"
UNFINISHED_EMAIL = "scan.unfinished@example.com"
APPEAL_TEXT = "只該出現在申訴與管理員檢視裡的私密說明SCAN-APPEAL-TEXT"
GOOGLE_SUB = "sub-9876543210-scan"
PASSWORDS = {
    "first": "scan-first-password-A1",
    "wrong": "scan-WRONG-password-B2",
    "reset": "scan-reset-password-C3",
    "changed": "scan-changed-password-D4",
    "admin": "scan-admin-password-E5",
}
AUTH_CODE = "scan-google-authorization-code"
BEARER = "scan-bearer-credential-xyz"
BASE_KEYS = {"time", "level", "logger", "message"}


@pytest.fixture()
def world():
    sender, provider = FakeSender(), FakeProvider()
    provider.profile = GoogleProfile(GOOGLE_SUB, GOOGLE_EMAIL, True, "掃描用 Google 使用者")
    config = load_config(environ={"APP_ENV": "testing", "EMAIL_VERIFICATION_REQUIRED": "true", **AUTH_ENV, "BASE_URL": "http://localhost:5000"})
    app = create_app(config, mail_sender=sender, identity_provider=provider, clock=FakeClock())
    Base.metadata.create_all(app.extensions["db_engine"])
    add_ai_endpoint(app)                                      # 代表會呼叫 Gemini 的端點，用來走年齡閘門
    return app, sender, provider


def test_negative_control_the_capture_would_notice_a_careless_log_line(log_lines):
    """若有人寫了 logger.info(f"user {email}")，這套收集方式必須抓得到；否則「找不到洩漏」就沒有意義。"""
    import logging

    logging.getLogger("app.careless").info(f"註冊了 {MEMBER_EMAIL}")
    assert MEMBER_EMAIL in log_lines.raw()


def test_a_complete_tour_of_every_flow_leaves_no_secret_in_the_logs(world, log_lines):
    app, sender, provider = world
    secrets_seen: set[str] = set()

    def remember_cookies(client):
        for name in ("session", "csrf", "oauth_tx", "admin_step"):
            cookie = client.get_cookie(name)
            if cookie:
                secrets_seen.add(cookie.value)

    api = "/api/auth"
    clock = app.extensions["clock"]
    client = app.test_client()
    extra = {"headers": {"Authorization": f"Bearer {BEARER}"}}

    # 註冊 → 驗證信 → 驗證
    assert client.post(f"{api}/register", json={"email": MEMBER_EMAIL, "password": PASSWORDS["first"], "display_name": "掃描使用者", "accept_terms": True, **age_fields(30)}, **extra).status_code == 200
    verify_token = sender.last_token()
    assert client.post(f"{api}/verify-email", json={"token": verify_token}).status_code == 200

    # 登入失敗一次、成功一次，再變更密碼
    assert client.post(f"{api}/login", json={"email": MEMBER_EMAIL, "password": PASSWORDS["wrong"]}).status_code == 401
    assert client.post(f"{api}/login", json={"email": MEMBER_EMAIL, "password": PASSWORDS["first"]}).status_code == 200
    remember_cookies(client)
    assert client.post(f"{api}/change-password", json={"current_password": PASSWORDS["first"], "new_password": PASSWORDS["changed"]}).status_code == 200
    assert client.post(f"{api}/logout").status_code == 200

    # 忘記密碼 → 重設
    clock.advance(minutes=5)
    assert client.post(f"{api}/forgot-password", json={"email": MEMBER_EMAIL}).status_code == 200
    reset_token = sender.last_token()
    assert client.post(f"{api}/reset-password", json={"token": reset_token, "new_password": PASSWORDS["reset"]}).status_code == 200
    assert client.post(f"{api}/login", json={"email": MEMBER_EMAIL, "password": PASSWORDS["reset"]}).status_code == 200
    remember_cookies(client)
    assert client.post(f"{api}/logout-all").status_code == 200

    # Google 登入（新帳號）
    google = app.test_client()
    assert google.post(f"{api}/google/start", json={"accept_terms": True, **age_fields(30)}).status_code == 200
    remember_cookies(google)
    from app.security.cookies import SignedCookies
    from app.security.oauth_cookie import OAUTH_SALT

    transaction = SignedCookies(app.extensions["app_config"], app.extensions["clock"]).loads(
        OAUTH_SALT, google.get_cookie("oauth_tx").value, max_age_seconds=600
    )
    secrets_seen.update({transaction["s"], transaction["n"], transaction["v"]})
    assert google.get(f"/auth/google/callback?code={AUTH_CODE}&state={transaction['s']}").status_code == 302
    remember_cookies(google)
    google.get(f"/auth/google/callback?code={AUTH_CODE}&state=forged-state-for-scan")          # 被拒絕的一次

    # Google 登入（新帳號但沒有勾選條款）：被拒絕，導向登入頁
    provider.profile = GoogleProfile(GOOGLE_SUB + "-b", "scan.google.b@example.com", True, "掃描用 Google 使用者 B")
    refused = app.test_client()
    assert refused.post(f"{api}/google/start", json={"accept_terms": False}).status_code == 200
    refused_tx = SignedCookies(app.extensions["app_config"], app.extensions["clock"]).loads(
        OAUTH_SALT, refused.get_cookie("oauth_tx").value, max_age_seconds=600
    )
    secrets_seen.update({refused_tx["s"], refused_tx["n"], refused_tx["v"]})
    assert refused.get(f"/auth/google/callback?code={AUTH_CODE}&state={refused_tx['s']}").headers["Location"] == "/login?error=terms_required"

    # 升級前就存在、沒有年齡確認紀錄的帳號：登入後被年齡閘門擋下，補確認之後通過
    with Session(app.extensions["db_engine"]) as s:
        add_user(s, 7, age=None, email="scan.legacy@example.com", email_canonical="scan.legacy@example.com", email_verified_at=clock())
    legacy = app.test_client()
    assert legacy.post(f"{api}/login", json={"email": "scan.legacy@example.com", "password": GOOD_PASSWORD}).status_code == 200
    remember_cookies(legacy)
    assert legacy.post("/api/_t/ai", json={}).status_code == 403
    assert legacy.post(f"{api}/verify-age", json=age_fields(41)).status_code == 200
    assert legacy.post("/api/_t/ai", json={}).status_code == 200

    # 一般的頁面請求（帶著 Cookie 與授權標頭）
    client.get("/login?next=/account", **extra)

    # 建立管理員
    result = app.test_cli_runner().invoke(args=["create-admin", "--email", ADMIN_EMAIL, "--display-name", "掃描管理員", "--password-stdin"], input=f"{PASSWORDS['admin']}\n")
    assert result.exit_code == 0, result.output

    # 管理員二階段登入：先通過 Google（拿到中間狀態 cookie，不是登入狀態），再輸入密碼（錯一次、對一次）
    provider.profile = GoogleProfile(GOOGLE_SUB + "-admin", ADMIN_EMAIL, True, "掃描管理員")
    admin_client = app.test_client()
    assert admin_client.post(f"{api}/google/start", json={}).status_code == 200
    admin_tx = SignedCookies(app.extensions["app_config"], app.extensions["clock"]).loads(
        OAUTH_SALT, admin_client.get_cookie("oauth_tx").value, max_age_seconds=600
    )
    secrets_seen.update({admin_tx["s"], admin_tx["n"], admin_tx["v"]})
    step = admin_client.get(f"/auth/google/callback?code={AUTH_CODE}&state={admin_tx['s']}")
    assert step.headers["Location"] == "/login/password"
    remember_cookies(admin_client)
    assert admin_client.get(f"{api}/me").status_code == 401                                   # 中間狀態不是登入狀態
    assert admin_client.post(f"{api}/admin-password", json={"password": PASSWORDS["wrong"]}).status_code == 401
    assert admin_client.post(f"{api}/admin-password", json={"password": PASSWORDS["admin"]}).status_code == 200
    remember_cookies(admin_client)

    # 年齡閘門：管理員第一次登入後被擋（會呼叫 Gemini 的端點先問政策）、補確認、再通過
    assert admin_client.post("/api/_t/ai", json={}).status_code == 403
    assert admin_client.post(f"{api}/verify-age", json=age_fields(52)).status_code == 200
    assert admin_client.post("/api/_t/ai", json={}).status_code == 200

    # 被鎖定的玩家申訴、管理員檢視與解除；補填年齡時未滿 13 歲被抹除；逾時未補填的帳號被清除（日誌與輸出都不得出現個資）
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import User
    from tests.support.auth import START, sign_in_as

    with Session(app.extensions["db_engine"]) as s:
        locked_id = add_user(s, 9, email=LOCKED_EMAIL, email_canonical=LOCKED_EMAIL, display_name="被鎖定的人", locked_at=clock(), lock_reason="rate_limited").id
        child_id = add_user(s, 10, age=None, password=None, email=CHILD_EMAIL, email_canonical=CHILD_EMAIL, display_name="太小的人").id
        add_user(
            s, 11, age=None, password=None, email=UNFINISHED_EMAIL, email_canonical=UNFINISHED_EMAIL, display_name="沒填完的人",
            age_deadline_at=clock() - timedelta(hours=1),
        )
        locked_public = s.scalar(select(User.public_id).where(User.id == locked_id))
    locked_client = app.test_client()
    sign_in_as(locked_client, locked_id)
    assert locked_client.post("/api/appeals", json={"content": APPEAL_TEXT}).status_code == 201
    assert admin_client.get("/api/admin/appeals").get_json()["appeals"][0]["content"] == APPEAL_TEXT       # 只有管理員的檢視看得到內容
    assert admin_client.post(f"/api/admin/users/{locked_public}/unlock", json={"password": PASSWORDS["admin"]}).status_code == 200
    child_client = app.test_client()
    sign_in_as(child_client, child_id)
    assert child_client.post(f"{api}/verify-age", json=age_fields(12)).status_code == 400                   # 未滿 13 歲：帳號立即抹除
    unconfirmed_preview = app.test_cli_runner().invoke(args=["purge-unconfirmed-accounts", "--dry-run"])
    unconfirmed_purge = app.test_cli_runner().invoke(args=["purge-unconfirmed-accounts"])
    assert unconfirmed_preview.exit_code == 0 and unconfirmed_purge.exit_code == 0 and "已刪除 1 個" in unconfirmed_purge.output

    # 閒置帳號清除：一個超過 12 個月沒登入、資料齊全的帳號被刪除；輸出與日誌都不得出現它的任何個資

    with Session(app.extensions["db_engine"]) as s:
        add_user(
            s, 8, email=STALE_EMAIL, email_canonical=STALE_EMAIL, display_name="閒置的人", age=22,
            last_login_at=START - timedelta(days=500), created_at=START - timedelta(days=600),
        )
    preview = app.test_cli_runner().invoke(args=["purge-inactive-accounts", "--dry-run"])
    purged = app.test_cli_runner().invoke(args=["purge-inactive-accounts"])
    assert preview.exit_code == 0 and purged.exit_code == 0 and "已刪除 1 個" in purged.output
    stats = app.test_cli_runner().invoke(args=["age-stats"])
    assert stats.exit_code == 0
    cli_output = preview.output + purged.output + stats.output + unconfirmed_preview.output + unconfirmed_purge.output

    # ---- 掃描 ----
    raw = log_lines.raw()
    assert raw.strip(), "測試沒有產生任何日誌，掃描沒有意義"
    samples = {
        MEMBER_EMAIL, GOOGLE_EMAIL, ADMIN_EMAIL, GOOGLE_SUB, AUTH_CODE, BEARER, verify_token, reset_token,
        "forged-state-for-scan", "scan.google.b@example.com", "scan.legacy@example.com", GOOGLE_SUB + "-b", STALE_EMAIL,
        age_fields(22)["birth_date"], age_fields(12)["birth_date"], APPEAL_TEXT, LOCKED_EMAIL, CHILD_EMAIL, UNFINISHED_EMAIL,
        GOOD_PASSWORD, *PASSWORDS.values(), *secrets_seen,
        *(age_fields(age)["birth_date"] for age in (30, 41, 52)),                 # 所有人的生日都不得出現在日誌
    }
    assert len(secrets_seen) >= 4
    for sample in samples:
        assert sample and sample not in raw, f"日誌洩漏了：{sample[:12]}…"
        assert sample not in cli_output, f"指令輸出洩漏了：{sample[:12]}…"

    # 每一行都是合法的 JSON，且只有允許的欄位
    lines = log_lines()
    assert len(lines) > 20
    for line in lines:
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"
    events = {line["event"] for line in lines if "event" in line}
    assert {
        "auth.register", "auth.verification_sent", "auth.email_verified", "auth.login_failed.bad_password", "auth.login_success",
        "auth.password_changed", "auth.logout", "auth.password_reset_requested", "auth.password_reset", "auth.logout_all",
        "auth.google_started", "auth.google_registered", "auth.google_rejected.state", "auth.admin_created", "auth.admin_google_passed",
        "auth.age_confirmed.register", "auth.age_confirmed.later", "auth.age_gate_denied",
        "auth.google_rejected.terms", "account.inactive_purged", "account.appeal_submitted", "account.player_unlocked",
        "account.underage_erased", "account.unconfirmed_purged", "auth.age_rejected.underage",
    } <= events
    # 請求日誌的路徑只有路由規則，不含查詢字串（Google 回呼的 code 與 state 在查詢字串裡）
    assert all("?" not in line.get("path", "") for line in lines)
    json.dumps(lines)                                                                       # 全部可序列化


# ---- 客服對話的完整流程 ----------------------------------------------------------------------------

CHAT_SECRETS = {
    "user": "我的訂單號碼是 SECRET-ORDER-778899，信箱 chat.scan@example.com",
    "reply": "回覆私密字樣-REPLY-5521",
    "summary": "摘要私密字樣-SUMMARY-6632",
    "bad": "壞輸出私密字樣-BAD-7743",
}


class ScriptedTransport:
    """假的 HTTP 傳輸：依腳本回應，沒有腳本時，對話呼叫回客服 JSON、摘要呼叫回摘要 JSON。"""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict]] = []

    @staticmethod
    def ok(text: str) -> tuple[int, dict]:
        return 200, {
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 321, "candidatesTokenCount": 123},
        }

    def post_json(self, url, headers, body, timeout):
        if self.script:
            return self.script.pop(0)
        system = body["system_instruction"]["parts"][0]["text"]
        if "累積摘要" in system:
            return self.ok(json.dumps({"summary": CHAT_SECRETS["summary"]}, ensure_ascii=False))
        return self.ok(json.dumps({
            "intent": "account_help", "confidence": 0.9, "entities": {"topic": None, "membership_tier": None, "account_issue": "forgot_password", "feedback_type": None},
            "reply_text": CHAT_SECRETS["reply"], "suggested_actions": [],
        }, ensure_ascii=False))


def test_a_complete_chat_tour_leaves_no_message_reply_summary_model_output_prompt_or_key_in_the_logs(log_lines):
    from app.llm.gemini import GeminiProvider
    from app.services.cs_prompt import leak_marker
    from tests.support.config_env import FAKE_GEMINI_KEY
    from tests.support.idor import logged_in_client

    transport = ScriptedTransport()
    config = load_config(environ={"APP_ENV": "testing", "GEMINI_API_KEY": FAKE_GEMINI_KEY})
    app = create_app(config, clock=FakeClock(), llm_provider=GeminiProvider(FAKE_GEMINI_KEY, transport=transport))
    Base.metadata.create_all(app.extensions["db_engine"])
    with Session(app.extensions["db_engine"]) as session:
        from app.models import User

        user = session.get(User, add_user(session, 1, age=30).id)
    client = logged_in_client(app, user)
    base = "/api/cs/conversations"

    def message_id(n):
        return f"00000000-0000-4000-8000-{n:012d}"

    public_id = client.post(base, json={"client_id": message_id(999)}).get_json()["conversation"]["id"]

    def say(n, text=CHAT_SECRETS["user"]):
        return client.post(f"{base}/{public_id}/messages", json={"client_msg_id": message_id(n), "content": text})

    for n in range(1, 12):                                                       # 11 輪：第 11 輪之後觸發摘要
        assert say(n).status_code == 200
    assert say(1).status_code == 200                                              # 重送（冪等）
    bad = transport.ok(CHAT_SECRETS["bad"])
    transport.script = [bad, bad]
    assert say(12).get_json()["assistant_message"]["msg_status"] == "fallback"   # 驗證失敗兩次
    transport.script = [(429, {"error": {"message": f"quota {FAKE_GEMINI_KEY}"}})]
    assert say(13).status_code == 200                                             # 速率限制 → 備援
    transport.script = [(503, {"error": {"message": CHAT_SECRETS["user"]}}), (503, {"error": {"message": CHAT_SECRETS["user"]}})]
    assert say(14).get_json()["assistant_message"]["msg_status"] == "fallback"   # 主要與備援都 5xx
    transport.script = [(200, {"promptFeedback": {"blockReason": "SAFETY"}})]
    assert say(15).get_json()["assistant_message"]["msg_status"] == "fallback"   # 被安全過濾阻擋
    echo = (403, {"error": {"message": f"API key {FAKE_GEMINI_KEY} not valid. {CHAT_SECRETS['user']}"}})
    transport.script = [echo]
    assert say(16).get_json()["assistant_message"]["msg_status"] == "fallback"   # 設定錯誤
    client.patch(f"{base}/{public_id}", json={"title": CHAT_SECRETS["user"][:20], "state_version": 1})
    client.get(f"{base}/{public_id}/messages")
    client.get("/cs")
    client.delete(f"{base}/{public_id}")

    raw = log_lines.raw()
    assert raw.strip()
    marker = leak_marker(app.extensions["app_config"].secret_key.reveal())
    samples = {
        "SECRET-ORDER-778899", "chat.scan@example.com", CHAT_SECRETS["user"], CHAT_SECRETS["reply"], CHAT_SECRETS["summary"],
        CHAT_SECRETS["bad"], FAKE_GEMINI_KEY, marker, "內部識別碼", "你是「智能客服」", user.email, public_id,
    }
    for sample in samples:
        assert sample and sample not in raw, f"日誌洩漏了：{sample[:14]}…"

    lines = log_lines()
    for line in lines:
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"
    events = {line["event"] for line in lines if "event" in line}
    assert {"llm.call", "llm.breaker_opened", "cs.reply"} <= events
    calls = [line for line in lines if line.get("event") == "llm.call"]
    assert {"ok", "validation_failed", "rate_limited", "server_error", "blocked", "config_error"} <= {c["outcome"] for c in calls}
    assert {c["kind"] for c in calls} == {"chat", "summary"}
    assert [c["level"] for c in calls if c["outcome"] == "config_error"] == ["ERROR"]
    assert all("?" not in line.get("path", "") for line in lines)
