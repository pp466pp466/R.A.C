"""模型結構化輸出的解析與驗證（見 llm-structured-calls 規格「模型輸出由單一函式解析與驗證」）。

`parse_llm_json` 是**唯一**處理模型輸出的入口（AGENTS.md 第 5 節）：依序做
1. 去除 BOM、前後空白與 Markdown 程式碼區塊標記（```json）；
2. JSON 解析；失敗時擷取「第一個大括號平衡的物件」再試（模型常在前後多寫一句話）；
3. 依中立的結構描述驗證（型別、必填、列舉、陣列長度、字串長度、數值範圍、拒絕多餘欄位）；
4. 依序執行呼叫端的語意驗證。
任一步失敗都拋出同一種 `LLMValidationError`，`reason` 只是分類，**絕不含模型輸出的任何文字**
（連多餘欄位的名稱也不放：那是模型寫的）。分類格式：
`not_json`、`schema:<欄位路徑>:<規則>`（路徑來自結構描述與陣列索引）、`semantic:<規則名>`。

中立結構描述支援的鍵：`type`（字串或陣列，如 `["string", "null"]`）、`nullable`、`properties`、`required`、
`additionalProperties`、`enum`、`items`、`minItems`、`maxItems`、`minLength`、`maxLength`、`minimum`、`maximum`。
轉接器只會把供應商支援的鍵送出去；長度與範圍一律在這裡驗證，不假設供應商保證它們。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

from app.llm.types import LLMValidationError

REASON_NOT_JSON = "not_json"

# 擷取夾雜在說明文字中的物件時，最多嘗試幾個候選（模型輸出有上限，這只是防呆）
_MAX_EXTRACT_ATTEMPTS = 5

_FENCE = re.compile(r"\A```[A-Za-z0-9_+-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?```\Z", re.DOTALL)


@dataclass(frozen=True)
class SemanticRule:
    """一條語意規則：`check` 收到已通過結構驗證的值，回傳 False 即失敗；`name` 會成為 `semantic:<name>` 的分類。"""

    name: str
    check: Callable[[Any], bool]


def parse_llm_json(text: str, schema: dict[str, Any], rules: Sequence[SemanticRule] = ()) -> Any:
    """解析並驗證模型輸出，成功時回傳解析後的值，失敗時拋出 `LLMValidationError`。"""
    value = _decode(text)
    _validate_schema(value, schema, "$")
    for rule in rules:
        if not rule.check(value):
            raise LLMValidationError(f"semantic:{rule.name}")
    return value


# ---- 解碼 ---------------------------------------------------------------------------


def _reject_constant(_name: str) -> None:
    raise ValueError("NaN 與無限大不是合法的 JSON")


def _loads(candidate: str) -> Any:
    return json.loads(candidate, parse_constant=_reject_constant)


def _decode(text: Any) -> Any:
    if not isinstance(text, str):
        raise LLMValidationError(REASON_NOT_JSON)
    cleaned = text.lstrip("﻿").strip()
    if not cleaned:
        raise LLMValidationError(REASON_NOT_JSON)
    fenced = _FENCE.match(cleaned)
    body = fenced.group("body").strip() if fenced else cleaned
    try:
        return _loads(body)
    except ValueError:
        pass
    for candidate in _balanced_objects(cleaned):
        try:
            return _loads(candidate)
        except ValueError:
            continue
    raise LLMValidationError(REASON_NOT_JSON)


def _balanced_objects(text: str) -> Iterator[str]:
    """依出現順序產生「大括號平衡」的片段；字串內的括號與跳脫字元不計。"""
    attempts = 0
    start = text.find("{")
    while start != -1 and attempts < _MAX_EXTRACT_ATTEMPTS:
        end = _closing_index(text, start)
        if end is None:
            start = text.find("{", start + 1)          # 這個開頭沒有對應的結尾，換下一個
            continue
        attempts += 1
        yield text[start : end + 1]
        start = text.find("{", end + 1)                # 這個候選解析失敗，跳過它整段（含內部的括號）


def _closing_index(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


# ---- 結構描述 -----------------------------------------------------------------------


def _fail(path: str, rule: str) -> LLMValidationError:
    return LLMValidationError(f"schema:{path}:{rule}")


def _is_type(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "null":
        return value is None
    return False


def _in_enum(value: Any, allowed: Sequence[Any]) -> bool:
    # True == 1 在 Python 成立，但 JSON 的布林與數字是不同的值
    return any(value == item and isinstance(value, bool) == isinstance(item, bool) for item in allowed)


def _validate_schema(value: Any, schema: dict[str, Any], path: str) -> None:
    declared = schema.get("type")
    names = [declared] if isinstance(declared, str) else list(declared or [])
    nullable = bool(schema.get("nullable")) or "null" in names
    if value is None and nullable:
        return
    typed = [name for name in names if name != "null"]
    if typed and not any(_is_type(value, name) for name in typed):
        raise _fail(path, "type")

    if "enum" in schema and not _in_enum(value, schema["enum"]):
        raise _fail(path, "enum")

    if isinstance(value, dict):
        _validate_object(value, schema, path)
    elif isinstance(value, list):
        _validate_array(value, schema, path)
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise _fail(path, "minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise _fail(path, "maxLength")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise _fail(path, "minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise _fail(path, "maximum")


def _validate_object(value: dict[str, Any], schema: dict[str, Any], path: str) -> None:
    properties = schema.get("properties", {})
    for name in schema.get("required", ()):
        if name not in value:
            raise _fail(f"{path}.{name}", "required")
    if properties and not schema.get("additionalProperties", False):
        if any(key not in properties for key in value):
            raise _fail(path, "additional_property")          # 不指出是哪個欄位：欄位名稱是模型寫的
    for name, sub_schema in properties.items():
        if name in value:
            _validate_schema(value[name], sub_schema, f"{path}.{name}")


def _validate_array(value: list[Any], schema: dict[str, Any], path: str) -> None:
    if len(value) < schema.get("minItems", 0):
        raise _fail(path, "minItems")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        raise _fail(path, "maxItems")
    item_schema = schema.get("items")
    if item_schema is not None:
        for index, item in enumerate(value):
            _validate_schema(item, item_schema, f"{path}[{index}]")
