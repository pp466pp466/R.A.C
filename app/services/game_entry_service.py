"""RPG 入口畫面的唯讀資料：可玩劇本清單、我的存檔清單與單一存檔的摘要（見 rpg-entry-api 規格、rpg-entry-ui 的 design D2、D3、D4、D7）。

**唯讀**：不呼叫模型、不碰錢包與額度、不寫入任何資料（連日誌也不寫：標題、筆名與主角姓名都是玩家文字衍生的內容）。

**不過年齡閘門**（`require_chat_access`）：`/game` 是登入後的預設頁面，尚未完成年齡確認的管理員也會落在這裡；清單不花成本，
生效模式缺少目前版本的同意時本來就是安全模式（`AccessPolicy.effective_mode`），所以放行是安全的。真正會花成本的「開始」由開存檔
既有的閘門擋下。呼叫端（Controller）只需要確認已登入；受限登入狀態在更早的登入檢查就被擋下。

**公開目錄**（`catalog`，rpg-script-catalog 規格）：上架中的**全部**版本，不依內容模式篩選（所有玩家都看得到所有上架的劇本；綁定安全模式只影響
生效模式）；可用關鍵字（只比對標題、簡介、世界名稱與標籤）、標籤與讀者頻道篩選，三者 AND 組合；依玩家的故事偏好排序（只影響順序、不增減集合）；
以**游標**分頁（`next_cursor`，排序鍵 `(偏好層, 發佈時間, 版本編號)`），取 `limit + 1` 筆，多出的一筆只用來決定 `has_more`，`offset` 不再被讀取。
每張卡片只含白名單欄位（`CARD_FIELDS`）：沒有劇本內文、內容雜湊、審查結果、作者的使用者編號與任何內部整數編號。一頁的資料以固定次數的查詢取回
（版本與劇本一次、標籤一次），不隨筆數增加。參數的解析與驗證在 `catalog_query.py`（純函式）。

**常用標籤**（`catalog_tags`）：上架版本中最常用的至多 30 個標籤與使用數（只計上架、不分大小寫合併）。

**劇本詳情**（`script_detail`）：只對上架中的版本回傳，**公開的白名單欄位**（`DETAIL_FIELDS`）：給「決定要不要玩」的資訊，
**不含**角色的隱藏特質與個性、里程碑、結局說明、規則、初始對話、審查結果、內容雜湊與任何內部整數編號；非上架的版本（含作者自己的草稿與下架的版本）
與不存在的識別碼得到相同的 `NotFound`。與目錄一樣不過年齡閘門、唯讀、不寫日誌（標題、筆名等都是玩家文字衍生的內容）。

**我的存檔**（`my_sessions`）：只有自己的、不含已封存者，依最後活動由新到舊；每筆只含白名單欄位（`SAVE_FIELDS`），其中
`content_tier` 是**現算的生效模式**（玩家目前的設定、存檔複本的劇本綁定與緊急煞車，不是開存檔當下的值）。各存檔已完成的
輪數一次查詢取回。標題取自存檔的劇本複本（JSON 欄位不能做篩選，但讀出後取值是允許的）。

**存檔摘要**（`session_summary`）：只回擁有者自己的存檔；非擁有者與不存在的得到相同的 `NotFound`（404）。

分頁參數的範圍在這裡驗證（Controller 只負責把查詢字串轉成整數）：不合法時拋 `ValidationError`，一次指出所有不合法的欄位
（目錄在 `catalog_query`、我的存檔用 `_page` 的 `limit` 與 `offset`）。
"""
from __future__ import annotations

from typing import Any

from app.models.rpg_session import SESSION_ACTIVE, SESSION_ENDED, RpgSession
from app.models.script import ENDING_CUSTOM, OFFICIAL_AUTHOR_NAME, Script, ScriptVersion
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.services.access_policy import AccessPolicy
from app.services.catalog_query import DEFAULT_LIMIT as CATALOG_DEFAULT_LIMIT
from app.services.catalog_query import MAX_LIMIT as CATALOG_MAX_LIMIT
from app.services.catalog_query import encode_cursor, parse_catalog_query
from app.services.content_tier import parse_tier
from app.services.current_user import CurrentUser, iso_utc
from app.services.errors import NotFound, ValidationError
from app.services.story_preference import normalize_audience

# 分頁：預設值與上限（上限由測試固定；Controller 以預設值讀取查詢參數）。目錄的在 `catalog_query`（游標分頁），這裡再匯出供 Controller 與測試使用。
SESSIONS_DEFAULT_LIMIT = 20
SESSIONS_MAX_LIMIT = 50
OFFSET_MAX = 10_000                # 「我的存檔」仍用位移分頁（只有自己的存檔，數量小）；公開目錄已改游標
CATALOG_TAGS_LIMIT = 30

# 對外欄位的白名單（回應的鍵集合恰為這些；測試固定，新增欄位要先改規格）
CARD_FIELDS = (
    "id", "title", "summary", "world_name", "tags", "author_name", "author_code", "content_tier", "audience",
)
DETAIL_FIELDS = (
    "id", "title", "summary", "world_name", "tags", "author_name", "author_code", "content_tier", "audience", "version_no",
    "published_at", "background", "characters", "locations", "has_custom_ending", "content_mode",
)
DETAIL_CHARACTER_FIELDS = ("name", "occupation")           # 角色只公開姓名與職業；個性、外貌、隱藏特質與年齡都不公開
SAVE_FIELDS = (
    "id", "title", "content_tier", "status", "player_name", "completed_turns", "last_active_at",
)
SUMMARY_FIELDS = ("id", "title", "content_tier", "player_name", "ended")


class GameEntryService:
    def __init__(self, *, scripts: ScriptRepository, sessions: SessionRepository, policy: AccessPolicy) -> None:
        self._scripts = scripts
        self._sessions = sessions
        self._policy = policy

    def catalog(
        self, current: CurrentUser, *, q: object = None, tag: object = None, audience: object = None, cursor: object = None,
        limit: object = CATALOG_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """玩家可以開存檔的上架劇本一頁：`{"scripts": [卡片…], "has_more": 布林, "next_cursor": 字串或 None, "content_mode": {"effective", "forced"}}`。

        `q`、`tag`、`audience` 篩選（見 `catalog_query`），`cursor` 是上一頁回應的 `next_cursor`；沒有下一頁時 `next_cursor` 為 `None`。
        參數不合法拋 `ValidationError`（一次指出所有不合法的欄位，含損毀的游標）。

        `content_mode` 讓畫面判斷「點選未綁定安全模式的劇本時是否要詢問修改安全模式」：玩家目前的生效模式（不含劇本）與緊急煞車是否啟用，
        與 `/api/account/content-mode` 同一個來源，**不含**生日、年齡、年齡區間，也不含 `is_adult`。
        """
        query = parse_catalog_query(q=q, tag=tag, audience=audience, limit=limit, cursor=cursor)
        mode = self._policy.mode_state(current)
        after = None if query.cursor is None else (query.cursor.rank, query.cursor.published_at, query.cursor.version_id)
        rows = self._scripts.list_catalog(
            terms=query.terms, tag=query.tag, audience=query.audience, after=after,
            preference=normalize_audience(current.story_preference), limit=query.limit + 1,
        )
        page = rows[:query.limit]
        has_more = len(rows) > query.limit
        next_cursor = None
        if has_more:
            last, _script, rank = page[-1]
            next_cursor = encode_cursor(rank, last.published_at, last.id)
        tags = self._scripts.tags_of_versions([version.id for version, _script, _rank in page])
        return {
            "scripts": [_card(version, script, tags.get(version.id, [])) for version, script, _rank in page],
            "has_more": has_more,
            "next_cursor": next_cursor,
            "content_mode": {"effective": mode.effective, "forced": mode.forced},
        }

    def catalog_tags(self) -> dict[str, Any]:
        """常用標籤：`{"tags": [{"tag": 小寫標籤, "count": 使用該標籤的上架版本數}…]}`，至多 `CATALOG_TAGS_LIMIT` 個，使用數由多到少。不依使用者。"""
        return {"tags": [{"tag": name, "count": count} for name, count in self._scripts.popular_tags(limit=CATALOG_TAGS_LIMIT)]}

    def script_detail(self, current: CurrentUser, version_public_id: object) -> dict[str, Any]:
        """一個上架版本的公開詳情（`DETAIL_FIELDS`）。非上架的版本（含擁有者自己的草稿與下架的版本）、不存在與格式不對的識別碼一律 `NotFound`。"""
        found = self._scripts.get_published_card(version_public_id) if isinstance(version_public_id, str) else None
        if found is None:
            raise NotFound()
        version, script = found
        mode = self._policy.mode_state(current)
        detail = _card(version, script, self._scripts.tags_of(version))
        detail.update({
            "version_no": version.version_no,
            "published_at": None if version.published_at is None else iso_utc(version.published_at),
            "background": version.background,
            "characters": [_detail_character(item) for item in (version.characters_json or []) if isinstance(item, dict)],
            "locations": [item for item in (version.locations_json or []) if isinstance(item, str)],
            "has_custom_ending": version.ending_mode == ENDING_CUSTOM,
            "content_mode": {"effective": mode.effective, "forced": mode.forced},
        })
        return detail

    def my_sessions(self, current: CurrentUser, *, limit: object = SESSIONS_DEFAULT_LIMIT, offset: object = 0) -> dict[str, Any]:
        """呼叫者自己的存檔一頁（不含已封存者）：`{"sessions": [存檔…], "has_more": 布林}`。已完結的存檔仍在清單內。"""
        size, skip = _page(limit, offset, maximum=SESSIONS_MAX_LIMIT)
        rows = self._sessions.list_owned(current.id, include_archived=False, limit=size + 1, offset=skip)
        page = rows[:size]
        counts = self._sessions.complete_turn_counts(current.id, [save.id for save in page])
        return {
            "sessions": [_save_item(save, counts.get(save.id, 0), self._policy.save_mode(current, save.script_snapshot_json)) for save in page],
            "has_more": len(rows) > size,
        }

    def session_summary(self, current: CurrentUser, session_public_id: object) -> dict[str, Any]:
        """存檔頁外殼要顯示的摘要（標題、主角姓名、內容模式、是否已完結）。非擁有者與不存在的存檔一律 `NotFound`。"""
        save = self._sessions.get_owned(current.id, session_public_id) if isinstance(session_public_id, str) else None
        if save is None:
            raise NotFound()
        return {
            "id": save.public_id, "title": _title_of(save), "content_tier": self._policy.save_mode(current, save.script_snapshot_json),
            "player_name": save.player_name, "ended": save.status == SESSION_ENDED,
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
        "content_tier": parse_tier(version.content_tier), "audience": normalize_audience(version.audience),
    }


def _detail_character(item: dict[str, Any]) -> dict[str, Any]:
    """詳情裡的一個角色：只有姓名與職業（白名單），其餘欄位不論存了什麼都不帶出去。"""
    return {field: item.get(field) for field in DETAIL_CHARACTER_FIELDS}


def _title_of(save: RpgSession) -> Any:
    snapshot = save.script_snapshot_json if isinstance(save.script_snapshot_json, dict) else {}
    return snapshot.get("title")


def _save_item(save: RpgSession, completed_turns: int, mode: str) -> dict[str, Any]:
    return {
        "id": save.public_id, "title": _title_of(save), "content_tier": mode,
        "status": SESSION_ENDED if save.status == SESSION_ENDED else SESSION_ACTIVE, "player_name": save.player_name,
        "completed_turns": completed_turns, "last_active_at": iso_utc(save.last_active_at),
    }
