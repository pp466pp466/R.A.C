"""玩家主角的角色卡與錨定事實的驗證（見 rpg-protagonist 規格、AGENTS.md 第 7 節「玩家主角」、rpg-character-creation 的 design D2、D3）。

**純運算**：沒有資料庫、網路與時間的依賴，相同輸入得到相同輸出。

- 角色卡：與劇本角色**同一套**欄位、上限與標準化（`script_spec.validate_character`），錯誤欄位路徑以 `protagonist.` 開頭。
- 錨定事實：每位主角至多一個，結構 `{kind, ref, text}`：`ref` 必須指向該劇本內容中存在的人物（姓名）、地點（名稱）或里程碑（編號）；
  `text` 是單行文字，1 至 60 字。主角或對象（指向人物時）未成年時，`text` 不得含戀愛或婚姻關係的用語（童養媳與已成婚類也在內）；**婚約類**（娃娃親、指腹為婚、訂婚、未婚夫妻）是歷史題材常見的背景設定，只在主角與對象一成年、一未成年時才擋——這是**欄位錯誤**，
  不是審查的 `reject`：它是結構規則，不帶審查類別，也不進任何拒絕計次。
- 自我介紹：選填的使用者文字（至多 300 字），這裡只做標準化與長度檢查；它只用於生成預覽，不保存。
- 錯誤訊息只描述規則，**不回顯輸入**，也不顯示不公開的標記字串。
- 成年門檻由呼叫端提供（年齡規則的單一來源 `AccessPolicy.adult_age`），這裡不寫死。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.services.script_rules import Lexicon, mentions_betrothal, mentions_romantic_relation
from app.services.script_spec import fold, normalize_multi_line, normalize_single_line, validate_character

INTRO_MAX = 300
ANCHOR_TEXT_MAX = 60

ANCHOR_CHARACTER = "character"
ANCHOR_LOCATION = "location"
ANCHOR_MILESTONE = "milestone"
ANCHOR_KINDS = (ANCHOR_CHARACTER, ANCHOR_LOCATION, ANCHOR_MILESTONE)

_ANCHOR_FIELDS = frozenset({"kind", "ref", "text"})
_PROTAGONIST_PATH = "protagonist"


def clean_intro(raw: Any, *, forbidden_markers: Iterable[str] = ()) -> tuple[str | None, dict[str, str]]:
    """選填的自我介紹：沒有或只有空白回傳 `(None, {})`；否則回傳標準化的文字。錯誤的欄位是 `intro`。"""
    if raw is None or raw == "":
        return None, {}
    if not isinstance(raw, str):
        return None, {"intro": "必須是文字。"}
    text = normalize_multi_line(raw)
    if not text:
        return None, {}
    if len(text) > INTRO_MAX:
        return None, {"intro": f"最多 {INTRO_MAX} 字。"}
    folded = fold(text)
    if any(marker and fold(marker) in folded for marker in forbidden_markers):
        return None, {"intro": "含有不允許的內容。"}
    return text, {}


def validate_protagonist(raw: Any, *, forbidden_markers: Iterable[str] = ()) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """驗證並標準化主角角色卡，回傳 `(角色或 None, 逐欄位錯誤)`；欄位路徑以 `protagonist.` 開頭。"""
    return validate_character(raw, path=_PROTAGONIST_PATH, forbidden_markers=forbidden_markers)


def _target_of(kind: str, ref: str, script_content: Mapping[str, Any]) -> tuple[str | None, int | None]:
    """依種類在劇本內容中找出 `ref` 指向的項目，回傳 `(劇本裡的正式寫法, 對象的年齡)`；找不到回傳 `(None, None)`。"""
    if kind == ANCHOR_CHARACTER:
        wanted = fold(normalize_single_line(ref))
        for character in script_content.get("characters") or []:
            if fold(character.get("name") or "") == wanted:
                age = character.get("age")
                return character["name"], age if isinstance(age, int) and not isinstance(age, bool) else None
    elif kind == ANCHOR_LOCATION:
        wanted = fold(normalize_single_line(ref))
        for location in script_content.get("locations") or []:
            if fold(location) == wanted:
                return location, None
    elif kind == ANCHOR_MILESTONE:
        for milestone in script_content.get("milestones") or []:
            if milestone.get("id") == ref:
                return milestone["id"], None
    return None, None


def validate_anchor(
    raw: Any,
    *,
    script_content: Mapping[str, Any],
    protagonist_age: int | None,
    adult_age: int,
    lexicon: Lexicon | None = None,
    forbidden_markers: Iterable[str] = (),
) -> tuple[dict[str, str] | None, dict[str, str]]:
    """驗證並標準化錨定事實，回傳 `({"kind", "ref", "text"} 或 None, 逐欄位錯誤)`。

    `ref` 以劇本裡的正式寫法回傳。`protagonist_age` 為 `None`（角色卡本身不合格）時略過年齡相關的守衛，因為玩家得先修正角色卡。
    """
    if not isinstance(raw, Mapping):
        return None, {"anchor": "聯繫必須是一個項目的資料。"}
    errors: dict[str, str] = {}
    for key in raw:
        if key not in _ANCHOR_FIELDS:
            errors[f"anchor.{str(key)[:50]}"] = "規格沒有這個欄位。"

    kind = raw.get("kind")
    target: str | None = None
    target_age: int | None = None
    if kind not in ANCHOR_KINDS:
        errors["anchor.kind"] = "聯繫的對象只能是劇本中的人物、地點或里程碑。"
    else:
        ref = raw.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            errors["anchor.ref"] = "請選擇聯繫的對象。"
        else:
            target, target_age = _target_of(kind, ref, script_content)
            if target is None:
                errors["anchor.ref"] = "這個對象不在劇本中。"

    text_raw = raw.get("text")
    text: str | None = None
    if text_raw is None or text_raw == "":
        errors["anchor.text"] = "這個欄位為必填。"
    elif not isinstance(text_raw, str):
        errors["anchor.text"] = "必須是文字。"
    else:
        cleaned = normalize_single_line(text_raw)
        folded = fold(cleaned)
        if not cleaned:
            errors["anchor.text"] = "這個欄位為必填。"
        elif len(cleaned) > ANCHOR_TEXT_MAX:
            errors["anchor.text"] = f"最多 {ANCHOR_TEXT_MAX} 字。"
        elif any(marker and fold(marker) in folded for marker in forbidden_markers):
            errors["anchor.text"] = "含有不允許的內容。"
        else:
            text = cleaned

    if text is not None and _someone_is_minor(protagonist_age, target_age, adult_age) and mentions_romantic_relation(text, lexicon=lexicon):
        errors["anchor.text"] = "主角或對象未成年時，聯繫不能是戀愛或婚姻關係。"
        text = None
    elif text is not None and _adult_with_minor(protagonist_age, target_age, adult_age) and mentions_betrothal(text, lexicon=lexicon):
        errors["anchor.text"] = "成年與未成年之間的聯繫，不能是婚約關係。"
        text = None

    if errors or target is None or text is None:
        return None, errors
    return {"kind": kind, "ref": target, "text": text}, {}


def _adult_with_minor(protagonist_age: int | None, target_age: int | None, adult_age: int) -> bool:
    """主角與對象（指向人物時才有年齡）一成年、一未成年。兩位都未成年的娃娃親是歷史題材常見的背景設定，不在此限；年齡不明（對象不是人物）也不擋。"""
    if protagonist_age is None or target_age is None:
        return False
    return (protagonist_age < adult_age) != (target_age < adult_age)


def _someone_is_minor(protagonist_age: int | None, target_age: int | None, adult_age: int) -> bool:
    return any(age is not None and age < adult_age for age in (protagonist_age, target_age))


def validate_anchor_choice(
    raw: Any, skip: Any, *, script_content: Mapping[str, Any], protagonist_age: int | None, adult_age: int,
    lexicon: Lexicon | None = None, forbidden_markers: Iterable[str] = (),
) -> tuple[dict[str, str] | None, dict[str, str]]:
    """開存檔時的錨定事實：帶一個合法的錨定事實，或明確略過（`skip` 恰為 `True`）；兩者都沒有或同時有，都是 `anchor` 的欄位錯誤。"""
    if skip is not None and not isinstance(skip, bool):
        return None, {"anchor": "略過必須是真或假。"}
    skipped = skip is True
    if raw is None and not skipped:
        return None, {"anchor": "請提供與劇本的聯繫，或明確選擇略過。"}
    if raw is not None and skipped:
        return None, {"anchor": "不能同時提供聯繫又選擇略過。"}
    if raw is None:
        return None, {}
    return validate_anchor(
        raw, script_content=script_content, protagonist_age=protagonist_age, adult_age=adult_age, lexicon=lexicon,
        forbidden_markers=forbidden_markers,
    )


def anchor_options(script_content: Mapping[str, Any]) -> dict[str, list[dict[str, Any]] | list[str]]:
    """創角畫面可以選的聯繫對象：人物只給姓名與職業（不給年齡與隱藏特質）、地點的名稱、里程碑的編號與標題。"""
    return {
        "characters": [
            {"name": character.get("name"), "occupation": character.get("occupation")}
            for character in script_content.get("characters") or []
        ],
        "locations": list(script_content.get("locations") or []),
        "milestones": [{"id": milestone.get("id"), "title": milestone.get("title")} for milestone in script_content.get("milestones") or []],
    }


def build_protagonist(character: Mapping[str, Any], anchor: Mapping[str, str] | None) -> dict[str, Any]:
    """存入 `sessions.protagonist_json` 的形狀：`{"character": {...}, "anchor": {...} 或 None}`。"""
    return {"character": dict(character), "anchor": dict(anchor) if anchor is not None else None}
