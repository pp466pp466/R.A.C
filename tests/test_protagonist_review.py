"""rpg-protagonist 規格「主角文字須通過規則層與模型層審查」的模型層：假供應商、真實的 `LlmService`、帳本與資料庫。

不打真實 API。涵蓋：四種結果、四種不可用、帳本類別、提示詞的位置、日誌只含列舉與數字。
"""
import json
import logging

import pytest
from sqlalchemy import select

from app.llm.types import LLMBlockedError, LLMConfigError, LLMTransientError
from app.models import LlmCallLog
from app.models.script import REVIEW_FLAG, REVIEW_PASS, REVIEW_REJECT
from app.observability import protagonist_events
from app.services.protagonist_prompt import protagonist_marker
from app.services.protagonist_review_service import ProtagonistReviewService
from app.services.script_review_service import UNAVAILABLE_BLOCKED, UNAVAILABLE_BUDGET, UNAVAILABLE_DEGRADED, UNAVAILABLE_NOT_CONFIGURED
from tests.support.auth import add_user
from tests.support.llm import FakeLLMProvider, Step
from tests.support.script_data import validated
from tests.test_llm_service import build

SECRET = "test-secret-key-for-protagonist-review-000"
MARKER = protagonist_marker(SECRET)
UNIQUE_NAME = "獨特的主角姓名PROTAGONIST-SECRET"
UNIQUE_FACT = "獨特的聯繫文字LINK-SECRET"


def reply(verdict="pass", categories=()):
    return json.dumps({"verdict": verdict, "categories": list(categories)}, ensure_ascii=False)


def make_service(session, provider, *, llm=True, exhausted=False):
    return ProtagonistReviewService(llm=build(session, provider) if llm else None, marker=MARKER, budget_exhausted=lambda: exhausted)


def stored(**changes):
    character = {
        "name": UNIQUE_NAME, "personality": "沉默愛思考", "age": 19, "appearance": "高瘦", "occupation": None, "speech_style": None,
        "hidden_trait": None, "image": None,
    }
    character.update(changes)
    return {"character": character, "anchor": {"kind": "character", "ref": "林曉", "text": UNIQUE_FACT}}


SCRIPT = validated().content


def ledger(session):
    session.expire_all()
    return list(session.scalars(select(LlmCallLog).order_by(LlmCallLog.id)))


@pytest.mark.parametrize(("verdict", "categories"), [
    (REVIEW_PASS, ()), (REVIEW_FLAG, ("tier_mismatch",)), (REVIEW_FLAG, ("minor_suspected", "other_concern")), (REVIEW_REJECT, ("minor_sexual",)),
])
def test_each_valid_verdict_is_returned_and_the_call_is_booked_as_protagonist_review(member_session, verdict, categories):
    user = add_user(member_session, 1)
    provider = FakeLLMProvider().script(reply(verdict, categories))
    result = make_service(member_session, provider).review(user.id, stored(), SCRIPT, "standard")
    assert (result.verdict, result.categories, result.unavailable) == (verdict, tuple(categories), None)
    [row] = ledger(member_session)
    assert (row.kind, row.user_id, row.status, row.attempt, row.conversation_id) == ("protagonist_review", user.id, "ok", 1, None)
    assert row.model and row.prompt_tokens is not None


def test_the_request_has_the_instruction_with_the_marker_and_the_card_only_in_the_user_content(member_session):
    user = add_user(member_session, 1)
    provider = FakeLLMProvider().script(reply())
    make_service(member_session, provider).review(user.id, stored(), SCRIPT, "adult")
    [request] = provider.calls
    assert MARKER in request.system_instruction and UNIQUE_NAME not in request.system_instruction and UNIQUE_FACT not in request.system_instruction
    [content] = request.contents
    assert content.role == "user" and UNIQUE_NAME in content.parts[0] and UNIQUE_FACT in content.parts[0] and "adult" in content.parts[0]
    assert request.response_schema is not None and set(request.response_schema["properties"]) == {"verdict", "categories"}


def test_an_invalid_answer_is_retried_once_and_a_second_invalid_answer_makes_the_review_unavailable(member_session):
    user = add_user(member_session, 1)
    provider = FakeLLMProvider().script("不是 JSON", reply("flag", []))
    result = make_service(member_session, provider).review(user.id, stored(), SCRIPT, "safe")
    assert (result.verdict, result.unavailable) == (None, UNAVAILABLE_DEGRADED)
    assert [row.status for row in ledger(member_session)] == ["validation_failed", "validation_failed"]


def test_a_marker_in_the_output_is_a_validation_failure_not_a_verdict(member_session):
    user = add_user(member_session, 1)
    leaked = json.dumps({"verdict": "flag", "categories": ["other_concern"], "x": MARKER})
    provider = FakeLLMProvider().script(leaked, reply("pass"))
    result = make_service(member_session, provider).review(user.id, stored(), SCRIPT, "safe")
    assert result.verdict == REVIEW_PASS and len(provider.calls) == 2


def test_without_an_rpg_provider_the_review_is_unavailable(member_session):
    user = add_user(member_session, 1)
    result = make_service(member_session, None, llm=False).review(user.id, stored(), SCRIPT, "safe")
    assert (result.verdict, result.unavailable) == (None, UNAVAILABLE_NOT_CONFIGURED)


def test_when_the_global_budget_is_used_up_the_model_is_not_called(member_session):
    user = add_user(member_session, 1)
    provider = FakeLLMProvider()
    result = make_service(member_session, provider, exhausted=True).review(user.id, stored(), SCRIPT, "safe")
    assert result.unavailable == UNAVAILABLE_BUDGET and provider.calls == [] and ledger(member_session) == []


@pytest.mark.parametrize(("steps", "reason"), [
    ([LLMTransientError("timeout"), LLMTransientError("server_error")], UNAVAILABLE_DEGRADED),
    ([LLMConfigError(http_status=400)], UNAVAILABLE_DEGRADED),
    ([LLMBlockedError("input")], UNAVAILABLE_BLOCKED),
    ([LLMBlockedError("output")], UNAVAILABLE_BLOCKED),
])
def test_provider_failures_make_the_review_unavailable_and_never_pass(member_session, steps, reason):
    user = add_user(member_session, 1)
    provider = FakeLLMProvider().script(*[Step(step) for step in steps])
    result = make_service(member_session, provider).review(user.id, stored(), SCRIPT, "safe")
    assert (result.verdict, result.unavailable) == (None, reason)


def test_the_primary_failing_with_a_transient_error_falls_back_and_can_still_succeed(member_session):
    user = add_user(member_session, 1)
    provider = FakeLLMProvider().script(Step(LLMTransientError("server_error")), reply("flag", ["other_concern"]))
    result = make_service(member_session, provider).review(user.id, stored(), SCRIPT, "safe")
    assert result.verdict == REVIEW_FLAG and provider.calls[0].model != provider.calls[1].model


def test_the_ledger_never_holds_the_protagonist_text(member_session):
    user = add_user(member_session, 1)
    make_service(member_session, FakeLLMProvider().script(reply("flag", ["tier_mismatch"]))).review(user.id, stored(), SCRIPT, "safe")
    for row in ledger(member_session):
        dumped = json.dumps({column.name: str(getattr(row, column.name)) for column in row.__table__.columns}, ensure_ascii=False)
        assert UNIQUE_NAME not in dumped and UNIQUE_FACT not in dumped


def test_logs_carry_only_enumerations_and_numbers(member_session, caplog):
    user = add_user(member_session, 1)
    allowed = {"event", "user_id", "model", "attempt", "outcome", "reason"}
    with caplog.at_level(logging.INFO, logger="app.protagonist"):
        make_service(member_session, FakeLLMProvider().script(reply("reject", ["minor_sexual"]))).review(user.id, stored(), SCRIPT, "safe")
        make_service(member_session, None, llm=False).review(user.id, stored(), SCRIPT, "safe")
    own = [record for record in caplog.records if record.name == "app.protagonist"]            # 其他記錄器（如 app.llm）的事件不在這個檢查範圍
    events = [{key: value for key, value in vars(record).items() if key in allowed} for record in own]
    assert {event["event"] for event in events} == {protagonist_events.EVENT_REVIEW, protagonist_events.EVENT_REVIEW_UNAVAILABLE}
    text = " ".join(f"{record.getMessage()} {vars(record)}" for record in caplog.records)             # 所有記錄器都不得洩漏主角文字
    assert UNIQUE_NAME not in text and UNIQUE_FACT not in text
    assert next(e for e in events if e["event"] == protagonist_events.EVENT_REVIEW)["outcome"] == "reject"
