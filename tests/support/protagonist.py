"""玩家主角的測試輔助：一張合法的角色卡、存進存檔的形狀，以及開存檔用的關鍵字（rpg-character-creation）。"""
from __future__ import annotations

from typing import Any

from app.services.protagonist import build_protagonist


def character(name: str = "旅人", age: int = 18, **extra: Any) -> dict[str, Any]:
    """一張欄位合法的角色卡（原始輸入）；`extra` 可覆蓋或新增欄位（值為 `...` 表示移除）。"""
    card: dict[str, Any] = {"name": name, "personality": "沉默愛思考", "age": age, "appearance": "高瘦，常穿灰衣"}
    for key, value in extra.items():
        if value is ...:
            card.pop(key, None)
        else:
            card[key] = value
    return card


def protagonist(name: str = "旅人", age: int = 18, anchor: dict[str, str] | None = None, **extra: Any) -> dict[str, Any]:
    """存進 `sessions.protagonist_json` 的形狀（給 `SessionRepository.create` 用）。角色卡補上 `image: None` 等標準化欄位。"""
    card = {"occupation": None, "speech_style": None, "hidden_trait": None, "image": None, **character(name, age, **extra)}
    return build_protagonist(card, anchor)


def start_kwargs(name: str = "旅人", age: int = 18, anchor: dict[str, str] | None = None, skip_anchor: bool | None = None, **extra: Any) -> dict[str, Any]:
    """`SessionService.start_session(current, version_id, **start_kwargs())` 的關鍵字：角色卡（原始輸入）加錨定事實或明確略過。
    沒有指定錨定事實時預設明確略過，讓不關心錨定事實的測試保持簡短。"""
    if anchor is None and skip_anchor is None:
        skip_anchor = True
    return {"protagonist": character(name, age, **extra), "anchor": anchor, "skip_anchor": skip_anchor}
