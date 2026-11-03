"""load-testing 規格：假模型入口只在開發與測試載入、錯誤率設定生效、不在映像內、虛擬會員的完整流程、位址守門。"""
import os
import re
import subprocess
import sys

import pytest

from app import create_app
from app.config import PROJECT_ROOT, load_config
from app.llm.types import LLMRequest, LLMTransientError
from loadtest import actions, fake_app, guard, provider
from loadtest.provider import LoadTestConfigError, LoadTestProvider, parse_error_rates
from tests.support.client import raw_client


def request(timeout=20.0, system="你是客服"):
    return LLMRequest(model="m", system_instruction=system, contents=(), response_schema=None, timeout_seconds=timeout, max_output_tokens=100)


# ---- 假模型入口只在開發與測試載入 -------------------------------------------------------


@pytest.mark.parametrize("env", ["production", "staging", "", "something-else"])
def test_the_fake_entry_refuses_to_load_outside_development_and_testing(env):
    environ = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    environ.pop("APP_ENV", None)
    if env:
        environ["APP_ENV"] = env
    result = subprocess.run([sys.executable, "-c", "import loadtest.fake_app"], cwd=PROJECT_ROOT, env=environ, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert "不可用於預備或正式環境" in result.stderr, result.stderr[-300:]


@pytest.mark.parametrize("env", ["testing", "development"])
def test_the_guard_accepts_the_two_local_environments(env):
    assert fake_app.guard_environment({"APP_ENV": env}) == env


def test_build_app_injects_the_load_test_provider_and_creates_the_tables(monkeypatch):
    monkeypatch.setenv("LOADTEST_LATENCY_SECONDS", "0")
    environ = {"APP_ENV": "testing", "LOADTEST_LATENCY_SECONDS": "0", "LOADTEST_JITTER_SECONDS": "0"}
    app = fake_app.build_app(environ)
    assert isinstance(app.extensions["llm_provider"], LoadTestProvider)
    from sqlalchemy import inspect

    assert "users" in inspect(app.extensions["db_engine"]).get_table_names()
    assert "EMAIL_VERIFICATION_REQUIRED" not in environ


def test_the_flask_factory_name_is_make_app():
    assert callable(fake_app.make_app)


# ---- 假供應商：延遲與錯誤率 -------------------------------------------------------------


def test_error_rate_parsing_accepts_valid_specs_and_rejects_bad_ones():
    assert parse_error_rates("rate_limited=0.02, timeout=0.01,server_error=0.005") == {"rate_limited": 0.02, "timeout": 0.01, "server_error": 0.005}
    assert parse_error_rates("") == {} and parse_error_rates(None) == {}
    for bad in ("nonsense=0.1", "timeout=abc", "timeout=1.5", "timeout=-0.1", "timeout=0.6,server_error=0.6"):
        with pytest.raises(LoadTestConfigError):
            parse_error_rates(bad)


def test_a_ten_percent_timeout_rate_gives_about_ten_percent_timeouts_and_the_rest_succeed_after_the_configured_delay():
    slept: list[float] = []
    fake = LoadTestProvider(latency_seconds=2.0, jitter_seconds=0.0, error_rates={"timeout": 0.10}, seed=7, sleep=slept.append)
    outcomes = []
    for _ in range(5000):
        try:
            outcomes.append(fake.generate(request(timeout=20.0)).latency_ms)
        except LLMTransientError as error:
            outcomes.append(error.kind)
    timeouts = outcomes.count("timeout")
    assert 400 <= timeouts <= 600, timeouts
    assert fake.errors["timeout"] == timeouts and fake.calls == 5000
    assert set(o for o in outcomes if o != "timeout") == {2000}, "沒有錯誤的呼叫延遲就是設定的 2 秒"
    assert slept.count(20.0) == timeouts and slept.count(2.0) == 5000 - timeouts, "逾時睡到請求的逾時秒數，其餘睡設定的延遲"


def test_each_error_kind_is_raised_with_the_matching_status():
    for kind, status in (("rate_limited", 429), ("server_error", 500)):
        fake = LoadTestProvider(latency_seconds=0, jitter_seconds=0, error_rates={kind: 1.0}, seed=1, sleep=lambda s: None)
        with pytest.raises(LLMTransientError) as error:
            fake.generate(request())
        assert error.value.kind == kind and error.value.http_status == status


def test_the_jitter_keeps_the_delay_within_the_configured_band():
    slept: list[float] = []
    fake = LoadTestProvider(latency_seconds=1.0, jitter_seconds=0.4, seed=3, sleep=slept.append)
    for _ in range(500):
        fake.generate(request())
    assert min(slept) >= 0.6 and max(slept) <= 1.4 and len(set(slept)) > 100


def test_summary_requests_get_a_summary_and_chat_requests_get_a_valid_reply():
    import json

    fake = LoadTestProvider(latency_seconds=0, jitter_seconds=0, sleep=lambda s: None)
    assert "summary" in json.loads(fake.generate(request(system="你負責維護一段客服對話的累積摘要")).text)
    reply = json.loads(fake.generate(request()).text)
    assert {"intent", "confidence", "entities", "reply_text", "suggested_actions"} <= set(reply)


def test_the_provider_makes_no_network_call_and_has_no_secret(monkeypatch):
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("假供應商不得連線")

    monkeypatch.setattr(socket, "create_connection", refuse)
    LoadTestProvider(latency_seconds=0, jitter_seconds=0, sleep=lambda s: None).generate(request())


def test_the_provider_reads_its_settings_from_the_environment():
    fake = provider.provider_from_environment({"LOADTEST_LATENCY_SECONDS": "0.5", "LOADTEST_ERROR_RATES": "timeout=0.5", "LOADTEST_SEED": "9"})
    assert fake._latency == 0.5 and fake._rates == {"timeout": 0.5}
    with pytest.raises(LoadTestConfigError):
        provider.provider_from_environment({"LOADTEST_LATENCY_SECONDS": "fast"})


# ---- 不在映像內 -------------------------------------------------------------------------


def test_the_image_never_contains_the_load_test_files_or_locust():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8-sig")
    assert not re.search(r"^\s*COPY\s+.*loadtest", dockerfile, re.M) and "locust" not in dockerfile.lower()
    ignored = [line.strip() for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8-sig").splitlines()]
    assert "loadtest" in ignored
    assert "locust" not in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8-sig").lower()
    dev = (PROJECT_ROOT / "requirements-dev.txt").read_text(encoding="utf-8-sig")
    assert re.search(r"^locust==\d+\.\d+\.\d+$", dev, re.M), "Locust 只在開發相依，且釘選版本"


def test_nothing_in_the_application_package_imports_the_load_test_code():
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        assert "loadtest" not in path.read_text(encoding="utf-8-sig"), path.name


# ---- 虛擬會員的流程（以 Flask 測試客戶端驅動同一組動作）--------------------------------------


class FlaskSession:
    origin = "http://localhost"

    def __init__(self, client):
        self.client = client

    def set_cookie(self, name, value):
        self.client.set_cookie(name, value)

    def _reply(self, response):
        body = response.get_json(silent=True) if response.mimetype == "application/json" else None
        return actions.Reply(response.status_code, body, "" if body is not None else response.get_data(as_text=True))

    def get(self, path, *, name=None):
        return self._reply(self.client.get(path))

    def post(self, path, *, json, headers, name=None, fail_if=None):
        reply = self._reply(self.client.post(path, json=json, headers=headers))
        self.problems = getattr(self, "problems", [])
        if fail_if is not None and fail_if(reply):
            self.problems.append(fail_if(reply))
        return reply


class MailSpy:
    def __init__(self):
        self.sent = []

    def send(self, *, to, subject, body):
        self.sent.append(to)


@pytest.fixture()
def world():
    spy = MailSpy()
    app = create_app(
        load_config(environ={"APP_ENV": "testing"}), mail_sender=spy,
        llm_provider=LoadTestProvider(latency_seconds=0, jitter_seconds=0, sleep=lambda s: None),
    )
    from app.models import Base

    Base.metadata.create_all(app.extensions["db_engine"])
    return app, spy


def member(app, number=1, run="t1"):
    return actions.Member(FlaskSession(raw_client(app)), run_id=run, number=number)


def mint_pool(app, count=2, prefix="loadtest-t1"):
    """與 scripts/mint_test_sessions.py 相同的做法：建立測試玩家並以該環境自己的簽章器簽出 cookie。"""
    from sqlalchemy.orm import Session

    from tests.support.auth import add_user

    values = []
    with Session(app.extensions["db_engine"]) as s:
        for n in range(1, count + 1):
            email = f"{prefix}-{n}@loadtest.invalid"
            user = add_user(s, n, email=email, email_canonical=email, password=None)
            values.append(app.session_interface.get_signing_serializer(app).dumps({"uid": user.id, "tv": user.token_version}))
    return app.config["SESSION_COOKIE_NAME"], values


def sign_in(app, m, number=1):
    name, values = mint_pool(app, number)
    return m.sign_in(name, values[number - 1])


def test_one_virtual_member_signs_in_with_a_minted_cookie_opens_a_conversation_and_chats(world):
    app, spy = world
    m = member(app)
    assert sign_in(app, m) and m.open_conversation()
    assert m.conversation_id
    reply = m.send_message()
    assert reply.ok and reply.body["assistant_message"]["seq"] >= 2 and m.last_seq >= 2
    assert m.list_conversations().ok
    pulled = m.pull_since()
    assert pulled.ok and pulled.body is not None
    assert spy.sent == [], "玩家端不寄任何信"


def test_a_cookie_the_environment_did_not_sign_is_not_a_login(world):
    app, _ = world
    m = member(app)
    assert m.sign_in(app.config["SESSION_COOKIE_NAME"], "not-a-valid-cookie") is False


def test_every_virtual_member_uses_its_own_reserved_domain_account_and_its_own_conversation(world):
    app, _ = world
    name, values = mint_pool(app, 2)
    first, second = member(app, 1), member(app, 2)
    for m, value in ((first, values[0]), (second, values[1])):
        assert m.sign_in(name, value) and m.open_conversation()
    assert first.conversation_id != second.conversation_id
    assert first.send_message().ok and second.send_message().ok
    me = first.session.get("/api/auth/me").body["user"]
    assert me["email"] == "loadtest-t1-1@loadtest.invalid" and me["email"].endswith("@" + guard.TEST_EMAIL_DOMAIN)


def test_the_requests_pass_the_csrf_check_and_would_be_rejected_without_the_token_and_origin(world):
    app, _ = world
    m = member(app)
    assert sign_in(app, m)
    no_token = m.session.post("/api/cs/conversations", json={"client_id": "x"}, headers={})
    assert 400 <= no_token.status < 500
    assert m.open_conversation(), "帶權杖與同源的 Origin 就通過"


def test_each_message_gets_a_new_client_id_so_the_seq_keeps_increasing(world):
    app, _ = world
    m = member(app)
    assert sign_in(app, m) and m.open_conversation()
    assert m.send_message().ok
    first = m.last_seq
    assert m.send_message().ok and m.last_seq > first


# ---- 預先簽出的登入狀態（loadtest/session_pool.py）---------------------------------------------------


def pool_file(tmp_path, count=3, name="__Host-session"):
    import json

    path = tmp_path / "pool.json"
    sessions = [{"email": f"p-{n}@loadtest.invalid", "value": f"v{n}"} for n in range(1, count + 1)]
    path.write_text(json.dumps({"cookie_name": name, "sessions": sessions}, ensure_ascii=False), encoding="utf-8")
    return path


def test_the_pool_hands_out_one_cookie_per_virtual_member_in_order(tmp_path):
    from loadtest.session_pool import load_pool

    pool = load_pool(pool_file(tmp_path))
    assert len(pool) == 3 and pool.cookie_name == "__Host-session"
    assert [pool.value_for(n) for n in (1, 2, 3)] == ["v1", "v2", "v3"]


def test_asking_for_more_members_than_cookies_fails_before_any_request_and_says_how_many_are_missing(tmp_path):
    from loadtest.session_pool import PoolError, load_pool, require

    pool = load_pool(pool_file(tmp_path, count=3))
    require(pool, 3)
    with pytest.raises(PoolError) as error:
        require(pool, 20)
    assert "20" in str(error.value) and "3" in str(error.value) and "17" in str(error.value)
    with pytest.raises(PoolError):
        pool.value_for(4)


@pytest.mark.parametrize("content", [
    "not json", "[]", '{"cookie_name": "x"}', '{"cookie_name": "", "sessions": [{"value": "v"}]}', '{"cookie_name": "x", "sessions": []}',
])
def test_a_malformed_pool_file_is_an_error_about_the_format(tmp_path, content):
    from loadtest.session_pool import PoolError, load_pool

    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(PoolError) as error:
        load_pool(path)
    assert "格式" in str(error.value)


def test_a_missing_pool_file_points_to_the_script_that_makes_it(tmp_path):
    from loadtest.session_pool import PoolError, load_pool

    with pytest.raises(PoolError) as missing:
        load_pool(tmp_path / "nope.json")
    assert "mint_test_sessions" in str(missing.value)


def test_the_member_module_no_longer_has_any_registration_or_password_login():
    text = (PROJECT_ROOT / "loadtest" / "actions.py").read_text(encoding="utf-8")
    assert "/api/auth/register" not in text and "/api/auth/login" not in text and "PASSWORD" not in text


# ---- 位址守門 ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["http://localhost:5000", "http://127.0.0.1:5000", "http://[::1]:5000", "127.0.0.1:8080"])
def test_local_addresses_are_allowed_by_default(host):
    assert guard.check_target(host) == "local"


@pytest.mark.parametrize("host", ["https://cs-web-staging-abc.a.run.app", "http://10.0.0.5:8080", "https://staging.example.com"])
def test_other_addresses_need_the_explicit_confirmation(host):
    with pytest.raises(guard.TargetRefused) as error:
        guard.check_target(host)
    assert "--confirm-remote" in str(error.value)
    assert guard.check_target(host, confirm_remote=True) == "remote"


@pytest.mark.parametrize("host", ["https://cs-web-production-abc.a.run.app", "https://production.example.com", "http://localhost-production:5000"])
def test_production_looking_addresses_are_refused_even_with_the_confirmation(host):
    with pytest.raises(guard.TargetRefused):
        guard.check_target(host, confirm_remote=True)


def test_the_documented_production_domains_are_refused_including_their_subdomains():
    domains = ("cs.example.org",)
    for host in ("https://cs.example.org", "https://www.cs.example.org/path", "https://CS.EXAMPLE.ORG"):
        with pytest.raises(guard.TargetRefused):
            guard.check_target(host, confirm_remote=True, production_domains=domains)
    assert guard.check_target("https://notcs.example.org", confirm_remote=True, production_domains=domains) == "remote"


@pytest.mark.parametrize("host", ["", "ftp://localhost", "http://"])
def test_invalid_targets_are_refused(host):
    with pytest.raises(guard.TargetRefused):
        guard.check_target(host, confirm_remote=True)


def test_the_test_accounts_use_the_reserved_invalid_domain():
    assert guard.TEST_EMAIL_DOMAIN == "loadtest.invalid"


GUARD_SCRIPT = """
from loadtest import locustfile

class Options:
    confirm_remote = False
    num_users = 0

class Environment:
    parsed_options = Options()
    def __init__(self, host):
        self.host = host

def refused(host):
    try:
        locustfile._guard_target(Environment(host))
    except SystemExit:
        return True
    return False

assert not refused("http://127.0.0.1:5000")
assert refused("https://staging.example.com") and refused("https://cs-web-production-x.a.run.app")
Options.confirm_remote = True
assert not refused("https://staging.example.com")
assert refused("https://cs-web-production-x.a.run.app")
# 預先簽出的登入狀態只有 3 組：虛擬使用者數不能超過它（在送出任何請求之前就失敗）
Options.num_users = 3
assert not refused("http://127.0.0.1:5000")
Options.num_users = 20
assert refused("http://127.0.0.1:5000")
print("ok")
"""


def test_the_locust_listener_enforces_the_guard_and_the_number_of_minted_sessions(tmp_path):
    """在子行程執行：匯入 locust 會對標準函式庫做 gevent 猴子補丁，不能發生在 pytest 行程裡（會影響其他測試）。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "LOADTEST_SESSIONS": str(pool_file(tmp_path, count=3))}
    result = subprocess.run([sys.executable, "-W", "ignore", "-c", GUARD_SCRIPT], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-400:]


def test_the_locust_scenario_stops_before_any_request_when_the_sessions_file_is_missing(tmp_path):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "LOADTEST_SESSIONS": str(tmp_path / "missing.json")}
    result = subprocess.run([sys.executable, "-W", "ignore", "-c", "from loadtest import locustfile"], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0 and "mint_test_sessions" in result.stderr


def test_the_test_process_never_imports_locust():
    assert "locust" not in sys.modules and "gevent" not in sys.modules


def test_a_degraded_reply_is_a_failure_even_though_the_http_status_is_200(world):
    """模型暫時性錯誤且備援也失敗時，客服回覆降級文案（HTTP 200）；負載測試的錯誤率不能漏掉它。"""
    from app.models import Base

    spy = MailSpy()
    broken = LoadTestProvider(latency_seconds=0, jitter_seconds=0, error_rates={"server_error": 1.0}, seed=1, sleep=lambda s: None)
    app = create_app(load_config(environ={"APP_ENV": "testing"}), mail_sender=spy, llm_provider=broken)
    Base.metadata.create_all(app.extensions["db_engine"])
    m = member(app)
    assert sign_in(app, m) and m.open_conversation()
    reply = m.send_message()
    assert reply.status == 200 and reply.body["assistant_message"]["msg_status"] == "fallback"
    assert m.degraded == 1 and m.session.problems == ["降級回覆（模型暫時性錯誤）"]


def test_a_normal_reply_is_not_reported_as_degraded(world):
    app, _ = world
    m = member(app)
    assert sign_in(app, m) and m.open_conversation()
    assert m.send_message().ok and m.degraded == 0 and getattr(m.session, "problems", []) == []
