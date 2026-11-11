"""RPG 入口畫面的唯讀資料：可玩劇本清單、我的存檔清單與單一存檔的摘要（見 rpg-entry-api 規格、rpg-entry-ui 的 design D2、D3、D4、D7）。

**唯讀**：不呼叫模型、不碰錢包與額度、不寫入任何資料（連日誌也不寫：標題、筆名與主角姓名都是玩家文字衍生的內容）。

**不過年齡閘門**（`require_chat_access`）：`/game` 是登入後的預設頁面，尚未完成年齡確認的管理員也會落在這裡；清單不花成本，
內容模式對尚未確認年齡者本來就是安全模式（`AccessPolicy.content_access`），所以放行是安全的。真正會花成本的「開始」由開存檔
既有的閘門擋下。呼叫端（Controller）只需要確認已登入；受限登入狀態在更早的登入檢查就被擋下。

**可玩劇本**（`catalog`）：上架中的版本，宣告的內容模式不高於玩家目前**生效**的模式（玩家偏好與年齡上限中較保守者）；依玩家的
故事偏好排序（只影響順序、不增減集合）；分頁用 `limit + 1` 取回，多出的一筆只用來決定 `has_more`。每張卡片只含白名單欄位
（`CARD_FIELDS`）：沒有劇本內文、內容雜湊、審查結果、作者的使用者編號與任何內部整數編號。一頁的資料以固定次數的查詢取回
（版本與劇本一次、標籤一次），不隨筆數增加。

**我的存檔**（`my_sessions`）：只有自己的、不含已封存者，依最後活動由新到舊；每筆只含白名單欄位（`SAVE_FIELDS`）。各存檔已完成的
輪數一次查詢取回。標題取自存檔的劇本複本（JSON 欄位不能做篩選，但讀出後取值是允許的）。

**存檔摘要**（`session_summary`）：只回擁有者自己的存檔；非擁有者與不存在的得到相同的 `NotFound`（404）。

分頁參數的範圍在這裡驗證（Controller 只負責把查詢字串轉成整數）：`limit` 與 `offset` 不合法時拋 `ValidationError`，一次指出所有不合法的欄位。
"""
from __future__ import annotations

from typing import Any

from app.models.rpg_session import SESSION_ACTIVE, SESSION_ENDED, RpgSession
from app.models.script import OFFICIAL_AUTHOR_NAME, Script, ScriptVersion
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser, iso_utc
from app.services.errors import NotFound, ValidationError
from app.services.story_preference import normalize_audience

# 分頁：預設值與上限（上限由測試固定；Controller 以預設值讀取查詢參數）
CATALOG_DEFAULT_LIMIT = 12
CATALOG_MAX_LIMIT = 24
SESSIONS_DEFAULT_LIMIT = 20
SESSIONS_MAX_LIMIT = 50
OFFSET_MAX = 10_000                # 避免很深的跳頁；入口只是最小版，完整目錄（RC-8）再用游標

# 對外欄位的白名單（回應的鍵集合恰為這些；測試固定，新增欄位要先改規格）
CARD_FIELDS = (
    "id", "title", "summary", "world_name", "tags", "author_name", "author_code", "content_tier", "audience",
)
SAVE_FIELDS = (
    "id", "title", "content_tier", "status", "player_name", "completed_turns", "last_active_at",
)
SUMMARY_FIELDS = ("id", "title", "content_tier", "player_name", "ended")


class GameEntryService:
    def __init__(self, *, scripts: ScriptRepository, sessions: SessionRepository, policy: AccessPolicy) -> None:
        self._scripts = scripts
        self._sessions = sessions
        self._policy = policy

    def catalog(self, current: CurrentUser, *, limit: object = CATALOG_DEFAULT_LIMIT, offset: object = 0) -> dict[str, Any]:
        """玩家可以開存檔的上架劇本一頁：`{"scripts": [卡片…], "has_more": 布林, "content_mode": {"effective", "is_adult"}}`。

        `content_mode` 讓畫面說明「為什麼沒看到某些劇本」（特別是未滿成年門檻固定為安全模式）：只有生效的模式與是否已滿成年門檻，
        與 `/api/account/content-mode` 的生效模式同一個來源，**不含**生日、年齡或年齡區間。
        """
        size, skip = _page(limit, offset, maximum=CATALOG_MAX_LIMIT)
        access = self._policy.content_access(current)
        rows = self._scripts.list_published_cards(
            access.effective, limit=size + 1, offset=skip, preference=normalize_audience(current.story_preference),
        )
        page = rows[:size]
        tags = self._scripts.tags_of_versions([version.id for version, _ in page])
        return {
            "scripts": [_card(version, script, tags.get(version.id, [])) for version, script in page],
            "has_more": len(rows) > size,
            "content_mode": {"effective": access.effective, "is_adult": access.is_adult},
        }

    def my_sessions(self, current: CurrentUser, *, limit: object = SESSIONS_DEFAULT_LIMIT, offset: object = 0) -> dict[str, Any]:
        """呼叫者自己的存檔一頁（不含已封存者）：`{"sessions": [存檔…], "has_more": 布林}`。已完結的存檔仍在清單內。"""
        size, skip = _page(limit, offset, maximum=SESSIONS_MAX_LIMIT)
        rows = self._sessions.list_owned(current.id, include_archived=False, limit=size + 1, offset=skip)
        page = rows[:size]
        counts = self._sessions.complete_turn_counts(current.id, [save.id for save in page])
        return {
            "sessions": [_save_item(save, counts.get(save.id, 0)) for save in page],
            "has_more": len(rows) > size,
        }

    def session_summary(self, current: CurrentUser, session_public_id: object) -> dict[str, Any]:
        """存檔頁外殼要顯示的摘要（標題、主角姓名、內容模式、是否已完結）。非擁有者與不存在的存檔一律 `NotFound`。"""
        save = self._sessions.get_owned(current.id, session_public_id) if isinstance(session_public_id, str) else None
        if save is None:
            raise NotFound()
        return {
            "id": save.public_id, "title": _title_of(save), "content_tier": save.content_tier, "player_name": save.player_name,
            "ended": save.status == SESSION_ENDED,
        }


def _page(limit: object, offset: object, *, maximum: int) -> tuple[int, int]:
    """驗證分頁參數：`limit` 為 1 至 `maximum`、`offset` 為 0 至 `OFFSET_MAX` 的整數（布林不算）。所有不合法的欄位一次回報。"""
    errors: dict[str, str] = {}
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        errors["limit"] = f"每次筆數須為 1 至 {maximum}。"
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= OFFSET_MAX:
        errors["offset"] = f"位移須為 0 至 {OFFSET_MAX}。"
    if errors:
        raise ValidationError(fields=errors)
    return int(limit), int(offset)                              # 上面已確認兩者都是整數


def _card(version: ScriptVersion, script: Script, tags: list[str]) -> dict[str, Any]:
    """一張劇本卡片：只含公開的摘要欄位。內建劇本的筆名是「官方」（短代號為空）；版本的讀者頻道為空值時以 `all` 回傳。"""
    author_name = script.author_name
    if not author_name and script.seed_key is not None:
        author_name = OFFICIAL_AUTHOR_NAME
    return {
        "id": version.public_id, "title": version.title, "summary": version.summary, "world_name": version.world_name,
        "tags": list(tags), "author_name": author_name, "author_code": script.author_code,
        "content_tier": version.content_tier, "audience": normalize_audience(version.audience),
    }


def _title_of(save: RpgSession) -> Any:
    snapshot = save.script_snapshot_json if isinstance(save.script_snapshot_json, dict) else {}
    return snapshot.get("title")


def _save_item(save: RpgSession, completed_turns: int) -> dict[str, Any]:
    return {
        "id": save.public_id, "title": _title_of(save), "content_tier": save.content_tier,
        "status": SESSION_ENDED if save.status == SESSION_ENDED else SESSION_ACTIVE, "player_name": save.player_name,
        "completed_turns": completed_turns, "last_active_at": iso_utc(save.last_active_at),
    }
