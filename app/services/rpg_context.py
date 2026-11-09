"""RPG 回合送給模型的上下文組裝（見 rpg-turn-context、rpg-opening 規格與 rpg-turn-engine 的 design D11、D12）。

**純函式**：不碰資料庫、網路與時間，不匯入資料模型、Repository 或 Flask（`tests/architecture/test_rpg_pure_modules.py` 守住）。

送給 Gemini 以外的 RPG 用途模型的內容 = 固定的系統指令（`rpg_prompt`，只有列舉參數）＋使用者內容區：
- 第一個 `user` 內容放資料區塊，順序固定：記事簿 → 當前世界狀態（所在地點、遊戲內時間、主角與來歷、在場人物切片、進行中的目標、已觸發而待延伸的里程碑、
  結局資訊）→ 劇本資料；之後是歷史（`user` 為過去的行動、`model` 為過去的劇情，只含最近 10 個**完成**的回合），最後一個 `user` 是本次行動。
  相鄰的同角色內容會合併（部分的順序不變），所以沒有歷史時資料區塊與本次行動在同一個 `user` 內容裡。
- **RPG 不使用摘要**：長期記憶是記事簿（玩家可編輯）；特質與階段另存在 NPC 角色卡，不只存在於記事簿。
- 完成輪數未滿 10 輪時，第 0 幕（已生成或退回的初始對話；非空時）是歷史最前面的一則 AI 訊息，之後隨視窗滑出；它不改變「最近 10 輪」的定義。
  `interrupted`、`blocked`、`streaming` 的回合不進入歷史。
- **玩家與作者的文字只出現在這裡**（資料區塊），絕不進入系統指令。區塊以固定的標題括號（`【標題】…【/標題】`）分隔；文字內含的 `【` `】` 一律換成
  `〔` `〕`（`neutralize`），所以無法偽造區塊的開頭或結尾。
- 在場人物切片最多 6 位：最近 3 輪出現過的、名稱出現在本次文字的，加上主角錨定事實所指的人物（一律優先）；每位特質至多 6 項、每項 8 字內，錨定事實 100 字內。
  劇本的 `hidden_trait` **不放進劇本資料區塊**，只透過角色卡切片（帶階段）進入上下文，使隱藏特質隨弧線階段逐步呈現。
- 不截斷任何區塊；大小由各欄位的上限保證，測試以全部上限組裝並斷言字數上界。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.llm.types import LLMContent
from app.services import rpg_prompt as prompt
from app.services.protagonist import ANCHOR_CHARACTER, ANCHOR_LOCATION, ANCHOR_MILESTONE, origin_of
from app.services.rpg_world import (
    ANCHOR_FACT_MAX, ENDING_CUSTOM, LAYER_HIDDEN, LAYER_SURFACE, TRAIT_LABEL_MAX, TRAIT_MAX_COUNT, WorldState, calendar_year, card_age,
    character_of, known_characters, name_appears, name_key, pending_milestones, protagonist_age, script_character_card,
)

TURN_COMPLETE = "complete"              # 與 `app.models.rpg_session.TURN_COMPLETE` 相同（測試核對）；純模組不匯入資料模型
HISTORY_TURNS = 10
NPC_SLICE_MAX = 6
NPC_RECENT_TURNS = 3
EMPTY_NOTEBOOK = "（目前是空的）"
EMPTY_LIST = "（無）"


@dataclass(frozen=True)
class HistoryTurn:
    """歷史中的一個回合（由 Service 從資料庫列轉成純資料）。只有 `status == "complete"` 且有輪數的回合會進入上下文。"""

    turn_number: int | None
    status: str
    action: str
    narration: str


@dataclass(frozen=True)
class NpcSlice:
    """一位在場人物的切片（已套用上限）。"""

    name: str
    age: int | None
    stage: str
    traits: tuple[tuple[str, str], ...]        # (label, layer)
    anchor_fact: str
    link: str                                  # 主角的錨定事實（所指的人物是他時）；沒有則為空字串


def neutralize(text: Any) -> str:
    """玩家、作者與模型的文字放進資料區塊前的處理：標題用的括號換成另一組，無法偽造區塊的開頭或結尾。"""
    value = text if isinstance(text, str) else ("" if text is None else str(text))
    return value.replace(prompt.OPEN_BRACKET, prompt.SAFE_OPEN_BRACKET).replace(prompt.CLOSE_BRACKET, prompt.SAFE_CLOSE_BRACKET)


def _block(label: str, body: str) -> str:
    return f"{prompt.block_open(label)}\n{body}\n{prompt.block_close(label)}"


def _join(items: Sequence[str]) -> str:
    return "、".join(item for item in items if item)


def _field(label: str, value: Any) -> str:
    return f"{label}：{neutralize(value)}" if isinstance(value, str) and value else ""


# ---- 歷史與視窗 -------------------------------------------------------------------------


def window_turns(history: Sequence[HistoryTurn]) -> list[HistoryTurn]:
    """最近 10 個完成的回合（依輪數由舊到新）。被擋、中斷與生成中的回合不在其中。"""
    complete = sorted(
        (turn for turn in history if turn.status == TURN_COMPLETE and isinstance(turn.turn_number, int)), key=lambda turn: turn.turn_number,
    )
    return complete[-HISTORY_TURNS:]


def _merge(messages: Sequence[tuple[str, str]]) -> tuple[LLMContent, ...]:
    """相鄰且同角色的內容合併成一個（保持順序），使角色交替。"""
    merged: list[tuple[str, list[str]]] = []
    for role, text in messages:
        if merged and merged[-1][0] == role:
            merged[-1][1].append(text)
        else:
            merged.append((role, [text]))
    return tuple(LLMContent(role, tuple(parts)) for role, parts in merged)


# ---- 在場人物 ---------------------------------------------------------------------------


def _anchor_of(protagonist: Mapping[str, Any]) -> Mapping[str, Any] | None:
    anchor = protagonist.get("anchor") if isinstance(protagonist, Mapping) else None
    return anchor if isinstance(anchor, Mapping) and isinstance(anchor.get("ref"), str) else None


def _slice_of(card: Mapping[str, Any], elapsed: int, link: str) -> NpcSlice:
    traits = tuple(
        (neutralize(trait["label"])[:TRAIT_LABEL_MAX], trait["layer"] if trait.get("layer") in (LAYER_SURFACE, LAYER_HIDDEN) else LAYER_SURFACE)
        for trait in (card.get("traits") or [])[:TRAIT_MAX_COUNT] if isinstance(trait, Mapping) and isinstance(trait.get("label"), str) and trait["label"]
    )
    return NpcSlice(
        name=neutralize(card["name"]), age=card_age(card, elapsed), stage=card.get("stage") if card.get("stage") in prompt.STAGE_LABELS else "surface",
        traits=traits, anchor_fact=neutralize(card.get("anchor_fact") or "")[:ANCHOR_FACT_MAX], link=link,
    )


def select_slices(
    world: WorldState, script: Mapping[str, Any], protagonist: Mapping[str, Any], *, turn_number: int, mention_text: str,
) -> list[NpcSlice]:
    """在場人物切片：最近 3 輪出現過的、名稱出現在 `mention_text` 的，加上主角錨定事實所指的人物（優先）；依最近出現排序取 6 位。
    還沒建卡的劇本角色（被提到或是錨定對象）以劇本資料建立臨時的卡片，不寫回世界狀態。"""
    anchor = _anchor_of(protagonist)
    target = name_key(anchor["ref"]) if anchor and anchor.get("kind") == ANCHOR_CHARACTER else None
    link = neutralize(anchor.get("text") or "") if anchor else ""
    protagonist_key = name_key(str(character_of(protagonist).get("name") or ""))
    entries: list[tuple[int, int, int, Mapping[str, Any], str]] = []     # (優先, -最近出現, 順序, 卡片, 鍵)
    seen: set[str] = set()
    for order, (key, card) in enumerate(world.entities["npcs"].items()):
        last_seen = card.get("last_seen_turn") if isinstance(card.get("last_seen_turn"), int) else 0
        if key == target or last_seen >= turn_number - NPC_RECENT_TURNS or name_appears(card["name"], mention_text):
            entries.append((0 if key == target else 1, -last_seen, order, card, key))
            seen.add(key)
    base = len(world.entities["npcs"])
    for order, character in enumerate(script.get("characters") or []):
        if not isinstance(character, Mapping) or not isinstance(character.get("name"), str):
            continue
        key = name_key(character["name"])
        if key in seen or key == protagonist_key or not (key == target or name_appears(character["name"], mention_text)):
            continue
        entries.append((0 if key == target else 1, 1, base + order, script_character_card(character, 0), key))
        seen.add(key)
    entries.sort(key=lambda item: item[:3])
    return [_slice_of(card, world.elapsed_months, link if key == target else "") for _, _, _, card, key in entries[:NPC_SLICE_MAX]]


def _render_slice(item: NpcSlice) -> str:
    age = f"{item.age} 歲" if item.age is not None else "未知"
    lines = [f"- {item.name}｜年齡：{age}｜階段：{prompt.STAGE_LABELS[item.stage]}"]
    by_layer = {layer: [label for label, kind in item.traits if kind == layer] for layer in (LAYER_SURFACE, LAYER_HIDDEN)}
    traits = "；".join(f"{prompt.LAYER_LABELS[layer]}：{_join(labels)}" for layer, labels in by_layer.items() if labels)
    lines += [f"  特質：{traits}"] if traits else []
    lines += [f"  錨定事實：{item.anchor_fact}"] if item.anchor_fact else []
    lines += [f"  與主角的聯繫：{item.link}"] if item.link else []
    return "\n".join(lines)


# ---- 世界狀態與劇本 ---------------------------------------------------------------------


def _time_text(script: Mapping[str, Any], elapsed: int) -> str:
    years, months = divmod(max(0, elapsed), 12)
    passed = "故事剛開始" if elapsed <= 0 else "已過 " + " ".join(part for part in (f"{years} 年" if years else "", f"{months} 個月" if months else "") if part)
    year = calendar_year(script, elapsed)
    calendar = script.get("calendar")
    if year is not None and isinstance(calendar, Mapping):
        return f"{neutralize(calendar.get('name') or '')}{year}年（{passed}）"
    return passed


def _protagonist_lines(world: WorldState, script: Mapping[str, Any], protagonist: Mapping[str, Any]) -> list[str]:
    character = character_of(protagonist)
    age = protagonist_age(world.entities, protagonist, world.elapsed_months)
    lines = [
        _field("姓名", character.get("name")), f"年齡：{age} 歲" if age is not None else "", f"來歷：{prompt.ORIGIN_LABELS[origin_of(character)]}",
        _field("個性", character.get("personality")), _field("外貌", character.get("appearance")), _field("職業", character.get("occupation")),
        _field("說話方式", character.get("speech_style")), _field("隱藏特質", character.get("hidden_trait")),
    ]
    anchor = _anchor_of(protagonist)
    if anchor and anchor.get("kind") in (ANCHOR_LOCATION, ANCHOR_MILESTONE):
        lines.append(f"與劇本的聯繫：{_anchor_target_text(anchor, script)}：{neutralize(anchor.get('text') or '')}")
    return [f"- {line}" for line in lines if line]


def _anchor_target_text(anchor: Mapping[str, Any], script: Mapping[str, Any]) -> str:
    ref = anchor["ref"]
    if anchor.get("kind") == ANCHOR_MILESTONE:
        for milestone in script.get("milestones") or []:
            if isinstance(milestone, Mapping) and milestone.get("id") == ref:
                return f"里程碑「{neutralize(milestone.get('title') or '')}」"
        return "里程碑"
    if anchor.get("kind") == ANCHOR_LOCATION:
        return f"地點「{neutralize(ref)}」"
    return f"人物「{neutralize(ref)}」"


def _ending_lines(script: Mapping[str, Any], ending_due: bool) -> list[str]:
    if script.get("ending_mode") != ENDING_CUSTOM:
        return ["這個劇本沒有結局，是沒有盡頭的開放世界。"]
    lines = [_field("結局說明", script.get("ending_text"))]
    if isinstance(script.get("ending_age"), int):
        lines.append(f"主角壽終的年齡：{script['ending_age']} 歲")
    lines.append("本回合是結局回合：是" if ending_due else "本回合是結局回合：否")
    return [line for line in lines if line]


def _goal_lines(world: WorldState) -> list[str]:
    return [f"{index}. {neutralize(goal['text'])}" for index, goal in enumerate(world.goals["active"], start=1)] or [EMPTY_LIST]


def _state_block(
    world: WorldState, script: Mapping[str, Any], protagonist: Mapping[str, Any], slices: Sequence[NpcSlice], ending_due: bool,
) -> str:
    pending = pending_milestones(script, world.milestone_progress)
    milestone_lines = [
        f"- 「{neutralize(m.get('title') or '')}」" + (f"：{neutralize(m['note'])}" if isinstance(m.get("note"), str) and m["note"] else "") for m in pending
    ] or [EMPTY_LIST]
    parts = [
        f"所在地點：{neutralize(world.current_location) or '（尚未確定）'}", f"遊戲內時間：{_time_text(script, world.elapsed_months)}",
        _field("目前局勢", world.status_summary), "主角：", *_protagonist_lines(world, script, protagonist),
        "在場人物：", *([_render_slice(item) for item in slices] or [EMPTY_LIST]),
        "進行中的目標：", *_goal_lines(world), "已觸發而待延伸的里程碑：", *milestone_lines, "結局資訊：", *_ending_lines(script, ending_due),
    ]
    return _block(prompt.LABEL_STATE, "\n".join(part for part in parts if part))


def _script_block(script: Mapping[str, Any], ages: Mapping[str, int]) -> str:
    characters = []
    for character in script.get("characters") or []:
        if not isinstance(character, Mapping) or not isinstance(character.get("name"), str):
            continue
        age = ages.get(name_key(character["name"]))
        details = _join([
            f"年齡：{age} 歲" if age is not None else "", _field("職業", character.get("occupation")), _field("個性", character.get("personality")),
            _field("外貌", character.get("appearance")), _field("說話方式", character.get("speech_style")),
        ])
        characters.append(f"- {neutralize(character['name'])}｜{details}" if details else f"- {neutralize(character['name'])}")      # 不含隱藏特質
    locations = _join([neutralize(item) for item in script.get("locations") or [] if isinstance(item, str)])
    parts = [
        _field("標題", script.get("title")), _field("世界", script.get("world_name")), _field("背景故事", script.get("background")),
        _field("規則與狀態轉移規則", script.get("rules")), "角色：", *(characters or [EMPTY_LIST]), f"地點：{locations or EMPTY_LIST}",
    ]
    return _block(prompt.LABEL_SCRIPT, "\n".join(part for part in parts if part))


def _ages_by_name(world: WorldState, script: Mapping[str, Any], protagonist: Mapping[str, Any]) -> dict[str, int]:
    return {name_key(item.name): item.age for item in known_characters(world, script, protagonist) if item.age is not None}


# ---- 對外的組裝函式 ---------------------------------------------------------------------


def build_turn_context(
    *, script: Mapping[str, Any], world: WorldState, protagonist: Mapping[str, Any], history: Sequence[HistoryTurn], opening_text: str | None,
    notebook: str, action: str, tier: str, turn_number: int, ending_due: bool, marker: str, language: str = prompt.DEFAULT_LANGUAGE,
) -> tuple[str, tuple[LLMContent, ...]]:
    """回合敘事的 `(系統指令, 內容)`。`turn_number` 是正要生成的輪數（已完成的輪數加 1）；`history` 可含任何狀態的回合，這裡只取最近 10 個完成的。"""
    origin = origin_of(character_of(protagonist))
    system = prompt.build_system(tier, language, origin, ending_due, marker)
    slices = select_slices(world, script, protagonist, turn_number=turn_number, mention_text=action)
    data = "\n\n".join([
        prompt.DATA_NOTE, _block(prompt.LABEL_NOTEBOOK, neutralize(notebook) or EMPTY_NOTEBOOK),
        _state_block(world, script, protagonist, slices, ending_due), _script_block(script, _ages_by_name(world, script, protagonist)),
    ])
    messages: list[tuple[str, str]] = [("user", data)]
    if turn_number - 1 < HISTORY_TURNS and isinstance(opening_text, str) and opening_text.strip():
        messages.append(("model", neutralize(opening_text)))
    for turn in window_turns(history):
        messages.append(("user", _block(prompt.LABEL_PAST_ACTION, neutralize(turn.action))))
        messages.append(("model", neutralize(turn.narration)))
    messages.append(("user", _block(prompt.LABEL_ACTION, neutralize(action))))
    return system, _merge(messages)


def build_opening_context(
    *, script: Mapping[str, Any], protagonist: Mapping[str, Any], tier: str, marker: str, language: str = prompt.DEFAULT_LANGUAGE,
) -> tuple[str, tuple[LLMContent, ...]]:
    """第 0 幕（開場）的 `(系統指令, 內容)`：主角角色卡與來歷、主角與劇本的聯繫（可為空）、劇本資料。沒有記事簿、歷史與行動。"""
    character = character_of(protagonist)
    origin = origin_of(character)
    system = prompt.build_opening_system(tier, language, origin, marker)
    world = WorldState.from_fields(entities=None, goals=None, milestone_progress=None)
    anchor = _anchor_of(protagonist)
    blocks = [prompt.DATA_NOTE, _block(prompt.LABEL_OPENING_PROTAGONIST, "\n".join(_protagonist_lines(world, script, protagonist)))]
    if anchor:
        blocks.append(_block(prompt.LABEL_OPENING_ANCHOR, f"{_anchor_target_text(anchor, script)}：{neutralize(anchor.get('text') or '')}"))
    blocks.append(_script_block(script, _ages_by_name(world, script, protagonist)))
    return system, (LLMContent("user", ("\n\n".join(blocks),)),)


def build_state_context(
    *, script: Mapping[str, Any], world: WorldState, protagonist: Mapping[str, Any], action: str, narration: str, turn_number: int,
    ending_due: bool, marker: str, language: str = prompt.DEFAULT_LANGUAGE,
) -> tuple[str, tuple[LLMContent, ...]]:
    """狀態呼叫的 `(系統指令, 內容)`：本回合的行動與完整劇情、主角、在場人物（含本回合劇情中出現的）、現有目標、待延伸的里程碑（含編號）、已知地點與結局資訊。"""
    system = prompt.build_state_system(language, marker)
    slices = select_slices(world, script, protagonist, turn_number=turn_number, mention_text=f"{action}\n{narration}")
    character = character_of(protagonist)
    age = protagonist_age(world.entities, protagonist, world.elapsed_months)
    pending = pending_milestones(script, world.milestone_progress)
    known_locations = [item for item in script.get("locations") or [] if isinstance(item, str)] + list(world.entities["locations"])
    blocks = [
        prompt.DATA_NOTE,
        _block(prompt.LABEL_TURN_ACTION, neutralize(action)), _block(prompt.LABEL_TURN_STORY, neutralize(narration)),
        _block(prompt.LABEL_OPENING_PROTAGONIST, "\n".join([
            f"- 姓名：{neutralize(character.get('name') or '')}", f"- 年齡：{age} 歲" if age is not None else "- 年齡：未知",
            f"- 目前所在地點：{neutralize(world.current_location) or '（尚未確定）'}",
        ])),
        _block(prompt.LABEL_PRESENT_NPCS, "\n".join(_render_slice(item) for item in slices) or EMPTY_LIST),
        _block(prompt.LABEL_GOALS, "\n".join(_goal_lines(world))),
        _block(prompt.LABEL_MILESTONES, "\n".join(
            f"- 編號 {neutralize(m['id'])}：「{neutralize(m.get('title') or '')}」" + (f"（{neutralize(m['note'])}）" if isinstance(m.get("note"), str) and m["note"] else "")
            for m in pending
        ) or EMPTY_LIST),
        _block(prompt.LABEL_LOCATIONS, _join([neutralize(item) for item in known_locations]) or EMPTY_LIST),
        _block(prompt.LABEL_ENDING, "\n".join(_ending_lines(script, ending_due))),
    ]
    return system, (LLMContent("user", ("\n\n".join(blocks),)),)

