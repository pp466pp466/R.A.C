"""parse_llm_json：模型輸出的單一解析入口（llm-structured-calls 規格「模型輸出由單一函式解析與驗證」）。"""
import json

import pytest

from app.llm.types import LLMValidationError
from app.services.llm_output import SemanticRule, parse_llm_json

SCHEMA = {
    "type": "object",
    "required": ["intent", "confidence", "tags"],
    "properties": {
        "intent": {"type": "string", "enum": ["a", "b"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "tags": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "string", "maxLength": 3}},
        "note": {"type": ["string", "null"]},
        "count": {"type": "integer"},
        "flag": {"type": "boolean"},
        "nested": {"type": "object", "required": ["x"], "properties": {"x": {"type": "string", "minLength": 2}}},
    },
}
GOOD = {"intent": "a", "confidence": 0.9, "tags": ["ok"]}


def reason_of(text, schema=SCHEMA, rules=()):
    with pytest.raises(LLMValidationError) as raised:
        parse_llm_json(text, schema, rules)
    return raised.value.reason


# ---- 解碼：程式碼區塊、夾雜文字、不是 JSON ----------------------------------------


def test_a_plain_object_is_parsed():
    assert parse_llm_json(json.dumps(GOOD), SCHEMA) == GOOD


@pytest.mark.parametrize("wrapper", [
    "```json\n{body}\n```",
    "```\n{body}\n```",
    "```JSON\r\n{body}\r\n```",
    "  \n```json\n{body}\n```  \n",
    "﻿{body}",
    "\n\n  {body}  \n",
])
def test_code_fences_bom_and_whitespace_are_stripped(wrapper):
    assert parse_llm_json(wrapper.replace("{body}", json.dumps(GOOD)), SCHEMA) == GOOD


@pytest.mark.parametrize("wrapper", [
    "好的，以下是結果：\n{body}\n希望有幫助。",
    "{body} 以上。",
    "前言 {{不是 JSON}} 然後 {body}",
    "```json\n{body}\n``` 另外補充一句。",
    "```json {body} ```",
])
def test_the_first_complete_object_is_extracted_from_surrounding_prose(wrapper):
    assert parse_llm_json(wrapper.replace("{body}", json.dumps(GOOD)), SCHEMA) == GOOD


def test_braces_and_escaped_quotes_inside_strings_do_not_confuse_the_extraction():
    tricky = {"intent": "a", "confidence": 1, "tags": ["}{"], "note": 'say "hi" } {'}
    assert parse_llm_json("說明 " + json.dumps(tricky, ensure_ascii=False) + " 結束", SCHEMA) == tricky


def test_an_unbalanced_opening_brace_before_the_object_is_skipped():
    assert parse_llm_json('提示 { 沒有結尾 ' + json.dumps(GOOD), SCHEMA) == GOOD


def test_only_the_first_object_counts_when_there_are_two():
    other = {**GOOD, "intent": "b"}
    assert parse_llm_json(f"{json.dumps(GOOD)} {json.dumps(other)}", SCHEMA)["intent"] == "a"


@pytest.mark.parametrize("text", [
    "", "   \n", "抱歉，我無法回答這個問題。", "{", '{"intent": "a", "confidence"', '{"intent": "a",}', "[1, 2",
    '{"intent": "a", "confidence": NaN, "tags": ["x"]}', '{"intent": "a", "confidence": Infinity, "tags": ["x"]}',
    "```json\n```", "{'intent': 'a'}",
])
def test_text_that_is_not_json_is_reported_as_not_json(text):
    assert reason_of(text) == "not_json"


def test_a_non_string_input_is_not_json():
    assert reason_of(None) == "not_json"


def test_a_truncated_object_is_not_json_even_if_a_prefix_looks_valid():
    assert reason_of(json.dumps(GOOD)[:-1]) == "not_json"


# ---- 結構描述 --------------------------------------------------------------------


@pytest.mark.parametrize("payload, expected", [
    ([1, 2], "schema:$:type"),
    ("文字", "schema:$:type"),
    ({**GOOD, "intent": 5}, "schema:$.intent:type"),
    ({**GOOD, "confidence": "0.9"}, "schema:$.confidence:type"),
    ({**GOOD, "confidence": True}, "schema:$.confidence:type"),                  # 布林不是數字
    ({**GOOD, "count": True}, "schema:$.count:type"),                            # 布林不是整數
    ({**GOOD, "count": 1.5}, "schema:$.count:type"),
    ({**GOOD, "flag": 1}, "schema:$.flag:type"),
    ({**GOOD, "tags": "ok"}, "schema:$.tags:type"),
    ({**GOOD, "nested": []}, "schema:$.nested:type"),
    ({"confidence": 1, "tags": ["x"]}, "schema:$.intent:required"),
    ({"intent": "a", "confidence": 1}, "schema:$.tags:required"),
    ({**GOOD, "nested": {}}, "schema:$.nested.x:required"),
    ({**GOOD, "intent": "c"}, "schema:$.intent:enum"),
    ({**GOOD, "confidence": 1.01}, "schema:$.confidence:maximum"),
    ({**GOOD, "confidence": -0.1}, "schema:$.confidence:minimum"),
    ({**GOOD, "tags": []}, "schema:$.tags:minItems"),
    ({**GOOD, "tags": ["a", "b", "c"]}, "schema:$.tags:maxItems"),
    ({**GOOD, "tags": ["ok", "太長的標籤"]}, "schema:$.tags[1]:maxLength"),
    ({**GOOD, "tags": [3]}, "schema:$.tags[0]:type"),
    ({**GOOD, "nested": {"x": "一"}}, "schema:$.nested.x:minLength"),
    ({**GOOD, "extra": 1}, "schema:$:additional_property"),
    ({**GOOD, "nested": {"x": "ab", "y": 1}}, "schema:$.nested:additional_property"),
])
def test_schema_violations_are_reported_with_the_field_path_and_the_rule(payload, expected):
    assert reason_of(json.dumps(payload, ensure_ascii=False)) == expected


@pytest.mark.parametrize("payload", [
    {**GOOD, "note": None},
    {**GOOD, "note": "文字"},
    {**GOOD, "count": 3, "flag": False},
    {**GOOD, "confidence": 0},
    {**GOOD, "confidence": 1},
    {**GOOD, "tags": ["abc", "def"]},
    {**GOOD, "nested": {"x": "ab"}},
])
def test_valid_variations_pass(payload):
    assert parse_llm_json(json.dumps(payload), SCHEMA) == payload


def test_nullable_flag_and_type_list_are_equivalent():
    flagged = {"type": "object", "required": ["v"], "properties": {"v": {"type": "string", "nullable": True}}}
    assert parse_llm_json('{"v": null}', flagged) == {"v": None}
    assert reason_of('{"v": 3}', flagged) == "schema:$.v:type"
    assert reason_of('{"v": null}', {"type": "object", "required": ["v"], "properties": {"v": {"type": "string"}}}) == "schema:$.v:type"


def test_a_required_field_may_be_present_with_null_only_when_nullable():
    assert reason_of('{"intent": null, "confidence": 1, "tags": ["x"]}') == "schema:$.intent:type"


def test_additional_properties_can_be_allowed_explicitly():
    open_schema = {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True}
    assert parse_llm_json('{"a": "x", "b": 2}', open_schema) == {"a": "x", "b": 2}


def test_an_object_schema_without_declared_properties_accepts_any_keys():
    assert parse_llm_json('{"anything": 1}', {"type": "object"}) == {"anything": 1}


def test_enum_distinguishes_booleans_from_numbers():
    schema = {"type": "object", "required": ["v"], "properties": {"v": {"enum": [1, 2]}}}
    assert parse_llm_json('{"v": 1}', schema) == {"v": 1}
    assert reason_of('{"v": true}', schema) == "schema:$.v:enum"


def test_string_length_counts_characters_not_bytes():
    schema = {"type": "object", "required": ["t"], "properties": {"t": {"type": "string", "maxLength": 4}}}
    assert parse_llm_json('{"t": "客服機器"}', schema)["t"] == "客服機器"
    assert reason_of('{"t": "客服機器人"}', schema) == "schema:$.t:maxLength"


def test_validation_does_not_modify_the_schema():
    before = json.dumps(SCHEMA, sort_keys=True)
    parse_llm_json(json.dumps(GOOD), SCHEMA)
    assert json.dumps(SCHEMA, sort_keys=True) == before


# ---- 語意驗證 --------------------------------------------------------------------


def test_semantic_rules_run_in_order_and_the_first_failure_names_the_rule():
    rules = (
        SemanticRule("confidence_high", lambda v: v["confidence"] >= 0.5),
        SemanticRule("intent_is_a", lambda v: v["intent"] == "a"),
        SemanticRule("never_reached", lambda v: 1 / 0),
    )
    assert parse_llm_json(json.dumps(GOOD), SCHEMA, rules[:2]) == GOOD
    assert reason_of(json.dumps({**GOOD, "confidence": 0.1, "intent": "b"}), rules=rules) == "semantic:confidence_high"
    assert reason_of(json.dumps({**GOOD, "intent": "b"}), rules=rules) == "semantic:intent_is_a"


def test_semantic_rules_do_not_run_when_the_structure_is_invalid():
    called = []
    rule = SemanticRule("spy", lambda v: called.append(v) or True)
    assert reason_of('{"intent": "a"}', rules=(rule,)) == "schema:$.confidence:required"
    assert called == []


def test_semantic_rules_receive_the_parsed_value():
    seen = []
    parse_llm_json("```json\n" + json.dumps(GOOD) + "\n```", SCHEMA, (SemanticRule("spy", lambda v: seen.append(v) or True),))
    assert seen == [GOOD]


# ---- 不洩漏模型文字 --------------------------------------------------------------

LEAK = "模型寫的機密字樣-ZX81"


@pytest.mark.parametrize("text", [
    LEAK,
    '{"' + LEAK + '": 1}',
    json.dumps({**GOOD, "intent": LEAK}, ensure_ascii=False),
    json.dumps({**GOOD, LEAK: 1}, ensure_ascii=False),
    json.dumps({**GOOD, "tags": [LEAK]}, ensure_ascii=False),
    json.dumps({**GOOD, "nested": {"x": "ab", LEAK: 1}}, ensure_ascii=False),
    json.dumps({**GOOD, "confidence": LEAK}, ensure_ascii=False),
    "{" + LEAK,
])
def test_neither_reason_nor_message_contains_any_model_text(text):
    with pytest.raises(LLMValidationError) as raised:
        parse_llm_json(text, SCHEMA)
    assert LEAK not in raised.value.reason and LEAK not in str(raised.value) and LEAK not in repr(raised.value)


def test_the_semantic_failure_message_does_not_contain_model_text_either():
    rule = SemanticRule("always_fails", lambda v: False)
    with pytest.raises(LLMValidationError) as raised:
        parse_llm_json(json.dumps({**GOOD, "note": LEAK}), SCHEMA, (rule,))
    assert raised.value.reason == "semantic:always_fails" and LEAK not in str(raised.value)
