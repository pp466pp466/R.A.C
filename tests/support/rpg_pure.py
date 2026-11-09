"""RPG 純邏輯模組（世界狀態、上下文、輸出審查）的測試輔助：一份劇本複本、世界狀態與角色卡的建構。不碰資料庫。"""
from __future__ import annotations

from typing import Any

from app.services import rpg_world as rw
from app.services.rpg_world import WorldState
from tests.support.script_data import validated

MARKER = "a1b2c3d4e5f60718293a4b5c"


def make_script(**changes: Any) -> dict[str, Any]:
    """標準化後的劇本複本（`script_snapshot_json` 的形狀）：兩位角色、兩個地點、年齡與年份各一個里程碑、曆法起始年 1200。"""
    base = {
        "characters": [
            {"name": "林曉", "personality": "活潑好動，嘴硬心軟", "age": 16, "appearance": "身形瘦小", "hidden_trait": "怕黑，夜裡會哭"},
            {"name": "沈師姐", "personality": "沉穩", "age": 28, "appearance": "黑衣"},
        ],
        "locations": ["古劍山莊", "青雲宗山門"],
        "milestones": [
            {"id": "m_age", "title": "宗門大測", "trigger": {"kind": "age", "value": 20}, "note": "主角須通過考驗"},
            {"id": "m_year", "title": "百年大典", "trigger": {"kind": "year", "value": 1202}},
        ],
    }
    base.update(changes)
    return validated(**base).content


SCRIPT = make_script()


def world(**fields: Any) -> WorldState:
    base = {"entities": None, "goals": None, "milestone_progress": None, "elapsed_months": 0, "current_location": "", "status_summary": ""}
    base.update(fields)
    return WorldState.from_fields(**base)


def with_card(
    name: str = "阿福", *, stage: str = "surface", age: int | None = None, age_at_month: int = 0, last_seen: int = 1,
    traits: tuple = (), anchor_fact: str | None = None, origin: str = "story",
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "name": name, "traits": [{"label": label, "layer": layer} for label, layer in traits], "stage": stage, "last_seen_turn": last_seen, "origin": origin,
    }
    if age is not None:
        card["age"], card["age_at_month"] = age, age_at_month
    if anchor_fact:
        card["anchor_fact"] = anchor_fact
    return card


def world_with(*cards: dict[str, Any], **fields: Any) -> WorldState:
    entities = {"v": 1, "npcs": {rw.name_key(card["name"]): card for card in cards}, "locations": []}
    return world(entities=entities, **fields)
