"""cs-nlp-contract 規格：意圖與實體的單一來源、輸出結構與長度驗證、低信心改寫。"""
import copy
import json

import pytest

from app.llm.types import LLMValidationError
from app.services import cs_intents
from app.services.cs_intents import (
    ENTITY_ENUMS, ENTITY_FIELDS, INTENTS, LOW_CONFIDENCE_TEXT, MAX_REPLY_CHARS, RESPONSE_SCHEMA, build_response_schema,
    semantic_rules, to_reply,
)
from app.services.llm_output import parse_llm_json

MARKER = "0123456789abcdef01234567"


def reply(**overrides):
    base = {
        "intent": "account_help",
        "confidence": 0.9,
        "entities": {"topic": "忘記密碼", "membership_tier": None, "account_issue": "forgot_password", "feedback_type": None},
        "reply_text": "您可以到登入頁點選「忘記密碼」，輸入註冊時的信箱，系統會寄出重設連結。",
        "suggested_actions": [{"type": "quick_reply", "label": "收不到信", "text": "我收不到重設密碼的信"}],
    }
    base.update(overrides)
    return base


def parse(payload, marker=MARKER):
    return parse_llm_json(json.dumps(payload, ensure_ascii=False), RESPONSE_SCHEMA, semantic_rules(marker))


def reason_of(payload, marker=MARKER):
    with pytest.raises(LLMValidationError) as raised:
        parse(payload, marker)
    return raised.value.reason


# ---- 意圖與實體的固定集合 ----------------------------------------------------------------


def test_there_are_exactly_the_seven_documented_intents():
    assert set(INTENTS) == {
        "greeting", "general_qa", "membership_info", "account_help", "complaint_feedback", "human_handoff", "unknown",
    }
    assert len(INTENTS) == len(set(INTENTS)) == 7


def test_the_four_entities_and_their_enumerations_are_fixed():
    assert ENTITY_FIELDS == ("topic", "membership_tier", "account_issue", "feedback_type")
    assert ENTITY_ENUMS == {
        "membership_tier": ("free", "plus", "vip"),
        "account_issue": ("forgot_password", "email_verification", "cannot_login", "google_login", "other"),
        "feedback_type": ("complaint", "suggestion", "bug_report"),
    }


def test_the_schema_enums_are_built_from_the_constants():
    properties = RESPONSE_SCHEMA["properties"]
    assert properties["intent"]["enum"] == list(INTENTS)
    entity_props = properties["entities"]["properties"]
    assert {field: entity_props[field]["enum"] for field in ENTITY_ENUMS} == {f: list(v) for f, v in ENTITY_ENUMS.items()}
    assert RESPONSE_SCHEMA["required"] == ["intent", "confidence", "entities", "reply_text", "suggested_actions"]
    assert properties["entities"]["required"] == list(ENTITY_FIELDS)


def test_every_accepted_intent_and_entity_value_passes_and_nothing_else_does():
    for intent in INTENTS:
        assert parse(reply(intent=intent))["intent"] == intent
    for field, values in ENTITY_ENUMS.items():
        for value in values:
            assert parse(reply(entities={**reply()["entities"], field: value}))["entities"][field] == value
    assert reason_of(reply(intent="billing")) == "schema:$.intent:enum"


def test_adding_an_intent_means_changing_one_constant(monkeypatch):
    monkeypatch.setattr(cs_intents, "INTENTS", INTENTS + ("billing",))
    assert "billing" in build_response_schema()["properties"]["intent"]["enum"]


def test_the_module_level_schema_is_not_mutated_by_validation():
    before = copy.deepcopy(RESPONSE_SCHEMA)
    parse(reply())
    assert RESPONSE_SCHEMA == before


# ---- 結構 ---------------------------------------------------------------------------------


def test_a_complete_reply_passes():
    assert parse(reply())["intent"] == "account_help"


@pytest.mark.parametrize("missing", ["intent", "confidence", "entities", "reply_text", "suggested_actions"])
def test_a_missing_top_level_field_fails(missing):
    payload = reply()
    del payload[missing]
    assert reason_of(payload) == f"schema:$.{missing}:required"


def test_an_extra_top_level_field_fails():
    assert reason_of({**reply(), "debug": "x"}) == "schema:$:additional_property"


def test_a_greeting_has_all_four_entity_fields_present_and_null():
    entities = {field: None for field in ENTITY_FIELDS}
    assert parse(reply(intent="greeting", entities=entities, reply_text="您好！歡迎使用客服。", suggested_actions=[]))["entities"] == entities


@pytest.mark.parametrize("field", ENTITY_FIELDS)
def test_a_missing_entity_field_fails(field):
    entities = {k: v for k, v in reply()["entities"].items() if k != field}
    assert reason_of(reply(entities=entities)) == f"schema:$.entities.{field}:required"


def test_an_extra_entity_field_fails():
    assert reason_of(reply(entities={**reply()["entities"], "order_id": "A1"})) == "schema:$.entities:additional_property"


def test_an_entity_enum_value_outside_the_set_fails():
    assert reason_of(reply(entities={**reply()["entities"], "account_issue": "lost_phone"})) == "schema:$.entities.account_issue:enum"
    assert reason_of(reply(entities={**reply()["entities"], "membership_tier": "gold"})) == "schema:$.entities.membership_tier:enum"


def test_the_topic_is_at_most_fifty_characters():
    assert parse(reply(entities={**reply()["entities"], "topic": "題" * 50}))
    assert reason_of(reply(entities={**reply()["entities"], "topic": "題" * 51})) == "schema:$.entities.topic:maxLength"


@pytest.mark.parametrize("confidence", [-0.01, 1.01, "0.9", None, True])
def test_confidence_must_be_a_number_from_zero_to_one(confidence):
    assert reason_of(reply(confidence=confidence)).startswith("schema:$.confidence:")


@pytest.mark.parametrize("confidence", [0, 0.0, 0.6, 1, 1.0])
def test_the_confidence_range_is_inclusive(confidence):
    assert parse(reply(confidence=confidence))


# ---- 回覆文字 ------------------------------------------------------------------------------


def test_a_reply_with_only_english_and_punctuation_fails():
    assert reason_of(reply(reply_text="Please reset your password via the login page!")) == "semantic:reply_text_no_cjk"


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_an_empty_or_blank_reply_fails(text):
    assert reason_of(reply(reply_text=text)) == "semantic:reply_text_blank"


@pytest.mark.parametrize("text", ["こんにちは、パスワードをお忘れですか", "비밀번호를 잊으셨나요", "Hello 你好", "請 reset 密碼", "漢"])
def test_any_chinese_japanese_or_korean_character_counts(text):
    assert parse(reply(reply_text=text))


@pytest.mark.parametrize("text", ["😀😀", "12345 !!!", "ÀÉÎõü"])
def test_emoji_digits_and_latin_letters_do_not_count_as_chinese(text):
    assert reason_of(reply(reply_text=text)) == "semantic:reply_text_no_cjk"


def test_exactly_one_thousand_characters_pass_and_one_more_fails():
    assert len(parse(reply(reply_text="客" * MAX_REPLY_CHARS))["reply_text"]) == 1000
    assert reason_of(reply(reply_text="客" * (MAX_REPLY_CHARS + 1))) == "schema:$.reply_text:maxLength"


@pytest.mark.parametrize("length", [4, 40, 200, 349, 350, 999])
def test_short_replies_are_valid_because_there_is_no_lower_bound_for_customer_service_replies(length):
    assert parse(reply(reply_text="答" * length))["reply_text"] == "答" * length


# ---- 建議動作 ------------------------------------------------------------------------------


def action(label="按鈕", text="這是一段文字"):
    return {"type": "quick_reply", "label": label, "text": text}


def test_up_to_four_suggested_actions_pass_including_none():
    assert parse(reply(suggested_actions=[]))
    assert parse(reply(suggested_actions=[action(f"動作{i}") for i in range(4)]))


def test_five_suggested_actions_fail():
    assert reason_of(reply(suggested_actions=[action() for _ in range(5)])) == "schema:$.suggested_actions:maxItems"


def test_label_and_text_have_their_own_limits():
    assert parse(reply(suggested_actions=[action("標" * 12, "文" * 50)]))
    assert reason_of(reply(suggested_actions=[action("標" * 13)])) == "schema:$.suggested_actions[0].label:maxLength"
    assert reason_of(reply(suggested_actions=[action(text="文" * 51)])) == "schema:$.suggested_actions[0].text:maxLength"


def test_only_the_quick_reply_type_exists():
    assert reason_of(reply(suggested_actions=[{**action(), "type": "link"}])) == "schema:$.suggested_actions[0].type:enum"


def test_an_action_with_a_missing_or_extra_field_fails():
    assert reason_of(reply(suggested_actions=[{"type": "quick_reply", "label": "按鈕"}])) == "schema:$.suggested_actions[0].text:required"
    assert reason_of(reply(suggested_actions=[{**action(), "url": "http://x"}])) == "schema:$.suggested_actions[0]:additional_property"


# ---- 洩漏標記 ------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["reply", "label", "text", "topic"])
def test_a_marker_anywhere_in_the_output_fails_validation(where):
    marker = "abcdef012345" if where == "label" else MARKER              # 標籤最長 12 字，用 12 字的標記才能到達語意規則
    leaked = f"內部代碼 {marker} 請勿外傳"
    payload = reply()
    if where == "reply":
        payload["reply_text"] = leaked
    elif where == "label":
        payload["suggested_actions"] = [action(label=marker)]
    elif where == "text":
        payload["suggested_actions"] = [action(text=leaked)]
    else:
        payload["entities"]["topic"] = marker
    assert reason_of(payload, marker) == "semantic:leaked_marker"


def test_the_marker_check_ignores_case_and_a_reply_without_it_passes():
    assert reason_of(reply(reply_text=f"代碼是 {MARKER.upper()}")) == "semantic:leaked_marker"
    assert parse(reply())


def test_a_different_marker_does_not_trigger_the_rule():
    assert parse(reply(reply_text=f"代碼是 {MARKER}"), marker="ffffffffffffffffffffffff")


def test_an_empty_marker_is_refused_because_it_would_match_everything():
    with pytest.raises(ValueError):
        semantic_rules("")


# ---- 低信心改寫 ----------------------------------------------------------------------------


def test_low_confidence_becomes_unknown_with_the_fixed_text_and_no_actions_or_entities():
    result = to_reply(parse(reply(confidence=0.4)))
    assert (result.intent, result.reply_text, result.rewritten) == ("unknown", LOW_CONFIDENCE_TEXT, True)
    assert result.suggested_actions == () and result.entities == {field: None for field in ENTITY_FIELDS}
    assert result.confidence == 0.4


@pytest.mark.parametrize("confidence, rewritten", [(0.59, True), (0.5999, True), (0.6, False), (0.61, False), (1, False)])
def test_the_threshold_is_exactly_zero_point_six(confidence, rewritten):
    result = to_reply(parse(reply(confidence=confidence)))
    assert result.rewritten is rewritten
    assert result.intent == ("unknown" if rewritten else "account_help")
    assert (result.reply_text == LOW_CONFIDENCE_TEXT) is rewritten


def test_an_unknown_intent_keeps_the_models_own_reply_even_at_low_confidence():
    text = "目前不提供訂單查詢，歡迎直接留下意見。"
    result = to_reply(parse(reply(intent="unknown", confidence=0.1, reply_text=text)))
    assert (result.intent, result.reply_text, result.rewritten) == ("unknown", text, False)


@pytest.mark.parametrize("intent", [i for i in INTENTS if i != "unknown"])
def test_every_other_intent_including_human_handoff_is_rewritten_when_confidence_is_low(intent):
    assert to_reply(parse(reply(intent=intent, confidence=0.3))).intent == "unknown"


def test_a_confident_reply_keeps_intent_entities_and_actions():
    result = to_reply(parse(reply()))
    assert result.intent == "account_help" and result.entities["account_issue"] == "forgot_password"
    assert result.suggested_actions == ({"type": "quick_reply", "label": "收不到信", "text": "我收不到重設密碼的信"},)


def test_surrounding_whitespace_is_trimmed_and_blank_chips_and_topics_are_dropped():
    parsed = parse(reply(
        reply_text="  您好，請稍候。\n", entities={**reply()["entities"], "topic": "   "},
        suggested_actions=[action("  好  ", " 是的 "), action("  ", "文字"), action("標籤", "   ")],
    ))
    result = to_reply(parsed)
    assert result.reply_text == "您好，請稍候。" and result.entities["topic"] is None
    assert result.suggested_actions == ({"type": "quick_reply", "label": "好", "text": "是的"},)
