"""staging-verification 規格：冒煙驗收腳本（檢查項目、需要登入的項目只在提供測試 cookie 時執行、預設不呼叫模型、輸出不含機密、位址守門）。

腳本的傳輸（HTTP）可注入：這裡用 Flask 測試客戶端以 `https://` 基底網址驅動同一組檢查；另外用真正的本機 HTTP 伺服器驗證
標準函式庫的實作（`UrllibTransport`）。
"""
import re
import threading
import urllib.error
from urllib.parse import urlsplit

import pytest
from werkzeug.serving import make_server

from app import create_app
from app.config import load_config
from app.models import Base
from loadtest import actions, guard
from loadtest.provider import LoadTestProvider
from sqlalchemy.orm import Session

from tests.support.auth import add_user
from tests.support.client import raw_client
from tests.support.config_env import production_like_env
from tests.support.scripts import load_script

smoke = load_script("staging_smoke")

ORIGIN = "https://staging.example.test"
MODEL_TITLE = "模型呼叫"


def provider(**kwargs):
    return LoadTestProvider(latency_seconds=0, jitter_seconds=0, **kwargs)


def make_app(env: str = "staging", llm=None):
    """預設是設定正確的預備環境；`testing` 代表「設定不對的環境」（沒有 HSTS 與 __Host- 前綴）。"""
    environ = {"APP_ENV": "testing"} if env == "testing" else production_like_env(env)
    app = create_app(load_config(environ=environ), llm_provider=llm or provider())
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


class FlaskTransport:
    """以 Flask 測試客戶端（不做任何自動處理的 raw_client）實作腳本的 `Transport`，並記下所有往返供斷言。"""

    def __init__(self, app, origin: str = ORIGIN, log: list | None = None) -> None:
        self.app, self.origin = app, origin
        self.client = raw_client(app)
        self.log: list[dict] = [] if log is None else log

    def fresh(self):
        return FlaskTransport(self.app, self.origin, self.log)

    def add_cookie(self, name, value):
        self.client.set_cookie(name, value, domain=urlsplit(self.origin).hostname)

    def request(self, method, path, *, headers=None, json_body=None):
        kwargs = {"json": json_body} if json_body is not None else {}
        response = self.client.open(path, method=method, headers=headers or {}, base_url=self.origin, **kwargs)
        pairs = list(response.headers)
        self.log.append({
            "method": method, "path": path, "request": json_body, "status": response.status_code,
            "set_cookies": [value for key, value in pairs if key.lower() == "set-cookie"], "text": response.get_data(as_text=True),
        })
        return smoke.HttpResponse(response.status_code, pairs, response.get_data())


def mint_cookie(app) -> str:
    """與 scripts/mint_test_sessions.py 相同的做法：建立測試玩家，以該環境自己的簽章器簽出 session cookie，格式 `<名稱>=<值>`。"""
    with Session(app.extensions["db_engine"]) as session:
        user = add_user(session, 1, email="smoke-1@loadtest.invalid", email_canonical="smoke-1@loadtest.invalid", password=None)
        value = app.session_interface.get_signing_serializer(app).dumps({"uid": user.id, "tv": user.token_version})
    return f"{app.config['SESSION_COOKIE_NAME']}={value}"


def run(app, *args, factory=None, cookie: str | None = None):
    """`cookie` 為 `"auto"` 時為這個應用程式簽出一組測試玩家的 cookie（等同使用者把 SMOKE_SESSION_COOKIE 設好）。"""
    messages: list[str] = []
    transports: list[FlaskTransport] = []

    def default_factory(origin):
        transports.append(FlaskTransport(app, origin))
        return transports[-1]

    if cookie == "auto":
        cookie = mint_cookie(app)
    environ = {smoke.SESSION_COOKIE_ENV: cookie} if cookie else {}
    code = smoke.main([ORIGIN, *args], out=messages.append, transport_factory=factory or default_factory, environ=environ)
    return code, messages, (transports[0] if transports else None)


def line_for(messages: list[str], fragment: str) -> str:
    return next(line for line in messages if fragment in line)


# ---- 全部通過 -------------------------------------------------------------------------------------


def test_every_check_passes_with_a_test_cookie_and_only_the_model_check_is_skipped():
    llm = provider()
    code, messages, _ = run(make_app(llm=llm), cookie="auto")
    assert code == 0, messages
    checks = [line for line in messages if not line.startswith("結果")]
    assert len(checks) == len(smoke.CHECKS) == 11
    assert all(line.startswith("通過") for line in checks if MODEL_TITLE not in line), messages
    model = line_for(messages, MODEL_TITLE)
    assert model.startswith("略過") and "--allow-model-call" in model
    assert messages[-1] == "結果：10 項通過、0 項失敗、1 項略過"
    assert llm.calls == 0                                          # 預設不呼叫任何模型


def test_without_a_test_cookie_the_checks_that_need_a_login_are_unverified_not_passed_and_the_exit_code_is_still_zero():
    code, messages, _ = run(make_app())
    assert code == 0, messages
    unverified = [line for line in messages if line.startswith("未驗證")]
    assert len(unverified) == 4, messages                          # 進入、開新對話、cookie 屬性、管理員 API
    assert all("SMOKE_SESSION_COOKIE" in line and "mint_test_sessions" in line for line in unverified)
    for fragment in ("帶測試玩家的登入 cookie 進入", "開新對話", "__Host-", "管理員 API"):
        assert line_for(messages, fragment).startswith("未驗證"), fragment
    assert not any(line.startswith("失敗") for line in messages)
    assert messages[-1] == "結果：6 項通過、0 項失敗、1 項略過、4 項未驗證"


def test_with_the_consent_flag_exactly_one_message_is_sent_and_the_reply_must_not_be_the_degraded_text():
    llm = provider()
    code, messages, _ = run(make_app(llm=llm), "--allow-model-call", cookie="auto")
    assert code == 0 and llm.calls == 1, messages
    assert line_for(messages, MODEL_TITLE).startswith("通過")


def test_the_consent_flag_alone_does_not_send_anything_without_a_login():
    llm = provider()
    code, messages, _ = run(make_app(llm=llm), "--allow-model-call")
    assert code == 0 and llm.calls == 0
    assert line_for(messages, MODEL_TITLE).startswith("未驗證")


def test_a_degraded_model_reply_fails_the_model_check():
    llm = provider(error_rates={"server_error": 1.0})
    code, messages, _ = run(make_app(llm=llm), "--allow-model-call", cookie="auto")
    assert code == 1
    failed = line_for(messages, MODEL_TITLE)
    assert failed.startswith("失敗") and "降級" in failed


def test_a_cookie_the_environment_does_not_recognise_fails_the_sign_in_and_the_dependent_checks_are_skipped():
    code, messages, _ = run(make_app(), cookie="session=not-a-signed-cookie")
    assert code == 1
    assert line_for(messages, "帶測試玩家的登入 cookie 進入").startswith("失敗")
    for fragment in ("開新對話", "__Host-", "管理員 API"):
        assert line_for(messages, fragment).startswith("略過"), fragment


# ---- 缺項各別失敗 -----------------------------------------------------------------------------------


def test_an_environment_without_hsts_and_host_cookies_fails_exactly_those_checks_and_names_the_problem():
    code, messages, _ = run(make_app("testing"), cookie="auto")
    assert code == 1
    headers = line_for(messages, "安全標頭")
    assert headers.startswith("失敗") and "Strict-Transport-Security" in headers and "X-Frame-Options" not in headers
    cookies = line_for(messages, "__Host-")
    assert cookies.startswith("失敗") and "__Host-" in cookies and "Secure" in cookies
    failed = [line for line in messages if line.startswith("失敗")]
    assert len(failed) == 2, messages                              # 其餘項目照常通過
    assert messages[-1] == "結果：8 項通過、2 項失敗、1 項略過"


def test_a_csrf_check_that_does_not_block_a_tokenless_post_fails_that_check_with_the_status():
    app = make_app()
    app.before_request_funcs[None] = [f for f in app.before_request_funcs[None] if f.__name__ != "check_request"]
    code, messages, _ = run(app)
    assert code == 1
    csrf = line_for(messages, "CSRF 權杖")
    assert csrf.startswith("失敗") and "狀態碼" in csrf and "應被拒絕為 403" in csrf


def test_a_removed_password_endpoint_that_comes_back_fails_the_check_and_names_the_path_and_status():
    app = make_app()

    @app.post("/api/auth/login")
    def comeback():
        return {"ok": True}

    code, messages, _ = run(app)
    assert code == 1
    removed = line_for(messages, "已移除的密碼登入與註冊端點")
    assert removed.startswith("失敗") and "/api/auth/login 得到 200" in removed and "/api/auth/register" not in removed


def test_a_content_security_policy_that_appears_is_reported_as_a_failure():
    app = make_app()

    @app.after_request
    def add_csp(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        return response

    code, messages, _ = run(app)
    assert code == 1 and "Content-Security-Policy" in line_for(messages, "安全標頭")


def test_an_unreachable_target_fails_the_checks_without_raising():
    class Dead:
        origin = ORIGIN

        def request(self, *a, **k):
            return smoke.HttpResponse(0)

        def add_cookie(self, name, value):
            pass

        def fresh(self):
            return self

    code, messages, _ = run(None, factory=lambda origin: Dead(), cookie="session=whatever")
    assert code == 1 and line_for(messages, "/healthz").startswith("失敗") and "0" in line_for(messages, "/healthz")
    assert line_for(messages, "開新對話").startswith("略過")          # 前提失敗的項目略過，不另外亂報


def test_the_admin_api_check_expects_404_the_same_as_a_missing_endpoint():
    code, messages, transport = run(make_app(), cookie="auto")
    assert code == 0
    admin = [entry for entry in transport.log if entry["path"] == "/api/admin/overview"]
    assert admin and admin[0]["status"] == 404


def test_the_removed_endpoint_check_sends_a_valid_csrf_token_so_the_404_is_not_hidden_by_a_403():
    _, _, transport = run(make_app())
    attempts = [entry for entry in transport.log if entry["path"] in smoke.REMOVED_ENDPOINTS]
    assert [entry["status"] for entry in attempts] == [404, 404]


# ---- 登入 cookie 只從環境變數進來，格式不對時拒絕且不回顯 -------------------------------------------------


@pytest.mark.parametrize("raw", ["no-equals-sign-secret-value", "=value-only-secret", "name-only-secret=", "bad name=secret-value", "a;b=secret"])
def test_a_malformed_cookie_variable_is_refused_before_any_request_and_never_echoed(raw):
    def never(origin):
        raise AssertionError("格式不對時不得建立傳輸或送出請求")

    messages: list[str] = []
    code = smoke.main([ORIGIN], out=messages.append, transport_factory=never, environ={smoke.SESSION_COOKIE_ENV: raw})
    assert code == 2 and messages and smoke.SESSION_COOKIE_ENV in messages[0]
    assert "secret" not in "\n".join(messages)


def test_the_cookie_is_not_accepted_as_a_command_line_argument():
    with pytest.raises(SystemExit):
        smoke.main([ORIGIN, "--session-cookie", "session=x"], out=lambda line: None)


# ---- 輸出只有項目與結果 -----------------------------------------------------------------------------


def test_the_output_never_contains_cookies_tokens_or_response_bodies():
    app = make_app()
    cookie = mint_cookie(app)
    code, messages, transport = run(app, "--allow-model-call", cookie=cookie)
    assert code == 0
    text = "\n".join(messages)
    secrets = {cookie, cookie.split("=", 1)[1], provider_reply_text()}
    for entry in transport.log:
        for set_cookie in entry["set_cookies"]:
            secrets.add(set_cookie.split(";", 1)[0].split("=", 1)[1])
        secrets.update(re.findall(r'name="csrf-token" content="([^"]+)"', entry["text"]))
    assert len(secrets) >= 5 and all(secret and secret not in text for secret in secrets)


def provider_reply_text() -> str:
    from loadtest.provider import REPLY

    return REPLY["reply_text"]


def test_a_failure_detail_never_echoes_a_cookie_value():
    _, messages, transport = run(make_app("testing"), cookie="auto")
    values = {cookie.split(";", 1)[0].split("=", 1)[1] for entry in transport.log for cookie in entry["set_cookies"]}
    assert values and not any(value in "\n".join(messages) for value in values)


# ---- 測試玩家使用保留網域 -----------------------------------------------------------------------------


def test_the_minted_players_use_the_reserved_invalid_domain():
    from tests.support.scripts import load_script

    assert guard.TEST_EMAIL_DOMAIN == load_script("mint_test_sessions").TEST_EMAIL_DOMAIN and guard.TEST_EMAIL_DOMAIN.endswith(".invalid")


# ---- 位址守門：只對 https 的預備環境，正式環境沒有任何參數可繞過 ----------------------------------------------


@pytest.mark.parametrize(
    "url, reason",
    [
        ("http://staging.example.test", "https"),
        ("https://cs-production-abc123.a.run.app", "正式環境"),
        ("https://production.example.com", "正式環境"),
        ("http://my-production.example.com", "正式環境"),
        ("https://user:pw@staging.example.test", "帳密"),
        ("https://staging.example.test/some/path", "路徑"),
        ("https://staging.example.test/?a=1", "查詢字串"),
        ("ftp://staging.example.test", ""),
    ],
)
def test_a_refused_target_sends_no_request_and_exits_with_2(url, reason):
    def never(origin):
        raise AssertionError("位址被拒絕時不得建立傳輸或送出請求")

    messages: list[str] = []
    assert smoke.main([url], out=messages.append, transport_factory=never) == 2
    assert messages and reason in messages[0] and "pw" not in messages[0]


def test_a_listed_production_domain_is_refused_and_no_flag_can_override_it(monkeypatch):
    monkeypatch.setattr(guard, "PRODUCTION_DOMAINS", ("cs.example.com",))
    for url in ("https://cs.example.com", "https://www.cs.example.com"):
        assert smoke.main([url], out=lambda line: None, transport_factory=lambda origin: pytest.fail("不得送出請求")) == 2
    for flag in ("--confirm-remote", "--force", "--yes"):
        with pytest.raises(SystemExit):
            smoke.main(["https://cs-staging.example.com", flag], out=lambda line: None)


def test_validate_target_returns_the_normalised_origin_and_sends_nothing():
    assert smoke.validate_target("https://abc-123.ngrok-free.dev/") == "https://abc-123.ngrok-free.dev"
    assert smoke.validate_target("staging.example.test") == "https://staging.example.test"


# ---- 標準函式庫的傳輸 -------------------------------------------------------------------------------


@pytest.fixture()
def live_url():
    app = make_app("testing")
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    thread.join(timeout=5)


def jar_of(transport):
    return next(handler.cookiejar for handler in transport._opener.handlers if hasattr(handler, "cookiejar"))


def test_the_urllib_transport_returns_4xx_as_responses_keeps_cookies_and_fresh_starts_clean(live_url):
    transport = smoke.UrllibTransport(live_url)
    assert transport.request("GET", "/healthz").status == 200
    assert transport.request("GET", "/no-such-page").status == 404                  # HTTPError 轉成回應，不是例外
    assert transport.request("GET", "/").status == 200
    assert any(cookie.name == "csrf" for cookie in jar_of(transport))              # 同一個實例保留 cookie
    clean = transport.fresh()
    assert clean.origin == transport.origin and len(jar_of(clean)) == 0            # 未登入的檢查用乾淨的 cookie jar
    assert clean.request("GET", "/api/cs/conversations").status == 401


def test_the_urllib_transport_sends_an_added_cookie_next_to_the_servers_own_cookies_and_fresh_does_not_carry_it():
    """`SMOKE_SESSION_COOKIE` 走這條路：cookie 放進 jar，之後的請求照常帶上，並和伺服器發的 CSRF cookie 並存（不是覆蓋整個 Cookie 標頭）。"""
    app = make_app("testing")
    cookie = mint_cookie(app)
    name, value = cookie.split("=", 1)
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        transport = smoke.UrllibTransport(f"http://127.0.0.1:{httpd.server_port}")
        assert transport.request("GET", "/api/auth/me").status == 401              # 還沒有登入 cookie
        transport.add_cookie(name, value)
        assert transport.request("GET", "/api/auth/me").status == 200              # 加進 jar 之後，同一個實例就是登入狀態
        assert transport.request("GET", "/").status == 200
        assert {cookie.name for cookie in jar_of(transport)} >= {name, "csrf"}     # 與伺服器發的 CSRF cookie 並存
        assert transport.fresh().request("GET", "/api/auth/me").status == 401      # 未登入的檢查用乾淨的 jar
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_a_cookie_added_for_an_https_origin_is_secure_so_a_host_prefixed_session_is_sent_there():
    transport = smoke.UrllibTransport("https://staging.example.test")
    transport.add_cookie("__Host-session", "value-1")
    (cookie,) = list(jar_of(transport))
    assert cookie.name == "__Host-session" and cookie.secure and cookie.path == "/" and cookie.domain == "staging.example.test"


def test_the_urllib_transport_reports_a_connection_failure_as_status_zero(monkeypatch):
    transport = smoke.UrllibTransport("https://staging.example.test")

    def refuse(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(transport._opener, "open", refuse)
    assert transport.request("GET", "/healthz").status == 0


def test_requests_through_ngrok_skip_the_browser_warning_page_and_other_hosts_do_not_get_the_header():
    assert smoke.UrllibTransport("https://abc-123.ngrok-free.dev")._extra == {"ngrok-skip-browser-warning": "1"}
    assert smoke.UrllibTransport("https://staging-abc.a.run.app")._extra == {}
