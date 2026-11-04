"""劇本固定規格 v1 的驗證與標準化（見 script-fixed-spec 規格、AGENTS.md 第 7、10 節、rpg-data-and-script 的 design D5）。

這是玩家創作與內建種子共用的資料合約，**純運算**：沒有資料庫、網路與時間的依賴，相同輸入得到相同輸出。

- 輸入原始內容（`dict`），輸出 `ValidatedScript`（標準化內容、`content_hash`、`total_chars`），或拋出 `ValidationError`，
  其 `fields` 以欄位路徑為鍵（`title`、`characters[2].age`、`milestones[0].trigger.value`），多個錯誤一次回報。
- 標準化：NFC、去除控制字元與格式字元（含零寬度字元）；單行欄位把任何空白收斂為單一空白並去除前後空白，多行欄位保留換行。
  長度一律以標準化之後的 Unicode 字元數計。
- 內容含規格沒有的欄位會被拒絕，而不是被忽略；圖片欄位只接受空值（v1 預留）。
- 錯誤訊息只描述規則，**不回顯使用者輸入**（也不顯示不公開的標記字串），可直接對應到表單欄位。
- 所有文字都是使用者文字：只以純文字處理，這裡不解讀任何標記語法。
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from app.models.user import CONTENT_TIERS
from app.services.content_tier import tier_rank
from app.services.errors import ValidationError

SPEC_VERSION = 1

# ---- 欄位與數量上限（暫定值，之後以實際 token 預算校正；見 design 的 Risks）-----------------------------

TITLE_MAX = 30
SUMMARY_MAX = 150
WORLD_NAME_MAX = 50
BACKGROUND_MAX = 1500
OPENING_MAX = 500
RULES_MAX = 800
TAG_MAX_COUNT = 5
TAG_MAX = 12
CALENDAR_NAME_MAX = 12
YEAR_MAX = 99999
ENDING_TEXT_MAX = 300
ENDING_AGE_MAX = 9999
CHARACTER_MAX_COUNT = 10
CHARACTER_NAME_MAX = 20
CHARACTER_PERSONALITY_MAX = 60
CHARACTER_APPEARANCE_MAX = 150
CHARACTER_OCCUPATION_MAX = 20
CHARACTER_SPEECH_STYLE_MAX = 100
CHARACTER_HIDDEN_TRAIT_MAX = 60
CHARACTER_AGE_MAX = 9999
LOCATION_MAX_COUNT = 15
LOCATION_MAX = 30
MILESTONE_MAX_COUNT = 8
MILESTONE_TITLE_MAX = 60
MILESTONE_NOTE_MAX = 100
MILESTONE_ID_MAX = 20
TRIGGER_AGE_MAX = 9999
TOTAL_CHARS_MAX = 8000

TRIGGER_AGE = "age"
TRIGGER_YEAR = "year"
TRIGGER_KINDS = (TRIGGER_AGE, TRIGGER_YEAR)
ENDING_MODES = ("none", "custom")

# 清單類欄位的數量以「去除重複之後」計；原始長度超過上限的這個倍數就直接拒絕，避免逐項處理過長的輸入
_RAW_LIST_FACTOR = 4

_TOP_LEVEL_FIELDS = frozenset({
    "spec_version", "title", "summary", "world_name", "content_tier", "background", "opening", "rules", "tags", "calendar", "ending_mode",
    "ending_text", "ending_age", "characters", "locations", "milestones", "cover_image",
})
_CHARACTER_FIELDS = frozenset({"name", "personality", "age", "appearance", "occupation", "speech_style", "hidden_trait", "image"})
_MILESTONE_FIELDS = frozenset({"id", "title", "trigger", "note"})
_TRIGGER_FIELDS = frozenset({"kind", "value"})
_CALENDAR_FIELDS = frozenset({"name", "start_year"})


@dataclass(frozen=True)
class ValidatedScript:
    content: dict[str, Any]       # 標準化後的完整內容（含 `spec_version`）
    content_hash: str             # 規範化 JSON 的 SHA-256（十六進位小寫）
    total_chars: int              # 全部文字欄位的字元總數（數字不計）
    spec_version: int = SPEC_VERSION


# ---- 標準化 ---------------------------------------------------------------------------

def _strip_controls(value: str) -> str:
    """NFC，並去除控制字元（Cc，換行與定位字元另外處理）與格式字元（Cf，含零寬度字元與雙向控制字元）。"""
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    kept = []
    for char in value:
        if char in "\n\t":
            kept.append(char)
        elif unicodedata.category(char) in ("Cc", "Cf"):
            continue
        else:
            kept.append(char)
    return "".join(kept)


def normalize_single_line(value: str) -> str:
    return " ".join(_strip_controls(value).split())


def normalize_multi_line(value: str) -> str:
    return _strip_controls(value).replace("\t", " ").strip()


def fold(value: str) -> str:
    """比較用：NFC 加不分大小寫。"""
    return unicodedata.normalize("NFC", value).casefold()


# ---- 驗證 -----------------------------------------------------------------------------

class _Collector:
    """逐欄位收集錯誤（同一路徑只保留第一個）與文字總量。"""

    def __init__(self, forbidden_markers: Iterable[str]) -> None:
        self.errors: dict[str, str] = {}
        self.total = 0
        self.markers = tuple(fold(marker) for marker in forbidden_markers if marker)

    def fail(self, path: str, message: str) -> None:
        self.errors.setdefault(path, message)

    def text(self, value: Any, path: str, *, maximum: int, required: bool = False, multiline: bool = False) -> str | None:
        if value is None or value == "":
            if required:
                self.fail(path, "這個欄位為必填。")
            return None
        if not isinstance(value, str):
            self.fail(path, "必須是文字。")
            return None
        text = normalize_multi_line(value) if multiline else normalize_single_line(value)
        if not text:
            if required:
                self.fail(path, "這個欄位為必填。")
            return None
        if len(text) > maximum:
            self.fail(path, f"最多 {maximum} 字。")
            return None
        if self.markers and any(marker in fold(text) for marker in self.markers):
            self.fail(path, "含有不允許的內容。")          # 不顯示標記字串本身；比對不分大小寫，避免只改大小寫就繞過
            return None
        self.total += len(text)
        return text

    def integer(self, value: Any, path: str, *, minimum: int, maximum: int, required: bool = False) -> int | None:
        if value is None:
            if required:
                self.fail(path, "這個欄位為必填。")
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(path, "必須是整數。")
            return None
        if not minimum <= value <= maximum:
            self.fail(path, f"必須介於 {minimum} 與 {maximum} 之間。")
            return None
        return value


def _is_empty(value: Any) -> bool:
    """圖片預留欄位的「空值」：沒有提供、空字串或只有空白。其他任何值（網址、檔名、資料 URI、數字…）都不是空值。"""
    return value is None or (isinstance(value, str) and not value.strip())


def _unknown_fields(collector: _Collector, mapping: dict[str, Any], allowed: frozenset[str], prefix: str) -> None:
    for key in mapping:
        if key not in allowed:
            # 路徑是「規格外的欄位名稱」，不是欄位內容；過長的名稱截斷，避免回應被任意放大
            collector.fail(f"{prefix}{str(key)[:50]}", "規格沒有這個欄位。")


def _list_of_text(collector: _Collector, value: Any, path: str, *, maximum_count: int, maximum: int, lower: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        collector.fail(path, "必須是清單。")
        return []
    if len(value) > maximum_count * _RAW_LIST_FACTOR:
        collector.fail(path, f"最多 {maximum_count} 個。")          # 先擋住離譜的長度，不逐項處理
        return []
    seen: set[str] = set()
    result: list[str] = []
    for index, item in enumerate(value):
        text = collector.text(item, f"{path}[{index}]", maximum=maximum, required=True)
        if text is None:
            continue
        key = fold(text)
        if key in seen:
            collector.total -= len(text)   # 重複的不會留在標準化內容裡，不計入總量
            continue                       # 去除重複，保留第一個的寫法
        seen.add(key)
        result.append(text)
    if len(result) > maximum_count:        # 數量以去除重複之後計
        collector.fail(path, f"最多 {maximum_count} 個。")
        return result[:maximum_count]
    return result


def _validate_characters(collector: _Collector, value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        collector.fail("characters", "必須是清單。")
        return []
    if len(value) > CHARACTER_MAX_COUNT:
        collector.fail("characters", f"最多 {CHARACTER_MAX_COUNT} 位。")
    seen_names: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value[:CHARACTER_MAX_COUNT]):
        path = f"characters[{index}]"
        if not isinstance(raw, dict):
            collector.fail(path, "必須是一位角色的資料。")
            continue
        _unknown_fields(collector, raw, _CHARACTER_FIELDS, f"{path}.")
        name = collector.text(raw.get("name"), f"{path}.name", maximum=CHARACTER_NAME_MAX, required=True)
        if name is not None:
            key = fold(name)
            if key in seen_names:
                collector.fail(f"{path}.name", "角色姓名不可重複。")
            seen_names.add(key)
        personality = collector.text(raw.get("personality"), f"{path}.personality", maximum=CHARACTER_PERSONALITY_MAX, required=True)
        age = collector.integer(raw.get("age"), f"{path}.age", minimum=0, maximum=CHARACTER_AGE_MAX, required=True)
        appearance = collector.text(raw.get("appearance"), f"{path}.appearance", maximum=CHARACTER_APPEARANCE_MAX, required=True, multiline=True)
        occupation = collector.text(raw.get("occupation"), f"{path}.occupation", maximum=CHARACTER_OCCUPATION_MAX)
        speech_style = collector.text(raw.get("speech_style"), f"{path}.speech_style", maximum=CHARACTER_SPEECH_STYLE_MAX, multiline=True)
        hidden_trait = collector.text(raw.get("hidden_trait"), f"{path}.hidden_trait", maximum=CHARACTER_HIDDEN_TRAIT_MAX)
        if not _is_empty(raw.get("image")):
            collector.fail(f"{path}.image", "圖片功能尚未開放。")
        result.append({
            "name": name, "personality": personality, "age": age, "appearance": appearance, "occupation": occupation,
            "speech_style": speech_style, "hidden_trait": hidden_trait, "image": None,
        })
    return result


def _validate_calendar(collector: _Collector, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        collector.fail("calendar", "必須是曆法的資料。")
        return None
    _unknown_fields(collector, value, _CALENDAR_FIELDS, "calendar.")
    name = collector.text(value.get("name"), "calendar.name", maximum=CALENDAR_NAME_MAX, required=True)
    start_year = collector.integer(value.get("start_year"), "calendar.start_year", minimum=1, maximum=YEAR_MAX, required=True)
    return {"name": name, "start_year": start_year}


def _validate_milestones(collector: _Collector, value: Any, calendar: dict[str, Any] | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        collector.fail("milestones", "必須是清單。")
        return []
    if len(value) > MILESTONE_MAX_COUNT:
        collector.fail("milestones", f"最多 {MILESTONE_MAX_COUNT} 個。")
    items = value[:MILESTONE_MAX_COUNT]
    provided_ids = {raw["id"] for raw in items if isinstance(raw, dict) and isinstance(raw.get("id"), str)}
    used_ids: set[str] = set()
    generated = 0
    needs_calendar = False
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        path = f"milestones[{index}]"
        if not isinstance(raw, dict):
            collector.fail(path, "必須是一個里程碑的資料。")
            continue
        _unknown_fields(collector, raw, _MILESTONE_FIELDS, f"{path}.")
        title = collector.text(raw.get("title"), f"{path}.title", maximum=MILESTONE_TITLE_MAX, required=True)
        note = collector.text(raw.get("note"), f"{path}.note", maximum=MILESTONE_NOTE_MAX, multiline=True)

        milestone_id = raw.get("id")
        if milestone_id is None or milestone_id == "":
            while True:                    # 依順序產生，跳過已被明確提供的編號：相同輸入永遠得到相同編號
                generated += 1
                candidate = f"m{generated}"
                if candidate not in provided_ids and candidate not in used_ids:
                    milestone_id = candidate
                    break
        elif not isinstance(milestone_id, str) or not milestone_id.isascii() or not milestone_id.replace("_", "").replace("-", "").isalnum() \
                or len(milestone_id) > MILESTONE_ID_MAX:
            collector.fail(f"{path}.id", f"編號只能是英數字、底線與連字號，最多 {MILESTONE_ID_MAX} 字。")
            milestone_id = None
        elif milestone_id in used_ids:
            collector.fail(f"{path}.id", "里程碑編號不可重複。")
        if isinstance(milestone_id, str):
            used_ids.add(milestone_id)

        trigger = _validate_trigger(collector, raw.get("trigger"), f"{path}.trigger", calendar)
        if trigger is not None and trigger["kind"] == TRIGGER_YEAR:
            needs_calendar = True
        result.append({"id": milestone_id, "title": title, "trigger": trigger, "note": note})
    if needs_calendar and calendar is None:
        collector.fail("calendar", "有年份觸發的里程碑時，必須提供曆法。")
    return result


def _validate_trigger(collector: _Collector, value: Any, path: str, calendar: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        collector.fail(path, "觸發必須選擇種類並填入數值，不接受自由文字。")
        return None
    _unknown_fields(collector, value, _TRIGGER_FIELDS, f"{path}.")
    kind = value.get("kind")
    if kind not in TRIGGER_KINDS:
        collector.fail(f"{path}.kind", "觸發種類只能是玩家年齡或遊戲內年份。")
        return None
    maximum = TRIGGER_AGE_MAX if kind == TRIGGER_AGE else YEAR_MAX
    minimum = 0 if kind == TRIGGER_AGE else 1
    number = collector.integer(value.get("value"), f"{path}.value", minimum=minimum, maximum=maximum, required=True)
    if number is not None and kind == TRIGGER_YEAR and calendar is not None and calendar.get("start_year") is not None \
            and number < calendar["start_year"]:
        collector.fail(f"{path}.value", "年份不得早於曆法的起始年。")
    return {"kind": kind, "value": number}


def validate_script(raw: Any, *, max_tier: str, forbidden_markers: Iterable[str] = ()) -> ValidatedScript:
    """驗證並標準化一份劇本內容。

    `max_tier`：作者的內容模式上限（未成年作者為 `safe`）；宣告的 `content_tier` 不得高於它。
    `forbidden_markers`：系統不公開的標記字串，文字欄位不得包含（呼叫端提供，錯誤訊息不顯示它們）。
    失敗時拋出 `ValidationError`，`fields` 為「欄位路徑 → 說明」。
    """
    if max_tier not in CONTENT_TIERS:
        raise ValueError("max_tier 必須是三種內容模式之一。")
    if not isinstance(raw, dict):
        raise ValidationError("劇本內容必須是一份資料。", fields={"content": "劇本內容必須是一份資料。"})

    c = _Collector(forbidden_markers)
    _unknown_fields(c, raw, _TOP_LEVEL_FIELDS, "")
    given_version = raw.get("spec_version")             # 標準化後的內容帶有版本，可以再被驗證一次（冪等）
    if given_version is not None and (isinstance(given_version, bool) or not isinstance(given_version, int) or given_version != SPEC_VERSION):
        c.fail("spec_version", f"不支援的規格版本，目前只支援第 {SPEC_VERSION} 版。")

    title = c.text(raw.get("title"), "title", maximum=TITLE_MAX, required=True)
    summary = c.text(raw.get("summary"), "summary", maximum=SUMMARY_MAX, required=True)
    world_name = c.text(raw.get("world_name"), "world_name", maximum=WORLD_NAME_MAX, required=True)
    background = c.text(raw.get("background"), "background", maximum=BACKGROUND_MAX, required=True, multiline=True)
    opening = c.text(raw.get("opening"), "opening", maximum=OPENING_MAX, multiline=True)
    rules = c.text(raw.get("rules"), "rules", maximum=RULES_MAX, multiline=True)

    tier = raw.get("content_tier")
    if tier is None or tier == "":
        c.fail("content_tier", "這個欄位為必填。")
        tier = None
    elif tier not in CONTENT_TIERS:
        c.fail("content_tier", "內容模式只能是安全、一般或成人。")
        tier = None
    elif tier_rank(tier) > tier_rank(max_tier):
        c.fail("content_tier", "宣告的內容模式高於你目前可使用的上限。")

    tags = _list_of_text(c, raw.get("tags"), "tags", maximum_count=TAG_MAX_COUNT, maximum=TAG_MAX)
    calendar = _validate_calendar(c, raw.get("calendar"))

    ending_mode = raw.get("ending_mode")
    if ending_mode is None or ending_mode == "":
        ending_mode = "none"
    ending_text = ending_age = None
    if ending_mode not in ENDING_MODES:
        c.fail("ending_mode", "結局模式只能是無結局或自訂結局。")
        ending_mode = None
    elif ending_mode == "custom":
        ending_text = c.text(raw.get("ending_text"), "ending_text", maximum=ENDING_TEXT_MAX, required=True, multiline=True)
        ending_age = c.integer(raw.get("ending_age"), "ending_age", minimum=1, maximum=ENDING_AGE_MAX)
    else:
        for field in ("ending_text", "ending_age"):
            if raw.get(field) not in (None, ""):
                c.fail(field, "選擇無結局時不能填寫結局說明或壽終年齡。")

    characters = _validate_characters(c, raw.get("characters"))
    locations = _list_of_text(c, raw.get("locations"), "locations", maximum_count=LOCATION_MAX_COUNT, maximum=LOCATION_MAX)
    milestones = _validate_milestones(c, raw.get("milestones"), calendar)
    if not _is_empty(raw.get("cover_image")):
        c.fail("cover_image", "圖片功能尚未開放。")

    if c.total > TOTAL_CHARS_MAX:
        c.fail("total", f"全部文字合計 {c.total} 字，超過上限 {TOTAL_CHARS_MAX} 字。")
    if c.errors:
        raise ValidationError("劇本內容不符合固定規格，請檢查標示的欄位。", fields=dict(c.errors))

    content = {
        "spec_version": SPEC_VERSION, "title": title, "summary": summary, "world_name": world_name, "content_tier": tier,
        "background": background, "opening": opening, "rules": rules, "tags": tags, "calendar": calendar,
        "ending_mode": ending_mode, "ending_text": ending_text, "ending_age": ending_age, "characters": characters,
        "locations": locations, "milestones": milestones, "cover_image": None,
    }
    return ValidatedScript(content=content, content_hash=hash_content(content), total_chars=c.total)


def hash_content(content: dict[str, Any]) -> str:
    """標準化內容的雜湊：規範化 JSON（排序鍵、不跳脫中文、UTF-8）的 SHA-256。鍵順序與空白不影響結果。"""
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
