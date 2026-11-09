"""測試輔助本身也要有測試：假供應商必須與真實轉接器的行為一致，否則用它寫的所有業務測試都不可信。"""
import pytest

from app.llm.types import (
    FINISH_LENGTH, LLMBlockedError, LLMConfigError, LLMContent, LLMProvider, LLMRequest, LLMTransientError, BLOCKED_INPUT,
)
from tests.support.llm import FakeLLMProvider, FakeMonotonic, Step


def request(model="m", timeout=20.0) -> LLMRequest:
    return LLMRequest(
        model=model, system_instruction="指令", contents=(LLMContent("user", ("你好",)),), response_schema=None,
        timeout_seconds=timeout, max_output_tokens=4096,
    )


def test_it_satisfies_the_provider_interface_and_replies_with_the_requested_model():
    provider: LLMProvider = FakeLLMProvider().script("{}")
    response = provider.generate(request(model="gemini-x"))
    assert response.text == "{}" and response.model == "gemini-x" and provider.name == "fake"
    assert (response.prompt_tokens, response.completion_tokens, response.thinking_tokens) == (100, 50, None)


def test_every_call_is_recorded_and_the_script_is_consumed_in_order():
    provider = FakeLLMProvider().script("一", "二")
    provider.generate(request("a"))
    provider.generate(request("b"))
    assert [c.model for c in provider.calls] == ["a", "b"] and provider.remaining == 0


def test_an_extra_call_beyond_the_script_fails_loudly():
    provider = FakeLLMProvider().script("一")
    provider.generate(request())
    with pytest.raises(AssertionError, match="腳本已用完"):
        provider.generate(request())


def test_a_step_slower_than_the_request_timeout_becomes_a_timeout_error_and_costs_exactly_the_timeout():
    clock = FakeMonotonic(100.0)
    provider = FakeLLMProvider(clock).script(Step("太慢", seconds=25))
    with pytest.raises(LLMTransientError) as raised:
        provider.generate(request(timeout=20))
    assert raised.value.kind == "timeout" and raised.value.latency_ms == 20000
    assert clock.now == 120.0                                               # 等到逾時就放棄，不會等滿 25 秒


def test_a_fast_step_advances_the_clock_by_its_own_duration():
    clock = FakeMonotonic(0.0)
    FakeLLMProvider(clock).script(Step("快", seconds=2.5)).generate(request(timeout=20))
    assert clock.now == 2.5


def test_scripted_errors_are_raised_with_the_latency_of_the_step():
    provider = FakeLLMProvider().script(
        Step(LLMTransientError("rate_limited"), seconds=0.3), Step(LLMBlockedError(BLOCKED_INPUT), seconds=1),
        Step(LLMConfigError(http_status=403), seconds=0.5),
    )
    with pytest.raises(LLMTransientError) as first:
        provider.generate(request())
    assert (first.value.kind, first.value.latency_ms) == ("rate_limited", 300)
    with pytest.raises(LLMBlockedError) as second:
        provider.generate(request())
    assert second.value.phase == "input" and second.value.latency_ms == 1000
    with pytest.raises(LLMConfigError) as third:
        provider.generate(request())
    assert third.value.http_status == 403


def test_a_callable_step_sees_the_request_and_a_dict_result_is_serialised_as_json():
    provider = FakeLLMProvider().script(Step(lambda req: {"echo": req.contents[0].parts[0]}))
    assert provider.generate(request()).text == '{"echo": "你好"}'


def test_hooks_run_before_every_call_so_tests_can_assert_the_state_at_call_time():
    seen = []
    provider = FakeLLMProvider().script("a", "b")
    provider.hooks.append(lambda req: seen.append(req.model))
    provider.generate(request("x"))
    provider.generate(request("y"))
    assert seen == ["x", "y"]


def test_finish_reason_and_token_counts_can_be_scripted():
    response = FakeLLMProvider().script(Step("{", finish_reason=FINISH_LENGTH, thinking_tokens=7, completion_tokens=None)).generate(request())
    assert (response.finish_reason, response.thinking_tokens, response.completion_tokens) == ("length", 7, None)


# ---- 串流（rpg-turn-engine 2.4）-----------------------------------------------------------------

def test_a_stream_script_is_consumed_in_order_advances_the_clock_and_ends_with_the_requested_model():
    from app.llm.types import StreamEnd, StreamText, StreamTick, StreamingLLMProvider
    from tests.support.llm import Chunk, Finish, Idle

    clock = FakeMonotonic()
    provider: StreamingLLMProvider = FakeLLMProvider(clock).script_stream(Chunk("甲", 0.5), Idle(2.0), Finish(completion_tokens=9, seconds=0.25))
    events = list(provider.stream(request("gemini-x")))
    assert events == [
        StreamText("甲"), StreamTick(), StreamEnd(finish_reason="stop", model="gemini-x", prompt_tokens=100, completion_tokens=9),
    ]
    assert clock.now == 1000.0 + 0.5 + 2.0 + 0.25 and provider.remaining_streams == 0
    assert [c.model for c in provider.calls] == ["gemini-x"]


def test_a_scripted_stream_failure_is_raised_in_place_with_its_latency_and_close_stops_the_stream():
    from app.llm.types import StreamText
    from tests.support.llm import Chunk, Fail, Finish

    provider = FakeLLMProvider().script_stream(Chunk("甲"), Fail(LLMTransientError("network"), seconds=0.7), Finish())
    iterator = iter(provider.stream(request()))
    assert next(iterator) == StreamText("甲")
    with pytest.raises(LLMTransientError) as caught:
        next(iterator)
    assert caught.value.latency_ms == 700

    handle = FakeLLMProvider().script_stream(Chunk("甲"), Chunk("乙"), Finish())
    handle_stream = handle.stream(request())
    iterator = iter(handle_stream)
    next(iterator)
    handle_stream.close()
    assert list(iterator) == [] and handle.handles[0].closed and handle.handles[0].served == 1


def test_a_stream_script_that_runs_out_without_a_finish_is_a_dropped_connection_and_extra_calls_are_a_test_bug():
    from tests.support.llm import Chunk

    provider = FakeLLMProvider().script_stream(Chunk("甲"))
    with pytest.raises(LLMTransientError) as caught:
        list(provider.stream(request()))
    assert caught.value.kind == "network"
    with pytest.raises(AssertionError):
        provider.stream(request())
