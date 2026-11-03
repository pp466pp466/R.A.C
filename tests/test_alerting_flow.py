"""ops-alerting 端到端：用真正的客服對話與登入產生帳本與鎖定，手動推進時鐘，走完整條告警生命週期並掃描日誌。

正常 → 用量達 80%（寄信）→ 持續（不寄）→ 模型錯誤率與設定錯誤同時成立 → 5 個帳號被鎖定 → 用量達 100%（升級）→
隔天全部恢復（各寄「已恢復」）→ 寄信失敗後重試。供應商與寄信器都是假的；`check-alerts` 以指令實際執行。
"""
import json

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.llm.types import LLMConfigError, LLMTransientError
from app.models import Base
from app.observability.json_logging import ALLOWED_FIELDS
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user
from tests.support.config_env import AUTH_ENV, FAKE_SMTP_PASSWORD
from tests.support.idor import logged_in_client
from tests.support.llm import FakeLLMProvider, Step

pytestmark = pytest.mark.usefixtures("generous_free_limits")

RECIPIENT = "watch.me@zz-host.example"
SECRET_USER_TEXT = "我的私密訊息-SECRET-MSG-8812"
SECRET_REPLY = "回覆的私密字樣-SECRET-REPLY-9923"
SMTP_DETAIL = "secret-smtp-detail"
BASE_KEYS = {"time", "level", "logger", "message"}


class Sender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.fail = False

    def send(self, *, to: str, subject: str, body: str) -> None:
        if self.fail:
            raise ConnectionError(f"SMTP 無回應：{SMTP_DETAIL}")
        self.sent.append({"to": to, "subject": subject, "body": body})


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def reply(text: str = SECRET_REPLY) -> dict:
    return {
        "intent": "account_help", "confidence": 0.9,
        "entities": {"topic": None, "membership_tier": None, "account_issue": "forgot_password", "feedback_type": None},
        "reply_text": text, "suggested_actions": [],
    }


def test_the_whole_alert_lifecycle_with_real_traffic_and_no_leak(log_lines):
    clock, sender = FakeClock(), Sender()
    provider = FakeLLMProvider(default=Step(lambda request: {"summary": "摘要"} if "累積摘要" in request.system_instruction else reply(), prompt_tokens=200, completion_tokens=0))
    environ = {
        "APP_ENV": "testing", "USAGE_DAILY_TOKEN_LIMIT": "1000", "ALERT_EMAIL_TO": RECIPIENT,
        **{k: v for k, v in AUTH_ENV.items() if k.startswith("MAIL_")},
    }
    app = create_app(load_config(environ=environ), clock=clock, mail_sender=sender, llm_provider=provider)
    Base.metadata.create_all(app.extensions["db_engine"])
    with Session(app.extensions["db_engine"]) as s:
        chat_user = add_user(s, 1, age=30)
        s.refresh(chat_user)
        s.expunge(chat_user)
        for n in range(2, 12):
            add_user(s, n)
    client = logged_in_client(app, chat_user)
    runner = app.test_cli_runner()
    outputs: list[str] = []
    counter = iter(range(1, 10_000))

    def check():
        result = runner.invoke(args=["check-alerts"])
        outputs.append(result.output)
        return result

    def say(conversation: str, text: str = SECRET_USER_TEXT):
        response = client.post(f"/api/cs/conversations/{conversation}/messages", json={"client_msg_id": uid(next(counter)), "content": text})
        assert response.status_code == 200, response.get_data(as_text=True)
        return response

    def new_conversation() -> str:
        return client.post("/api/cs/conversations", json={"client_id": uid(next(counter))}).get_json()["conversation"]["id"]

    def lock(first: int, last: int) -> None:
        anonymous = app.test_client()
        for n in range(first, last + 1):
            for _ in range(5):
                assert anonymous.post("/api/auth/login", json={"email": f"user{n}@example.com", "password": "definitely-the-wrong-password"}).status_code == 401

    def subjects() -> list[str]:
        return [m["subject"] for m in sender.sent]

    # ---- 正常：沒有任何流量，什麼都不寄 ----
    first = check()
    assert first.exit_code == 0 and sender.sent == []

    # ---- 用量剛好 80%（4 輪各 200 token）：進入警告，寄一封 ----
    conversation = new_conversation()
    for _ in range(4):
        say(conversation)
    clock.advance(seconds=30)
    assert check().exit_code == 0
    assert subjects() == ["【智能客服】告警：全站用量（進入告警）"]
    assert "目前數值：80.0%（門檻：80%）" in sender.sent[0]["body"] and "嚴重度：警告" in sender.sent[0]["body"]

    # ---- 持續：不重複寄 ----
    clock.advance(minutes=5)
    assert check().exit_code == 0 and len(sender.sent) == 1

    # ---- 錯誤爆發：6 則訊息先被 429、再由備援回覆；另有 1 則遇到設定錯誤（金鑰被撤銷）----
    for _ in range(6):
        provider.script(Step(LLMTransientError("rate_limited"), seconds=0.1), Step(reply(), prompt_tokens=10, completion_tokens=0))
        say(conversation)
    provider.script(Step(LLMConfigError(http_status=403), seconds=0.1))
    say(conversation)
    clock.advance(seconds=30)
    assert check().exit_code == 0
    assert subjects()[1:] == ["【智能客服】告警：模型錯誤率（進入告警）", "【智能客服】告警：模型設定錯誤（進入告警）"]
    assert "嚴重度：嚴重" in sender.sent[2]["body"] and "金鑰" in sender.sent[2]["body"]

    # ---- 5 個帳號因連續登入失敗被鎖定 ----
    lock(2, 6)
    assert check().exit_code == 0
    assert subjects()[-1] == "【智能客服】告警：帳號鎖定激增（進入告警）" and len(sender.sent) == 4

    # ---- 用量越過 100%：升級 ----
    say(conversation)                                                     # 這一輪 200 token：860 → 1060
    assert check().exit_code == 0
    assert subjects()[-1] == "【智能客服】告警：全站用量（升級）" and "嚴重度：嚴重" in sender.sent[-1]["body"] and len(sender.sent) == 5

    # ---- 隔天：流量恢復正常、鎖定到期、錯誤的窗口已過 → 四項各寄一封「已恢復」----
    clock.advance(days=1)
    fresh = new_conversation()
    for _ in range(10):
        provider.script(Step(reply(), prompt_tokens=10, completion_tokens=0))
        say(fresh)
    clock.advance(seconds=30)
    assert check().exit_code == 0
    assert sorted(subjects()[5:]) == sorted([
        "【智能客服】告警：全站用量（已恢復）", "【智能客服】告警：模型錯誤率（已恢復）",
        "【智能客服】告警：模型設定錯誤（已恢復）", "【智能客服】告警：帳號鎖定激增（已恢復）",
    ])
    clock.advance(minutes=5)
    assert check().exit_code == 0 and len(sender.sent) == 9               # 恢復之後不再寄

    # ---- 寄信失敗：指令以非零代碼結束，下次檢查重試 ----
    lock(7, 11)
    sender.fail = True
    failed = check()
    assert failed.exit_code != 0 and "以下告警失敗：帳號鎖定激增" in failed.output and len(sender.sent) == 9
    sender.fail = False
    clock.advance(minutes=5)
    assert check().exit_code == 0
    assert subjects()[-1] == "【智能客服】告警：帳號鎖定激增（進入告警）" and len(sender.sent) == 10

    # ---- 掃描：日誌、指令輸出與信件內容 ----
    assert {m["to"] for m in sender.sent} == {RECIPIENT}
    raw = log_lines.raw() + "".join(outputs) + "".join(m["subject"] + m["body"] for m in sender.sent)
    for secret in (
        RECIPIENT, "zz-host.example", SMTP_DETAIL, FAKE_SMTP_PASSWORD, GOOD_PASSWORD, SECRET_USER_TEXT, SECRET_REPLY,
        "user1@example.com", "使用者1", "definitely-the-wrong-password",
    ):
        assert secret not in raw, f"洩漏了：{secret[:16]}…"
    lines = log_lines()
    for line in lines:
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"
    events = [line for line in lines if line.get("event", "").startswith("alert.")]
    assert {e["event"] for e in events} == {"alert.firing", "alert.resolved", "alert.delivery_failed", "alert.check_completed"}
    assert sum(e["event"] == "alert.firing" for e in events) == 6 and sum(e["event"] == "alert.resolved" for e in events) == 4
    heartbeats = [e for e in events if e["event"] == "alert.check_completed"]
    assert len(heartbeats) == len(outputs) and all(not {"reason", "severity", "count"} & set(e) for e in heartbeats)   # 每次檢查都跑完（寄信失敗那次也是）
    assert all(e["reason"] in {"usage_budget", "llm_error_rate", "llm_config_error", "login_lockouts"} for e in events if e not in heartbeats)
    json.dumps(lines)
