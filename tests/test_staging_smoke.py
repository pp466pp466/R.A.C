"""staging-verification 規格：冒煙驗收腳本（檢查項目、預設不呼叫模型、輸出不含機密、位址守門、測試帳號網域）。

腳本的傳輸（HTTP）可注入：這裡用 Flask 測試客戶端以 `https://` 基底網址驅動同一組檢查；另外用真正的本機 HTTP 伺服器驗證
標準函式庫的實作（`UrllibTransport`）。
"""
import re
import threading
import urllib.error

import pytest
from werkzeug.serving import make_server

from app import create_app
from app.config import load_config
from app.models import Base
from loadtest import actions, guard
from loadtest.provider import LoadTestProvider
from tests.support.client import raw_client
from tests.support.config_env import VERIFICATION_OFF_ENV, production_like_env
from tests.support.scripts import load_script

smoke = load_script("staging_smoke")

ORIGIN = "https://staging.example.test"
MODEL_TITLE = "模型呼叫"


def provider(**kwargs):
    return LoadTestProvider(latency_seconds=0, jitter_seconds=0, **kwargs)


def make_app(env: str = "staging", llm=None):
    """預設是設定正確的預備環境（信箱驗證關閉，與雲端預備環境一致）；`testing` 代表「設定不對的環境」（沒有 HSTS 與 __Host- 前綴）。"""
    environ = {"APP_ENV": "testing"} if env == "testing" else production_like_env(env, **VERIFICATION_OFF_ENV)
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

    def request(self, method, path, *, headers=None, json_body=None):
        kwargs = {"json": json_body} if json_body is not None else {}
        response = self.client.open(path, method=method, headers=headers or {}, base_url=self.origin, **kwargs)
        pairs = list(response.headers)
        self.log.append({
            "method": method, "path": path, "request": json_body, "status": response.status_code,
            "set_cookies": [value for key, value in pairs if key.lower() == "set-cookie"], "text": response.get_data(as_text=True),
        })
        return smoke.HttpResponse(response.status_code, pairs, response.get_data())


def run(app, *args, factory=None):
    messages: list[str] = []
    transports: list[FlaskTransport] = []

    def default_factory(origin):
        transports.append(FlaskTransport(app, origin))
        return transports[-1]

    code = smoke.main([ORIGIN, *args], out=messages.append, transport_factory=factory or default_factory)
    return code, messages, (transports[0] if transports else None)


def line_for(messages: list[str], fragment: str) -> str:
    return next(line for line in messages if fragment in line)


# ---- 全部通過 -------------------------------------------------------------------------------------


def test_every_check_passes_against_a_correctly_configured_staging_app_and_the_model_check_is_skipped():
    llm = provider()
    code, messages, _ = run(make_app(llm=llm))
    assert code == 0, messages
    checks = [line for line in messages if not line.startswith("結果")]
    assert len(checks) == len(smoke.CHECKS) == 10
    assert all(line.startswith("通過") for line in checks if MODEL_TITLE not in line), messages
    model = line_for(messages, MODEL_TITLE)
    assert model.startswith("略過") and "--allow-model-call" in model
    assert messages[-1] == "結果：9 項通過、0 項失敗、1 項略過"
    assert llm.calls == 0                                          # 預設不呼叫任何模型


def test_with_the_consent_flag_exactly_one_message_is_sent_and_the_reply_must_not_be_the_degraded_text():
    llm = provider()
    code, messages, _ = run(make_app(llm=llm), "--allow-model-call")
    assert code == 0 and llm.calls == 1, messages
    assert line_for(messages, MODEL_TITLE).startswith("通過")


def test_a_degraded_model_reply_fails_the_model_check():
    llm = provider(error_rates={"server_error": 1.0})
    code, messages, _ = run(make_app(llm=llm), "--allow-model-call")
    assert code == 1
    failed = line_for(messages, MODEL_TITLE)
    assert failed.startswith("失敗") and "降級" in failed


# ---- 缺項各別失敗 -----------------------------------------------------------------------------------


def test_an_environment_without_hsts_and_host_cookies_fails_exactly_those_checks_and_names_the_problem():
    code, messages, _ = run(make_app("testing"))
    assert code == 1
    headers = line_for(messages, "安全標頭")
    assert headers.startswith("失敗") and "Strict-Transport-Security" in headers and "X-Frame-Options" not in headers
    cookies = line_for(messages, "__Host-")
    assert cookies.startswith("失敗") and "__Host-" in cookies and "Secure" in cookies
    failed = [line for line in messages if line.startswith("失敗")]
    assert len(failed) == 2, messages                              # 其餘項目照常通過
    assert messages[-1] == "結果：7 項通過、2 項失敗、1 項略過"


def test_a_csrf_check_that_does_not_block_a_tokenless_post_fails_that_check_with_the_status():
    app = make_app()
    app.before_request_funcs[None] = [f for f in app.before_request_funcs[None] if f.__name__ != "check_request"]
    code, messages, _ = run(app)
    assert code == 1
    csrf = line_for(messages, "CSRF 權杖")
    assert csrf.startswith("失敗") and "狀態碼" in csrf and "應被拒絕為 403" in csrf


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

        def fresh(self):
            return self

    code, messages, _ = run(None, factory=lambda origin: Dead())
    assert code == 1 and line_for(messages, "/healthz").startswith("失敗") and "0" in line_for(messages, "/healthz")
    assert line_for(messages, "開新對話").startswith("略過")          # 前提失敗的項目略過，不另外亂報


def test_the_admin_api_check_expects_404_the_same_as_a_missing_endpoint():
    code, messages, transport = run(make_app())
    assert code == 0
    admin = [entry for entry in transport.log if entry["path"] == "/api/admin/overview"]
    assert admin and admin[0]["status"] == 404


# ---- 輸出只有項目與結果 -----------------------------------------------------------------------------


def test_the_output_never_contains_cookies_tokens_passwords_or_response_bodies():
    code, messages, transport = run(make_app(), "--allow-model-call")
    assert code == 0
    text = "\n".join(messages)
    secrets = {actions.PASSWORD, "負載測試1", provider_reply_text()}
    for entry in transport.log:
        for cookie in entry["set_cookies"]:
            value = cookie.split(";", 1)[0].split("=", 1)[1]
            secrets.add(value)
        secrets.update(re.findall(r'name="csrf-token" content="([^"]+)"', entry["text"]))
    assert len(secrets) >= 6 and all(secret and secret not in text for secret in secrets)


def provider_reply_text() -> str:
    from loadtest.provider import REPLY

    return REPLY["reply_text"]


def test_a_failure_detail_never_echoes_a_cookie_value():
    _, messages, transport = run(make_app("testing"))
    values = {cookie.split(";", 1)[0].split("=", 1)[1] for entry in transport.log for cookie in entry["set_cookies"]}
    assert values and not any(value in "\n".join(messages) for value in values)


# ---- 測試帳號使用保留網域 -----------------------------------------------------------------------------


def test_the_registered_account_uses_the_reserved_invalid_domain():
    _, _, transport = run(make_app())
    registrations = [entry["request"] for entry in transport.log if entry["path"] == "/api/auth/register"]
    assert len(registrations) == 1 and registrations[0]["email"].endswith("@loadtest.invalid")
    assert guard.TEST_EMAIL_DOMAIN.endswith(".invalid")


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


def test_the_urllib_transport_reports_a_connection_failure_as_status_zero(monkeypatch):
    transport = smoke.UrllibTransport("https://staging.example.test")

    def refuse(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(transport._opener, "open", refuse)
    assert transport.request("GET", "/healthz").status == 0


def test_requests_through_ngrok_skip_the_browser_warning_page_and_other_hosts_do_not_get_the_header():
    assert smoke.UrllibTransport("https://abc-123.ngrok-free.dev")._extra == {"ngrok-skip-browser-warning": "1"}
    assert smoke.UrllibTransport("https://staging-abc.a.run.app")._extra == {}
