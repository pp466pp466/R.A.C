"""RPG 世界狀態的純邏輯（見 rpg-world-state、rpg-turn-engine 規格與 rpg-turn-engine 的 design D10）。

模型只「提議」，這裡驗證後才產生新的世界狀態；**純函式**：沒有資料庫、網路、時間與隨機的依賴，相同輸入得到相同輸出，
不匯入資料模型、Repository 或 Flask（`tests/architecture/test_rpg_pure_modules.py` 守住）。

世界狀態由存檔的欄位組成（`WorldState.from_fields` 讀、`to_fields` 寫，序列化由 Repository 負責）：
- `entities_json`（v1）：`{"v": 1, "npcs": {<名稱鍵>: 角色卡}, "protagonist": {"age", "age_at_month"}（第一次更正才建立）, "locations": [新增的地點]}`；
  角色卡 = `{name, traits: [{label, layer}], stage, age?, age_at_month?, anchor_fact?, last_seen_turn, origin}`，`origin` 是 `script`（劇本角色）或 `story`。
- `goals_json`：`{"active": [{text, milestone_id, turn}], "archived": [{text, milestone_id, turn, done_turn}]}`。
- `milestone_progress_json`：`{<里程碑編號>: {"triggered_turn": n, "extended": bool}}`；劇本複本唯讀，進度另存。

年齡紀錄 = `age` 加上紀錄後經過的整年數（`age_at_month` 是紀錄當下的遊戲內月數）；更正可往任何方向，每次更正產生通知（含更正前的目前年齡）。
劇本角色第一次在行動或敘事文字中出現時建卡（階段 `surface`），其年齡與特質取自劇本；劇本的 `personality`／`hidden_trait` 依標點拆成
至多 8 字的短語（各至多 3 項），個性當表層、隱藏特質當隱藏——劇本原文仍完整留在劇本資料區塊（隱藏特質除外，見 `rpg_context`）。

**所有玩家可見的文字**（特質、錨定事實、目標、地點、狀態摘要、NPC 名稱）都去除控制字元，並經過與劇本相同的規則層；任何非通過的結果就丟棄該項
（這些文字會回到下一回合的上下文，不能成為繞過輸出審查的通道）。錨定事實一旦有值就不被模型覆寫（錨定＝固定）。丟棄只計數量，不記內容。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping

from app.services.protagonist import ANCHOR_CHARACTER
from app.services.script_rules import DEFAULT_ADULT_AGE, REVIEW_PASS, check_text_rules
from app.services.script_spec import CHARACTER_NAME_MAX, LOCATION_MAX, TRIGGER_AGE, TRIGGER_YEAR, fold, normalize_single_line

WORLD_VERSION = 1

STAGE_SURFACE = "surface"
STAGE_CRACKING = "cracking"
STAGE_REVEALED = "revealed"
STAGES = (STAGE_SURFACE, STAGE_CRACKING, STAGE_REVEALED)
# 允許的轉移：只前進一階；cracking 可退回 surface；revealed 是終點
_NEXT_STAGE = {STAGE_SURFACE: STAGE_CRACKING, STAGE_CRACKING: STAGE_REVEALED}
_ALLOWED_TRANSITIONS = {
    (STAGE_SURFACE, STAGE_CRACKING), (STAGE_CRACKING, STAGE_REVEALED), (STAGE_CRACKING, STAGE_SURFACE),
}

LAYER_SURFACE = "surface"
LAYER_HIDDEN = "hidden"
LAYERS = (LAYER_SURFACE, LAYER_HIDDEN)

ORIGIN_SCRIPT = "script"
ORIGIN_STORY = "story"

ENDING_CUSTOM = "custom"

NPC_MAX_COUNT = 30
TRAIT_MAX_COUNT = 6
TRAIT_LABEL_MAX = 8
SCRIPT_TRAITS_PER_SOURCE = 3
ANCHOR_FACT_MAX = 100
NEW_LOCATION_MAX_COUNT = 30
GOAL_MAX_ACTIVE = 5
GOAL_TEXT_MAX = 60
ARCHIVED_GOALS_MAX = 50            # 已歸檔目標只留最近的 50 筆，避免 JSON 無限成長
STATUS_SUMMARY_MAX = 100
AGE_MIN = 1
AGE_MAX = 9999

NOTICE_AGE_CORRECTED = "age_corrected"


# ---- 世界狀態 -------------------------------------------------------------------------


def empty_entities() -> dict[str, Any]:
    return {"v": WORLD_VERSION, "npcs": {}, "locations": []}


def _normalized_entities(raw: Any) -> dict[str, Any]:
    """容錯讀取 `entities_json`：缺值、舊資料或壞資料一律補成 v1 的骨架，壞的角色卡略過。"""
    source = raw if isinstance(raw, Mapping) else {}
    raw_npcs = source.get("npcs") if isinstance(source.get("npcs"), Mapping) else {}
    npcs: dict[str, Any] = {}
    for key, card in raw_npcs.items():
        if isinstance(key, str) and isinstance(card, Mapping) and isinstance(card.get("name"), str) and card["name"]:
            npcs[key] = copy.deepcopy(dict(card))
    raw_locations = source.get("locations") if isinstance(source.get("locations"), list) else []
    locations = [item for item in raw_locations if isinstance(item, str) and item]
    entities: dict[str, Any] = {"v": WORLD_VERSION, "npcs": npcs, "locations": locations}
    protagonist = source.get("protagonist")
    if isinstance(protagonist, Mapping) and _is_int(protagonist.get("age")):
        entities["protagonist"] = {"age": protagonist["age"], "age_at_month": _month_or_zero(protagonist.get("age_at_month"))}
    return entities


def _normalized_goals(raw: Any) -> dict[str, list[dict[str, Any]]]:
    source = raw if isinstance(raw, Mapping) else {}
    result: dict[str, list[dict[str, Any]]] = {"active": [], "archived": []}
    for name in result:
        items = source.get(name)
        if isinstance(items, list):
            result[name] = [copy.deepcopy(dict(item)) for item in items if isinstance(item, Mapping) and isinstance(item.get("text"), str) and item["text"]]
    return result


def _normalized_progress(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: {"triggered_turn": value.get("triggered_turn"), "extended": bool(value.get("extended"))}
        for key, value in raw.items() if isinstance(key, str) and isinstance(value, Mapping)
    }


@dataclass(frozen=True)
class WorldState:
    """一個存檔目前的世界狀態（純資料；不要原地修改，`apply_proposal` 回傳新的）。"""

    entities: dict[str, Any]
    goals: dict[str, list[dict[str, Any]]]
    milestone_progress: dict[str, dict[str, Any]]
    elapsed_months: int = 0
    current_location: str = ""
    status_summary: str = ""

    @classmethod
    def from_fields(
        cls, *, entities: Any, goals: Any, milestone_progress: Any, elapsed_months: Any = 0, current_location: Any = "", status_summary: Any = "",
    ) -> "WorldState":
        return cls(
            entities=_normalized_entities(entities), goals=_normalized_goals(goals), milestone_progress=_normalized_progress(milestone_progress),
            elapsed_months=_month_or_zero(elapsed_months),
            current_location=current_location if isinstance(current_location, str) else "",
            status_summary=status_summary if isinstance(status_summary, str) else "",
        )

    def to_fields(self) -> dict[str, Any]:
        """對應存檔欄位的值（深拷貝，呼叫端可直接交給 Repository 序列化）。"""
        return {
            "entities_json": copy.deepcopy(self.entities), "goals_json": copy.deepcopy(self.goals),
            "milestone_progress_json": copy.deepcopy(self.milestone_progress), "elapsed_months": self.elapsed_months,
            "current_location": self.current_location, "status_summary": self.status_summary,
        }


@dataclass(frozen=True)
class Notice:
    """給玩家的通知（`state` 事件）。目前只有年齡更正；`text` 是伺服器產生的繁體中文。"""

    kind: str
    text: str
    name: str
    before: int | None = None
    after: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "name": self.name, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class CharacterAge:
    """審查用的在場角色（含主角）：名稱與目前年齡（未知為 None）。只以名稱比對；第二人稱（你）不推論為主角（使用者 2026-11-09 決定：
    審查針對未成年**角色**，不對玩家本體設限）。"""

    name: str
    age: int | None


@dataclass(frozen=True)
class ProposalContext:
    """`apply_proposal` 需要的一切（全部是回合開始時凍結的純資料）。"""

    script: Mapping[str, Any]
    protagonist: Mapping[str, Any]            # {"character": {...}, "anchor": {...} | None}
    turn_number: int                          # 正要完成的輪數
    action: str                               # 本回合玩家行動與完整敘事（用來判斷誰出現過）
    narration: str
    tier: str                                 # 存檔的內容模式（規則層用）
    max_months: int                           # 每回合經過月數上限（資料庫設定表，呼叫端現讀）
    ending_due: bool = False                  # 回合開始時由 `compute_ending_due` 判定
    adult_age: int = DEFAULT_ADULT_AGE
    markers: tuple[str, ...] = ()             # 不公開的標記字串：含它的項目一律丟棄


@dataclass(frozen=True)
class ProposalResult:
    world: WorldState
    notices: tuple[Notice, ...]
    dropped: int                               # 被丟棄的提議項目數（只計數量）
    ending: bool                               # 這個回合完成後存檔是否完結
    triggered: tuple[str, ...] = ()            # 本次新觸發的里程碑編號


# ---- 基本工具 ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _month_or_zero(value: Any) -> int:
    return value if _is_int(value) and value > 0 else 0


def name_key(name: str) -> str:
    """名稱比對用的鍵：單行化、NFC、不分大小寫。"""
    return fold(normalize_single_line(name))


def name_appears(name: str, text: str) -> bool:
    """名稱是否出現在文字中（NFC、不分大小寫的子字串）。單字的名稱可能誤中其他詞，這是已知的取捨。"""
    key = name_key(name)
    return bool(key) and key in fold(text)


def current_age(age: Any, age_at_month: Any, elapsed_months: int) -> int | None:
    """年齡紀錄的目前年齡：紀錄的年齡加上紀錄後經過的整年數；沒有紀錄回傳 None。"""
    if not _is_int(age):
        return None
    return age + max(0, elapsed_months - _month_or_zero(age_at_month)) // 12


def character_of(protagonist: Mapping[str, Any] | None) -> Mapping[str, Any]:
    character = protagonist.get("character") if isinstance(protagonist, Mapping) else None
    return character if isinstance(character, Mapping) else {}


def protagonist_from_session(protagonist_json: Any, player_name: str = "", character_age: Any = None) -> dict[str, Any]:
    """主角資料；遷移之前建立的存檔沒有 `protagonist_json`，以 `player_name`、`character_age` 補成同樣的結構。"""
    if isinstance(protagonist_json, Mapping) and isinstance(protagonist_json.get("character"), Mapping):
        return {"character": dict(protagonist_json["character"]), "anchor": protagonist_json.get("anchor")}
    return {"character": {"name": player_name, "age": character_age if _is_int(character_age) else None}, "anchor": None}


def protagonist_age(entities: Mapping[str, Any], protagonist: Mapping[str, Any], elapsed_months: int) -> int | None:
    """主角目前的年齡：有更正紀錄時以紀錄為準，否則是角色卡的年齡（開存檔時，月數 0）。"""
    record = entities.get("protagonist")
    if isinstance(record, Mapping) and _is_int(record.get("age")):
        return current_age(record["age"], record.get("age_at_month"), elapsed_months)
    return current_age(character_of(protagonist).get("age"), 0, elapsed_months)


def calendar_year(script: Mapping[str, Any], elapsed_months: int) -> int | None:
    """遊戲內年份 = 曆法起始年加經過的整年；劇本沒有曆法回傳 None。"""
    calendar = script.get("calendar")
    start = calendar.get("start_year") if isinstance(calendar, Mapping) else None
    return start + elapsed_months // 12 if _is_int(start) else None


def find_script_character(script: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    wanted = name_key(name)
    for character in script.get("characters") or []:
        if isinstance(character, Mapping) and isinstance(character.get("name"), str) and name_key(character["name"]) == wanted:
            return character
    return None


def _phrases(text: Any) -> list[str]:
    """把一段中文描述依標點拆成至多 8 字的短語（去重、保持順序），作為劇本角色的特質標籤。"""
    if not isinstance(text, str):
        return []
    pieces: list[str] = []
    current = []
    for char in normalize_single_line(text):
        if char in "，,、；;。.！!？? 　":
            if current:
                pieces.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        pieces.append("".join(current))
    result: list[str] = []
    for piece in pieces:
        label = piece[:TRAIT_LABEL_MAX]
        if label and label not in result:
            result.append(label)
    return result[:SCRIPT_TRAITS_PER_SOURCE]


def script_character_card(character: Mapping[str, Any], turn_number: int) -> dict[str, Any]:
    """劇本角色的角色卡（第一次出現時建立）：年齡取自劇本，個性為表層特質、隱藏特質為隱藏特質，階段 `surface`。"""
    traits = [{"label": label, "layer": LAYER_SURFACE} for label in _phrases(character.get("personality"))]
    traits += [{"label": label, "layer": LAYER_HIDDEN} for label in _phrases(character.get("hidden_trait"))]
    card: dict[str, Any] = {
        "name": character["name"], "traits": traits[:TRAIT_MAX_COUNT], "stage": STAGE_SURFACE,
        "last_seen_turn": turn_number, "origin": ORIGIN_SCRIPT,
    }
    if _is_int(character.get("age")) and AGE_MIN <= character["age"] <= AGE_MAX:
        card["age"] = character["age"]
        card["age_at_month"] = 0
    return card


def card_age(card: Mapping[str, Any], elapsed_months: int) -> int | None:
    return current_age(card.get("age"), card.get("age_at_month"), elapsed_months)


def known_characters(world: WorldState, script: Mapping[str, Any], protagonist: Mapping[str, Any]) -> list[CharacterAge]:
    """審查用的在場角色名單：主角、有年齡紀錄的 NPC 卡，以及還沒建卡的劇本角色（年齡取自劇本）。年齡未知者不列入。"""
    elapsed = world.elapsed_months
    result = [CharacterAge(str(character_of(protagonist).get("name") or ""), protagonist_age(world.entities, protagonist, elapsed))]
    seen = {name_key(result[0].name)} if result[0].name else set()
    for key, card in world.entities["npcs"].items():
        age = card_age(card, elapsed)
        if age is not None and key not in seen:
            result.append(CharacterAge(card["name"], age))
            seen.add(key)
    for character in script.get("characters") or []:
        if not isinstance(character, Mapping) or not isinstance(character.get("name"), str):
            continue
        key = name_key(character["name"])
        age = character.get("age")
        if key not in seen and _is_int(age):
            result.append(CharacterAge(character["name"], age))
            seen.add(key)
    return [item for item in result if item.name and item.age is not None]


# ---- 里程碑與結局 ---------------------------------------------------------------------------


def _milestone_reached(milestone: Mapping[str, Any], age: int | None, year: int | None) -> bool:
    trigger = milestone.get("trigger")
    if not isinstance(trigger, Mapping) or not _is_int(trigger.get("value")):
        return False
    if trigger.get("kind") == TRIGGER_AGE:
        return age is not None and age >= trigger["value"]
    if trigger.get("kind") == TRIGGER_YEAR:
        return year is not None and year >= trigger["value"]
    return False


def pending_milestones(script: Mapping[str, Any], progress: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """已觸發而尚未延伸的里程碑（依劇本順序）。"""
    return [
        milestone for milestone in script.get("milestones") or []
        if isinstance(milestone, Mapping) and milestone.get("id") in progress and not progress[milestone["id"]].get("extended")
    ]


def compute_ending_due(script: Mapping[str, Any], world: WorldState, protagonist: Mapping[str, Any]) -> bool:
    """回合開始時是否為結局回合：劇本為自訂結局、有壽終年齡，且主角目前年齡不低於它。"""
    if script.get("ending_mode") != ENDING_CUSTOM or not _is_int(script.get("ending_age")):
        return False
    age = protagonist_age(world.entities, protagonist, world.elapsed_months)
    return age is not None and age >= script["ending_age"]


# ---- 提議的驗證 ---------------------------------------------------------------------------


class _Applier:
    """一次 `apply_proposal` 的工作區：在深拷貝上修改，最後凍結成新的 `WorldState`。"""

    def __init__(self, world: WorldState, proposal: Mapping[str, Any], ctx: ProposalContext) -> None:
        self.ctx = ctx
        self.proposal = proposal
        self.entities = copy.deepcopy(world.entities)
        self.goals = copy.deepcopy(world.goals)
        self.progress = copy.deepcopy(world.milestone_progress)
        self.location = world.current_location
        self.summary = world.status_summary
        self.elapsed = world.elapsed_months
        self.dropped = 0
        self.notices: list[Notice] = []
        self.triggered: list[str] = []
        self.created: set[str] = set()
        self.character = character_of(ctx.protagonist)
        self.protagonist_key = name_key(str(self.character.get("name") or ""))
        self.markers = tuple(marker.lower() for marker in ctx.markers if marker)

    # -- 文字 --

    def basic(self, value: Any, maximum: int) -> str | None:
        """去除控制字元、單行化，長度 1 至 maximum；不合格回傳 None。"""
        if not isinstance(value, str):
            return None
        text = normalize_single_line(value)
        return text if 1 <= len(text) <= maximum else None

    def checked(self, value: Any, maximum: int) -> str | None:
        """`basic` 之外還要通過規則層（任何非通過的結果）與標記字串檢查。"""
        text = self.basic(value, maximum)
        if text is None:
            return None
        if any(marker in text.lower() for marker in self.markers):
            return None
        result = check_text_rules([text], tier=self.ctx.tier, adult_age=self.ctx.adult_age)
        return text if result.verdict == REVIEW_PASS else None

    # -- 月數與里程碑 --

    def months(self) -> None:
        value = self.proposal.get("elapsed_months")
        increment = value if _is_int(value) and value > 0 else 0
        self.elapsed += min(increment, max(0, self.ctx.max_months))

    def milestones(self) -> None:
        age = protagonist_age(self.entities, self.ctx.protagonist, self.elapsed)
        year = calendar_year(self.ctx.script, self.elapsed)
        for milestone in self.ctx.script.get("milestones") or []:
            if not isinstance(milestone, Mapping) or not isinstance(milestone.get("id"), str):
                continue
            if milestone["id"] in self.progress or not _milestone_reached(milestone, age, year):
                continue
            self.progress[milestone["id"]] = {"triggered_turn": self.ctx.turn_number, "extended": False}
            self.triggered.append(milestone["id"])

    # -- 地點 --

    def location_step(self) -> None:
        value = self.proposal.get("current_location")
        if value is None or value == "":
            return
        text = self.basic(value, LOCATION_MAX)
        if text is None:
            self.dropped += 1
            return
        wanted = fold(text)
        known = [item for item in self.ctx.script.get("locations") or [] if isinstance(item, str)] + list(self.entities["locations"])
        for item in known:
            if fold(item) == wanted:
                self.location = item
                return
        added = self.checked(text, LOCATION_MAX)
        if added is None or len(self.entities["locations"]) >= NEW_LOCATION_MAX_COUNT:
            self.dropped += 1
            return
        self.entities["locations"].append(added)
        self.location = added

    def summary_step(self) -> None:
        value = self.proposal.get("status_summary")
        if value is None or value == "":
            return
        text = self.checked(value, STATUS_SUMMARY_MAX)
        if text is None:
            self.dropped += 1
        else:
            self.summary = text

    # -- NPC --

    def appearances(self) -> None:
        """名稱出現在行動或敘事文字的卡片更新最近出現的輪數；第一次出現的劇本角色建卡。不依賴模型回報。"""
        seen_text = f"{self.ctx.action}\n{self.ctx.narration}"
        for card in self.entities["npcs"].values():
            if name_appears(card["name"], seen_text):
                card["last_seen_turn"] = self.ctx.turn_number
        for character in self.ctx.script.get("characters") or []:
            if not isinstance(character, Mapping) or not isinstance(character.get("name"), str):
                continue
            key = name_key(character["name"])
            if key and key != self.protagonist_key and key not in self.entities["npcs"] and name_appears(character["name"], seen_text):
                self.entities["npcs"][key] = script_character_card(character, self.ctx.turn_number)
                self.created.add(key)

    def npc_updates(self) -> None:
        updates = self.proposal.get("npc_updates")
        for update in updates if isinstance(updates, list) else []:
            if not isinstance(update, Mapping):
                self.dropped += 1
                continue
            self.one_update(update)

    def one_update(self, update: Mapping[str, Any]) -> None:
        name = self.checked(update.get("name"), CHARACTER_NAME_MAX)
        if name is None:
            self.dropped += 1
            return
        key = name_key(name)
        if key == self.protagonist_key:
            self.protagonist_age_update(update.get("age"))        # 主角的角色卡由玩家決定，模型只能提議年齡
            return
        card = self.entities["npcs"].get(key)
        if card is None:
            script_character = find_script_character(self.ctx.script, name)
            card = script_character_card(script_character, self.ctx.turn_number) if script_character else {
                "name": name, "traits": [], "stage": STAGE_SURFACE, "last_seen_turn": self.ctx.turn_number, "origin": ORIGIN_STORY,
            }
            self.entities["npcs"][key] = card
            self.created.add(key)
        card["last_seen_turn"] = self.ctx.turn_number
        self.traits(card, update.get("traits"))
        self.stage(card, key, update.get("stage"))
        if update.get("age") is not None:
            self.card_age_update(card, update.get("age"))
        if update.get("anchor_fact") is not None:
            self.anchor_fact(card, update.get("anchor_fact"))

    def traits(self, card: dict[str, Any], proposed: Any) -> None:
        for item in proposed if isinstance(proposed, list) else ():
            label = self.checked(item.get("label"), TRAIT_LABEL_MAX) if isinstance(item, Mapping) else None
            layer = item.get("layer") if isinstance(item, Mapping) else None
            if label is None or layer not in LAYERS:
                self.dropped += 1
            elif any(fold(existing["label"]) == fold(label) for existing in card["traits"]):
                continue                                          # 重複的特質不是錯誤，也不計丟棄
            elif len(card["traits"]) >= TRAIT_MAX_COUNT:
                self.dropped += 1
            else:
                card["traits"].append({"label": label, "layer": layer})

    def stage(self, card: dict[str, Any], key: str, proposed: Any) -> None:
        if proposed is None or key in self.created:
            return                                                # 新出現的 NPC 不論提議什麼，一律從 surface 起
        if proposed not in STAGES:
            self.dropped += 1
            return
        current = card.get("stage") if card.get("stage") in STAGES else STAGE_SURFACE
        if current == proposed or current == STAGE_REVEALED:
            return
        if (current, proposed) in _ALLOWED_TRANSITIONS:
            card["stage"] = proposed
        elif current == STAGE_SURFACE and proposed == STAGE_REVEALED:
            card["stage"] = _NEXT_STAGE[STAGE_SURFACE]            # 跳階降為只前進一階

    def anchor_fact(self, card: dict[str, Any], proposed: Any) -> None:
        text = self.checked(proposed, ANCHOR_FACT_MAX)
        if text is None:
            self.dropped += 1
        elif not card.get("anchor_fact"):
            card["anchor_fact"] = text                            # 已有錨定事實就不覆寫

    def valid_age(self, proposed: Any) -> int | None:
        if _is_int(proposed) and AGE_MIN <= proposed <= AGE_MAX:
            return proposed
        self.dropped += 1
        return None

    def card_age_update(self, card: dict[str, Any], proposed: Any) -> None:
        age = self.valid_age(proposed)
        if age is None:
            return
        before = card_age(card, self.elapsed)
        if before == age:
            return
        card["age"], card["age_at_month"] = age, self.elapsed
        if before is not None:                                    # 第一次寫入年齡不是更正
            self.notices.append(self.age_notice(card["name"], before, age))

    def protagonist_age_update(self, proposed: Any) -> None:
        if proposed is None:
            return
        age = self.valid_age(proposed)
        if age is None:
            return
        before = protagonist_age(self.entities, self.ctx.protagonist, self.elapsed)
        if before == age:
            return
        self.entities["protagonist"] = {"age": age, "age_at_month": self.elapsed}
        if before is not None:
            self.notices.append(self.age_notice(str(self.character.get("name") or ""), before, age))

    @staticmethod
    def age_notice(name: str, before: int, after: int) -> Notice:
        return Notice(NOTICE_AGE_CORRECTED, f"{name}的年齡由 {before} 歲變更為 {after} 歲", name, before, after)

    def evict(self) -> None:
        """超過 30 張卡時淘汰最久沒出現者；主角錨定事實所指的人物不被淘汰。"""
        anchor = self.ctx.protagonist.get("anchor") if isinstance(self.ctx.protagonist, Mapping) else None
        protected = {name_key(anchor["ref"])} if isinstance(anchor, Mapping) and anchor.get("kind") == ANCHOR_CHARACTER and isinstance(anchor.get("ref"), str) else set()
        npcs = self.entities["npcs"]
        while len(npcs) > NPC_MAX_COUNT:
            candidates = [(card.get("last_seen_turn") if _is_int(card.get("last_seen_turn")) else 0, key) for key, card in npcs.items() if key not in protected]
            if not candidates:
                break
            victim = min(candidates, key=lambda item: item[0])[1]      # 同分取最早建立者（字典保持插入順序）
            del npcs[victim]

    # -- 目標 --

    def goals_step(self) -> None:
        done = self.proposal.get("goals_done")
        for value in done if isinstance(done, list) else ():
            text = self.basic(value, GOAL_TEXT_MAX)
            index = next((i for i, goal in enumerate(self.goals["active"]) if text is not None and fold(goal["text"]) == fold(text)), None)
            if index is None:
                self.dropped += 1
                continue
            goal = self.goals["active"].pop(index)
            self.goals["archived"].append({**goal, "done_turn": self.ctx.turn_number})
        self.goals["archived"] = self.goals["archived"][-ARCHIVED_GOALS_MAX:]
        added = self.proposal.get("goals_add")
        for item in added if isinstance(added, list) else ():
            self.one_goal(item)

    def one_goal(self, item: Any) -> None:
        text = self.checked(item.get("text"), GOAL_TEXT_MAX) if isinstance(item, Mapping) else None
        if text is None:
            self.dropped += 1
            return
        milestone_id = item.get("milestone_id")
        if milestone_id not in (None, ""):
            pending = {m["id"] for m in pending_milestones(self.ctx.script, self.progress)}
            if not isinstance(milestone_id, str) or milestone_id not in pending:
                self.dropped += 1                                  # 只能延伸「已觸發而尚未延伸」的里程碑
                return
        else:
            milestone_id = None
        if any(fold(goal["text"]) == fold(text) for goal in self.goals["active"]):
            return
        if len(self.goals["active"]) >= GOAL_MAX_ACTIVE:
            self.dropped += 1
            return
        self.goals["active"].append({"text": text, "milestone_id": milestone_id, "turn": self.ctx.turn_number})
        if milestone_id is not None:
            self.progress[milestone_id]["extended"] = True

    # -- 結局 --

    def ending(self) -> bool:
        custom = self.ctx.script.get("ending_mode") == ENDING_CUSTOM
        return bool(self.ctx.ending_due or (custom and self.proposal.get("ending_proposed") is True))


def apply_proposal(world: WorldState, proposal: Mapping[str, Any], ctx: ProposalContext) -> ProposalResult:
    """驗證模型的狀態提議，回傳新的世界狀態、給玩家的通知與丟棄的項目數。處理順序：月數 → 里程碑（以新月數）→ 地點 → 狀態摘要 →
    NPC（出現、建卡、更新）→ 目標（先完成再新增）→ 淘汰 → 結局。每個項目獨立驗證，不合格者丟棄、其餘照常處理。"""
    if not isinstance(proposal, Mapping):
        return ProposalResult(world, (), 1, bool(ctx.ending_due))
    work = _Applier(world, proposal, ctx)
    work.months()
    work.milestones()
    work.location_step()
    work.summary_step()
    work.appearances()
    work.npc_updates()
    work.goals_step()
    work.evict()
    new_world = WorldState(
        entities=work.entities, goals=work.goals, milestone_progress=work.progress, elapsed_months=work.elapsed,
        current_location=work.location, status_summary=work.summary,
    )
    return ProposalResult(new_world, tuple(work.notices), work.dropped, work.ending(), tuple(work.triggered))
