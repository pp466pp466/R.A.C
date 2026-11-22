"""劇本與劇本版本的資料存取（見 script-store 規格、AGENTS.md 第 4、7、10 節、rpg-data-and-script 的 design D2、D11）。

只用 ORM 表達式，不提交交易（請求結束時統一提交）。

**讀取範圍**
- 擁有者範圍的方法（名稱含 `owned`，或第一個參數是 `user_id`）一律以 `Script.creator_id == user_id` 為查詢條件；
  別人的資源與不存在的資源得到相同的結果（`None`），Service 據此回 404。
- 公開讀取（`get_published`、`list_published`…）只回傳 `published` 的版本；沒有「只憑識別碼取得任意版本」的方法。
- 管理員讀取他人的待審內容走後續審核流程的專用路徑並寫稽核，不在這裡。

**狀態與不可變**
- 版本的狀態與兩個輔助欄位（`live_script_id`、`open_script_id`）**只在這個檔案裡被寫入**（`tests/architecture` 有測試擋住別處）：
  建立版本與下面的轉移方法。轉移集合固定（`_ALLOWED_TRANSITIONS`，(來源, 目標) 配對的白名單，由呼叫端明確指定來源），
  每次轉移是單一條件式 UPDATE（`WHERE status IN (指定的來源)`），並行的兩個請求只有一個成功。輔助欄位在狀態為 `published` 時等於
  `script_id`、為 `draft` 或 `pending_review` 時等於 `script_id`（另一個欄位），其餘（含 `removed`）為空；兩者各有部分唯一索引，
  所以「每個劇本至多一個上架版本與一個進行中的版本」由資料庫保證，不是靠程式檢查。
- 送審時間、下架者、決定類別、審核備註與決定時間（rpg-script-publishing）是版本的中繼資料，同樣只由這裡的轉移方法在同一個 UPDATE
  內寫入（`tests/architecture` 擋住別處）；備註只供管理員，作者路徑的回應由 Service 以固定的鍵集合組裝，不含備註。
- 只有 `draft` 的內容可以修改或刪除（條件式 UPDATE／DELETE）；其餘狀態的內容與標籤不可變。
- 核可（`approve_version`）把同一劇本原本上架的版本與下架的版本改為 `superseded` 並上架新版本，在同一個 SAVEPOINT 內完成，
  失敗時兩者都保持原狀態；第一次核可時鎖定筆名。
- 下架與恢復：誰下架誰恢復（`remove_own`／`restore_own`、`remove_by_admin`／`restore_by_admin`）；恢復是條件式 UPDATE
  （`status = 'removed'` 且 `removed_by` 等於恢復者），在 SAVEPOINT 內執行，同劇本已有另一個上架版本時由部分唯一索引擋下。
  `get_startable` 排除下架的版本（任何人，包含作者自己）。

**標籤**以顯示形式儲存（保留原本的大小寫，這樣版本內容可以由欄位完整還原、雜湊不變）；驗證器已經以 NFC 與不分大小寫
去除重複，所以同一版本內不會有只差大小寫的兩個標籤。依標籤查詢時以小寫比對（`func.lower`，ORM 表達式，雙軌一致）。

**刪除版本與存檔的參照**：`sessions.source_version_id` 是無動作的外鍵（SQL Server 不允許多重串聯路徑），所以刪除版本之前先把
參照它的存檔清為空值；存檔本身不受影響（它有自己的劇本複本）。

**檢舉的自動結案**（rpg-script-reports 的 design D2）：版本離開上架時，未結案的檢舉在**同一個交易**內由
`ScriptReportRepository.close_open` 結案——下架（作者或管理員，共用 `_remove`）結為 `removed`；被新的上架版本取代
（核可 `approve_version`、內建劇本重新上架 `publish_builtin`，兩者都在轉移的 SAVEPOINT 內）結為 `superseded`。結案放在轉移內部而不是
Service：離開上架的入口有四個，放在 Service 每個入口都得記得呼叫，漏一個就留下孤兒的未結案檢舉。轉移失敗時結案不會發生，
SAVEPOINT 內失敗則結案一併回復。恢復上架（`_restore`）**不**重開檢舉：結案是歷史事實。這個檔案只呼叫 `close_open`，
不自己寫結案欄位（`tests/architecture/test_script_report_writes.py`）。

**匿名保留**（`retain_published_anonymously`，rpg-script-reports 的 design D6）：作者選擇保留時，抹除流程在刪除使用者之前呼叫它，
只留下目前上架的版本並解除作者關聯；其餘一律刪除。**「沒有作者」不等於「內建劇本」**：判斷內建一律看 `seed_key`。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import Unicode, and_, case, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import util as orm_util

from app.models.base import utcnow
from app.models.rpg_session import RpgSession
from app.models.script import (
    DECISION_CATEGORIES, ENDING_NONE, MAX_DECISION_NOTE_CHARS, OFFICIAL_AUTHOR_NAME, OPEN_VERSION_STATUSES, REMOVER_ADMIN,
    REMOVER_AUTHOR, RETIRED_AUTHOR_NAME, REVIEW_CATEGORIES, REVIEW_VERDICTS, VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED,
    VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED, Script, ScriptVersion, ScriptVersionTag,
)
from app.models.script_report import CLOSED_REMOVED, CLOSED_SUPERSEDED
from app.models.user import AUDIENCE_ALL, AUDIENCE_FEMALE, AUDIENCE_MALE
from app.observability import script_events
from app.repositories.base import BaseRepository
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.errors import (
    AuthorNameLockedError, BuiltinNotRemovableError, DraftChangedError, DuplicateSeedKeyError, InvalidVersionTransitionError,
    LiveVersionExistsError, OpenVersionExistsError, RemovalLockedError, RemovedByAuthorError, VersionNotDraftError,
)

# 允許的 (來源狀態, 目標狀態) 配對（固定的轉移集合，見 script-store 規格「版本的狀態與轉移」與 script-takedown 規格）。
# 呼叫端明確傳入來源（核可只來自 `pending_review`、恢復只來自 `removed`），再對照這張白名單：白名單之外的組合在程式層就拒絕，
# 而不是只靠 UPDATE 的 WHERE。目標相同、來源不同的轉移（核可與恢復都是 → `published`）因此不會互相借用來源。
_ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    (VERSION_DRAFT, VERSION_PENDING),             # 送審
    (VERSION_PENDING, VERSION_DRAFT),             # 撤回
    (VERSION_PENDING, VERSION_PUBLISHED),         # 核可
    (VERSION_PENDING, VERSION_REJECTED),          # 退回
    (VERSION_PUBLISHED, VERSION_SUPERSEDED),      # 被新的上架版本取代
    (VERSION_PUBLISHED, VERSION_REMOVED),         # 下架（作者或管理員）
    (VERSION_REMOVED, VERSION_PUBLISHED),         # 恢復上架（只有下架者）
    (VERSION_REMOVED, VERSION_SUPERSEDED),        # 下架之後出了新的上架版本：舊的下架版不可再恢復
})


def _check_pairs(sources: Sequence[str], to_status: str) -> None:
    """來源與目標的每個配對都必須在白名單內；程式錯誤時在送出 UPDATE 之前拋出，版本不受影響。"""
    if not sources or any((source, to_status) not in _ALLOWED_TRANSITIONS for source in sources):
        raise InvalidVersionTransitionError


def _checked_decision(category: str, note: str | None) -> tuple[str, str | None]:
    """退回與管理員下架的決定：類別必須是固定列舉、備註至多 500 字（NFC 與控制字元由 Service 處理）；空備註存為空值。
    列舉之外是呼叫端的程式錯誤，拋出 `ValueError`。"""
    if category not in DECISION_CATEGORIES:
        raise ValueError("決定的類別必須是固定列舉。")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_DECISION_NOTE_CHARS):
        raise ValueError(f"審核備註至多 {MAX_DECISION_NOTE_CHARS} 字。")
    return category, (note or None)


def _helper_columns(script_id: int, status: str) -> dict[str, int | None]:
    """輔助欄位的值：唯一由狀態決定，所以寫入狀態的每個地方都經過這裡。"""
    return {
        "live_script_id": script_id if status == VERSION_PUBLISHED else None,
        "open_script_id": script_id if status in OPEN_VERSION_STATUSES else None,
    }


def _content_columns(content: Mapping[str, Any], content_hash: str, total_chars: int) -> dict[str, Any]:
    """標準化內容（`script_spec.validate_script` 的輸出）對應到版本的欄位；標籤另存，不在這裡。"""
    calendar = content.get("calendar") or {}
    return {
        "spec_version": content["spec_version"],
        "content_hash": content_hash,
        "total_chars": total_chars,
        "title": content["title"],
        "summary": content["summary"],
        "world_name": content["world_name"],
        "content_tier": content["content_tier"],
        "audience": _audience_of(content),
        "background": content["background"],
        "opening": content.get("opening"),
        "rules": content.get("rules"),
        "ending_mode": content.get("ending_mode") or ENDING_NONE,
        "ending_text": content.get("ending_text"),
        "ending_age": content.get("ending_age"),
        "calendar_name": calendar.get("name"),
        "calendar_start_year": calendar.get("start_year"),
        "characters_json": list(content.get("characters") or []),
        "locations_json": list(content.get("locations") or []),
        "milestones_json": list(content.get("milestones") or []),
    }


def _audience_of(content: Mapping[str, Any]) -> str:
    """版本要寫入的讀者頻道：標準化內容只在男頻或女頻時帶有 `audience` 鍵，沒有就是全讀者（新版本寫明確的 `all`，不寫空值）。"""
    value = content.get("audience")
    return value if value in (AUDIENCE_MALE, AUDIENCE_FEMALE) else AUDIENCE_ALL


def _audience_rank(preference: str | None) -> Any | None:
    """依故事偏好分層的 `case` 表達式，`None`＝不分層（偏好為空、`all` 或無法辨識）。與 `story_preference.rank` 同一規則：
    同頻 0、全讀者（含空值）1、另一頻 2。

    `case` 是 ORM 表達式，空值被明確映射（不依賴 NULL 排序方向的方言差異）。不分層時回傳 `None` 而不是常數 0：
    SQL Server 的 `ORDER BY` 不接受常數，所以呼叫端在不分層時乾脆不加這個排序鍵與鍵集條件（常數不影響順序）。
    Repository 不得匯入 Service，所以這裡只用模型的常數；兩處規則的一致由測試守住。
    """
    if preference not in (AUDIENCE_MALE, AUDIENCE_FEMALE):
        return None
    return case(
        (ScriptVersion.audience == preference, 0),
        (or_(ScriptVersion.audience.is_(None), ScriptVersion.audience == AUDIENCE_ALL), 1),
        else_=2,
    )


def _audience_order(preference: str | None) -> list[Any]:
    """依故事偏好排序的鍵（空串列＝不加排序鍵），`list_published` 與 `list_published_by_tag` 使用。"""
    rank = _audience_rank(preference)
    return [] if rank is None else [rank]


_LIKE_ESCAPE = "\\"


def _keyword_filter(term: str) -> Any:
    """一個關鍵字詞的條件：標題、簡介、世界名稱或任一標籤「包含」它（不分大小寫）。**只比對這四個一般欄位**，不碰背景、角色、
    里程碑等長文字或 JSON 欄位（AGENTS.md 第 4 節：不得依 JSON 內容篩選）。

    `term` 已由 Service 以 `casefold` 正規化；欄位以 `lower()` 比對（SQLite 的 `lower()` 只處理 ASCII、SQL Server 的預設定序不分大小寫，
    中文與 ASCII 在兩者的結果一致）。`%`、`_`、`[` 與跳脫字元由 `_escape_like` 變成一般字元。
    """
    pattern = _escape_like(term)
    return or_(
        func.lower(ScriptVersion.title, type_=Unicode).contains(pattern, escape=_LIKE_ESCAPE),
        func.lower(ScriptVersion.summary, type_=Unicode).contains(pattern, escape=_LIKE_ESCAPE),
        func.lower(ScriptVersion.world_name, type_=Unicode).contains(pattern, escape=_LIKE_ESCAPE),
        exists().where(
            ScriptVersionTag.version_id == ScriptVersion.id,
            func.lower(ScriptVersionTag.tag, type_=Unicode).contains(pattern, escape=_LIKE_ESCAPE),
        ),
    )


def _escape_like(term: str) -> str:
    """把 LIKE 的特殊字元變成字面字元：跳脫字元本身、`%`、`_`，以及 SQL Server 的 `[`（字元集合，SQLite 沒有這個語法，
    跳脫它在兩邊都合法）。與 SQLAlchemy 的 `autoescape` 相同做法，只是多處理 `[`，且跳脫字元固定，行為不依方言而不同。"""
    return "".join(f"{_LIKE_ESCAPE}{char}" if char in (_LIKE_ESCAPE, "%", "_", "[") else char for char in term)


def _published_window(statement: Any, *, limit: int, offset: int, preference: str | None) -> Any:
    """`list_published` 的條件與排序（公開目錄的 `list_catalog` 另有自己的版本，兩者的上架集合與偏好分層規則相同，由測試固定一致）：
    只含 `published`，**不依內容模式篩選**（所有玩家都看得到所有上架的劇本；綁定安全模式只影響生效模式）；
    先依偏好分層、再由新到舊（發佈時間，同時間依編號），最後才分頁。"""
    return (
        statement
        .where(ScriptVersion.status == VERSION_PUBLISHED)
        .order_by(*_audience_order(preference), ScriptVersion.published_at.desc(), ScriptVersion.id.desc())
        .limit(limit)
        .offset(offset)
    )


_ID_CHUNK = 500      # SQL Server 單一語句最多約 2100 個參數；以編號集合為單位的更新與刪除分批執行


def _chunks(ids: Sequence[int]) -> Iterator[list[int]]:
    ordered = list(dict.fromkeys(ids))
    for start in range(0, len(ordered), _ID_CHUNK):
        yield ordered[start:start + _ID_CHUNK]


@dataclass(frozen=True)
class RetainResult:
    """匿名保留的結果（只有整數）：`kept` 保留的版本數（每份保留的劇本一個，即上架中的那個）、`deleted` 刪除的版本數
    （含整份被刪的劇本的全部版本）。給日誌事件用；沒有劇本時兩者皆為 0。"""

    kept: int
    deleted: int


class ScriptRepository(BaseRepository[Script]):
    model = Script

    # ---- 內部工具 ---------------------------------------------------------------

    @property
    def _reports(self) -> ScriptReportRepository:
        """結案與刪除檢舉用（只呼叫它的 `close_open`、`delete_for_versions`、`delete_for_author`；結案欄位只在它那個檔案寫入）。"""
        return ScriptReportRepository(self._session)

    def delete_reports_on_authored_versions(self, user_id: int) -> int:
        """刪除這位作者名下所有版本上的檢舉，回傳筆數。抹除流程在刪除使用者之前、**每一條路**都呼叫：`script_reports.version_id`
        是無動作的外鍵，版本隨使用者串聯消失時還有檢舉會讓刪除失敗。匿名保留先做完之後，保留的劇本已解除作者關聯，這裡不會誤刪它們的檢舉。"""
        self._session.flush()
        return self._reports.delete_for_author(user_id)

    def _close_reports(self, version_ids: Sequence[int], result: str, now: datetime) -> int:
        """把這些版本未結案的檢舉全部結案，回傳筆數（呼叫端在轉移**確定成功之後**才記錄事件，SAVEPOINT 回復時不會留下不實的紀錄）。"""
        return sum(self._reports.close_open(version_id, result, now) for version_id in version_ids)

    def _expire(self, model: type, primary_key: int) -> None:
        """條件式 UPDATE 繞過了 ORM：讓已載入的物件在下次存取時重新讀取。"""
        loaded = self._session.identity_map.get(orm_util.identity_key(model, primary_key))
        if loaded is not None:
            self._session.expire(loaded)

    def _next_version_no(self, script_id: int) -> int:
        current = self._session.scalar(select(func.max(ScriptVersion.version_no)).where(ScriptVersion.script_id == script_id))
        return (current or 0) + 1

    def _add_tags(self, version_id: int, tags: Sequence[str]) -> None:
        if tags:
            self._session.add_all([ScriptVersionTag(version_id=version_id, tag=tag) for tag in tags])
            self._session.flush()

    def _insert_version(
        self, script_id: int, version_no: int, status: str, content: Mapping[str, Any], content_hash: str, total_chars: int,
        *, published_at: datetime | None = None,
    ) -> ScriptVersion:
        version = ScriptVersion(
            script_id=script_id, version_no=version_no, status=status, published_at=published_at,
            **_helper_columns(script_id, status), **_content_columns(content, content_hash, total_chars),
        )
        self._session.add(version)
        self._session.flush()
        self._add_tags(version.id, content.get("tags") or [])
        return version

    def _transition(
        self, version: ScriptVersion, to_status: str, *, sources: Sequence[str], where: Sequence[Any] = (), **extra: Any
    ) -> None:
        """唯一寫入版本狀態（與輔助欄位）的地方：條件式 UPDATE，來源狀態不符時不改任何東西。

        `sources` 由呼叫端明確指定，並須全部落在 `_ALLOWED_TRANSITIONS` 內；`where` 是額外的條件（恢復比對下架者）；
        `extra` 是同一個 UPDATE 一併寫入的中繼資料（送審時間、下架者、決定…）。"""
        _check_pairs(sources, to_status)
        result = self._session.execute(
            update(ScriptVersion)
            .where(ScriptVersion.id == version.id, ScriptVersion.status.in_(tuple(sources)), *where)
            .values(status=to_status, **_helper_columns(version.script_id, to_status), **extra)
            .execution_options(synchronize_session=False)
        )
        self._expire(ScriptVersion, version.id)
        if result.rowcount != 1:
            raise InvalidVersionTransitionError

    def _owned_version(self, user_id: int, version_public_id: str) -> ScriptVersion | None:
        return self._session.scalar(
            select(ScriptVersion)
            .join(Script, Script.id == ScriptVersion.script_id)
            .where(ScriptVersion.public_id == version_public_id, Script.creator_id == user_id)
        )

    # ---- 擁有者：建立 -------------------------------------------------------------

    def create_script(
        self, user_id: int, *, author_name: str | None, author_code: str | None, content: Mapping[str, Any], content_hash: str,
        total_chars: int,
    ) -> ScriptVersion:
        """建立一個劇本與它的第一個草稿（版本 1）。筆名與短代號是目前暱稱的快照，發佈前可更新。"""
        script = Script(creator_id=user_id, author_name=author_name, author_code=author_code)
        self._session.add(script)
        self._session.flush()
        return self._insert_version(script.id, 1, VERSION_DRAFT, content, content_hash, total_chars)

    def create_draft_version(
        self, user_id: int, script_public_id: str, *, content: Mapping[str, Any], content_hash: str, total_chars: int
    ) -> ScriptVersion | None:
        """為自己的劇本建立下一個草稿版本。已有進行中的版本時拋出 `OpenVersionExistsError`（含並行的情況）；
        不是自己的劇本回傳 `None`。"""
        script = self.get_owned_script(user_id, script_public_id)
        if script is None:
            return None
        if self._session.scalar(select(ScriptVersion.id).where(ScriptVersion.open_script_id == script.id)) is not None:
            raise OpenVersionExistsError
        try:
            with self._session.begin_nested():            # 並行的另一個請求搶先時，只回復這一筆
                return self._insert_version(
                    script.id, self._next_version_no(script.id), VERSION_DRAFT, content, content_hash, total_chars
                )
        except IntegrityError as exc:
            raise OpenVersionExistsError from exc

    # ---- 擁有者：讀取 -------------------------------------------------------------

    def get_owned_script(self, user_id: int, script_public_id: str) -> Script | None:
        return self._session.scalar(select(Script).where(Script.public_id == script_public_id, Script.creator_id == user_id))

    def get_owned_version(self, user_id: int, version_public_id: str) -> ScriptVersion | None:
        """自己的任何狀態的版本；別人的與不存在的一樣回傳 `None`。"""
        return self._owned_version(user_id, version_public_id)

    def list_owned(self, user_id: int, *, limit: int = 50, offset: int = 0) -> list[tuple[Script, ScriptVersion]]:
        """自己的全部劇本與版本（由新到舊）。"""
        statement = (
            select(Script, ScriptVersion)
            .join(ScriptVersion, ScriptVersion.script_id == Script.id)
            .where(Script.creator_id == user_id)
            .order_by(Script.id.desc(), ScriptVersion.version_no.desc())
            .limit(limit)
            .offset(offset)
        )
        return [(script, version) for script, version in self._session.execute(statement).all()]

    def count_owned(self, user_id: int) -> int:
        """作者擁有的劇本數（含只有草稿的劇本）；劇本數量上限用它。"""
        return self._session.execute(select(func.count(Script.id)).where(Script.creator_id == user_id)).scalar_one()

    def count_live_owned(self, user_id: int) -> int:
        """作者目前上架中的版本數（`live_script_id` 非空；下架的不算）。帳號頁據此決定要不要顯示「匿名保留」的選項。"""
        return self._session.execute(
            select(func.count(ScriptVersion.id))
            .join(Script, Script.id == ScriptVersion.script_id)
            .where(Script.creator_id == user_id, ScriptVersion.live_script_id.is_not(None))
        ).scalar_one()

    def get_open_version(self, user_id: int, script_public_id: str) -> ScriptVersion | None:
        """自己劇本目前進行中的版本（草稿或待審），沒有（或不是自己的劇本）回傳 `None`。"""
        return self._session.scalar(
            select(ScriptVersion)
            .join(Script, Script.id == ScriptVersion.script_id)
            .where(Script.public_id == script_public_id, Script.creator_id == user_id, ScriptVersion.open_script_id.is_not(None))
        )

    def latest_closed_version(self, user_id: int, script_public_id: str) -> ScriptVersion | None:
        """自己劇本最新的非進行中版本（已發佈、被退回、被取代或已下架）：由它的內容建立下一個草稿。"""
        return self._session.scalar(
            select(ScriptVersion)
            .join(Script, Script.id == ScriptVersion.script_id)
            .where(
                Script.public_id == script_public_id, Script.creator_id == user_id,
                ScriptVersion.status.in_((VERSION_PUBLISHED, VERSION_REJECTED, VERSION_SUPERSEDED, VERSION_REMOVED)),
            )
            .order_by(ScriptVersion.version_no.desc())
            .limit(1)
        )

    def get_script_of(self, version: ScriptVersion) -> Script:
        script = self._session.get(Script, version.script_id)
        assert script is not None, "版本的外鍵保證劇本存在"
        return script

    # ---- 擁有者：修改與刪除草稿 ---------------------------------------------------------

    def update_draft(
        self, user_id: int, version_public_id: str, *, content: Mapping[str, Any], content_hash: str, total_chars: int,
        expected_hash: str | None = None,
    ) -> ScriptVersion | None:
        """覆寫自己草稿的內容與標籤。不是自己的回傳 `None`；不是草稿拋出 `VersionNotDraftError`，內容保持原樣。

        `expected_hash`（編輯開始時的內容雜湊）有給時，條件式 UPDATE 同時比對它：草稿在這之間被別處更新（例如另一個分頁）就不覆寫，
        拋出 `DraftChangedError`。沒命中時再讀一次區分「不是草稿」與「草稿已被更新」。"""
        version = self._owned_version(user_id, version_public_id)
        if version is None:
            return None
        conditions = [ScriptVersion.id == version.id, ScriptVersion.status == VERSION_DRAFT]
        if expected_hash is not None:
            conditions.append(ScriptVersion.content_hash == expected_hash)
        with self._session.begin_nested():
            result = self._session.execute(
                update(ScriptVersion)
                .where(*conditions)
                .values(**_content_columns(content, content_hash, total_chars))
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                current = self._session.execute(
                    select(ScriptVersion.status).where(ScriptVersion.id == version.id)
                ).scalar_one_or_none()
                if current != VERSION_DRAFT:
                    raise VersionNotDraftError
                raise DraftChangedError
            self._session.execute(delete(ScriptVersionTag).where(ScriptVersionTag.version_id == version.id).execution_options(synchronize_session=False))
            self._add_tags(version.id, content.get("tags") or [])
        self._expire(ScriptVersion, version.id)
        return version

    def update_author_name(self, user_id: int, script_public_id: str, *, name: str, code: str) -> Script | None:
        """重新取用目前的暱稱作為筆名。已在第一次發佈時鎖定則拋出 `AuthorNameLockedError`；不是自己的回傳 `None`。"""
        script = self.get_owned_script(user_id, script_public_id)
        if script is None:
            return None
        result = self._session.execute(
            update(Script)
            .where(Script.id == script.id, Script.author_locked_at.is_(None))
            .values(author_name=name, author_code=code)
            .execution_options(synchronize_session=False)
        )
        self._expire(Script, script.id)
        if result.rowcount != 1:
            raise AuthorNameLockedError
        return script

    def delete_draft(self, user_id: int, version_public_id: str) -> bool | None:
        """刪除自己的草稿版本；那是劇本唯一的版本時連劇本一起刪除。回傳 `True`；不是自己的回傳 `None`；
        不是草稿拋出 `VersionNotDraftError`（下架與歷史保留由後續的審核與發佈流程處理）。

        先把參照這個版本的存檔清為空值（無動作的外鍵，見檔案開頭），存檔本身不受影響。
        """
        version = self._owned_version(user_id, version_public_id)
        if version is None:
            return None
        if version.status != VERSION_DRAFT:
            raise VersionNotDraftError
        script_id, version_id = version.script_id, version.id
        with self._session.begin_nested():
            self._session.execute(
                update(RpgSession).where(RpgSession.source_version_id == version_id).values(source_version_id=None)
                .execution_options(synchronize_session=False)
            )
            result = self._session.execute(
                delete(ScriptVersion).where(ScriptVersion.id == version_id, ScriptVersion.status == VERSION_DRAFT)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise VersionNotDraftError                  # 並行的另一個請求已改變狀態
            remaining = self._session.scalar(select(func.count()).select_from(ScriptVersion).where(ScriptVersion.script_id == script_id))
            if not remaining:
                self._session.execute(delete(Script).where(Script.id == script_id).execution_options(synchronize_session=False))
        self._session.expunge(version)
        return True

    # ---- 狀態轉移（唯一寫入版本狀態的方法）-------------------------------------------------

    def submit_for_review(self, user_id: int, version_public_id: str, *, now: datetime | None = None) -> ScriptVersion | None:
        """擁有者送審：`draft` → `pending_review`（內容從此凍結），同時記錄送審時間（審核佇列由舊到新排序用；
        沒有傳入時間時取目前的 UTC 時間，正式路徑由 Service 傳入自己的時鐘）。不是自己的回傳 `None`。"""
        version = self._owned_version(user_id, version_public_id)
        if version is None:
            return None
        self._transition(version, VERSION_PENDING, sources=(VERSION_DRAFT,), submitted_at=now if now is not None else utcnow())
        return version

    def withdraw(self, user_id: int, version_public_id: str) -> ScriptVersion | None:
        """擁有者撤回：`pending_review` → `draft`（要修改須先撤回），清除送審時間。不是自己的回傳 `None`。"""
        version = self._owned_version(user_id, version_public_id)
        if version is None:
            return None
        self._transition(version, VERSION_DRAFT, sources=(VERSION_PENDING,), submitted_at=None)
        return version

    def approve_version(self, version_public_id: str, *, now: datetime) -> ScriptVersion | None:
        """核可（審核流程專用，不是玩家路徑）：`pending_review` → `published`；同一劇本原本上架的版本（與下架的版本）在同一個
        SAVEPOINT 內改為 `superseded`（下架之後出了新的上架版本，舊的下架版就不可再恢復），它們未結案的檢舉同時結為
        `superseded`（下架的版本在下架時就已結案，所以實際上只有原本上架的那個會有未結案的檢舉）；第一次核可時鎖定筆名。
        失敗時三者（轉移、結案、筆名）都保持原狀。找不到版本回傳 `None`。"""
        version = self._session.scalar(select(ScriptVersion).where(ScriptVersion.public_id == version_public_id))
        if version is None:
            return None
        script_id = version.script_id
        replaced = (VERSION_PUBLISHED, VERSION_REMOVED)
        _check_pairs(replaced, VERSION_SUPERSEDED)
        # 被取代的版本：它們未結案的檢舉隨之結為「被取代」。**在 SAVEPOINT 之前讀**：SAVEPOINT 之內的第一個語句必須是寫入——
        # 先讀會在（本機 SQLite 檔案資料庫）取得共用鎖，之後升級成寫入鎖時，並行的另一個寫入者會讓它直接 `database is locked`
        # （不會等待 busy_timeout；`tests/test_admin_service_scripts.py` 的並行核可與退回測試守著）。讀到之後才被改變的版本都已結案或不會變成上架。
        superseded_ids = list(self._session.scalars(
            select(ScriptVersion.id).where(ScriptVersion.script_id == script_id, ScriptVersion.status.in_(replaced))
        ))
        with self._session.begin_nested():
            self._session.execute(                      # 先讓出上架的位置（部分唯一索引：同一劇本至多一個上架版本）
                update(ScriptVersion)
                .where(ScriptVersion.script_id == script_id, ScriptVersion.status.in_(replaced))
                .values(status=VERSION_SUPERSEDED, **_helper_columns(script_id, VERSION_SUPERSEDED))
                .execution_options(synchronize_session=False)
            )
            self._transition(version, VERSION_PUBLISHED, sources=(VERSION_PENDING,), published_at=now)   # 不是待審就拋出，上面的取代與結案一併回復
            closed = self._close_reports(superseded_ids, CLOSED_SUPERSEDED, now)
            self._lock_author_name(script_id, now)
        self._session.expire_all()
        script_events.log_reports_auto_closed(count=closed, result=CLOSED_SUPERSEDED)    # SAVEPOINT 成功之後才記；沒有檢舉（0 筆）就不產生事件
        return version

    def reject_version(self, version_public_id: str, category: str, note: str | None, now: datetime) -> ScriptVersion | None:
        """退回（審核流程專用）：`pending_review` → `rejected`，內容保留；記錄固定類別（`DECISION_CATEGORIES`）、
        只供管理員的審核備註（至多 500 字，可為空）與決定時間。類別不是列舉值或備註過長拋出 `ValueError`（版本不變）。
        找不到版本回傳 `None`；不是待審拋出 `InvalidVersionTransitionError`。"""
        category, note = _checked_decision(category, note)
        version = self._session.scalar(select(ScriptVersion).where(ScriptVersion.public_id == version_public_id))
        if version is None:
            return None
        self._transition(
            version, VERSION_REJECTED, sources=(VERSION_PENDING,),
            decision_category=category, decision_note=note, decided_at=now,
        )
        return version

    # ---- 下架與恢復（script-takedown 規格；誰下架誰恢復）--------------------------------------------

    def remove_own(self, user_id: int, version_public_id: str, now: datetime) -> ScriptVersion | None:
        """作者自行下架：`published` → `removed`，下架者為 `author`，不填類別與備註。不是自己的回傳 `None`；
        不是上架中拋出 `InvalidVersionTransitionError`。下架同時讓出「上架」的位置（輔助欄位為空）。"""
        version = self._owned_version(user_id, version_public_id)
        if version is None:
            return None
        self._remove(version, REMOVER_AUTHOR, None, None, now)
        return version

    def remove_by_admin(self, version_public_id: str, category: str, note: str | None, now: datetime) -> ScriptVersion | None:
        """管理員下架（審核流程專用）：`published` → `removed`，下架者為 `admin`，記錄固定類別與只供管理員的備註。
        內建劇本（有種子鍵）拋出 `BuiltinNotRemovableError`——**以種子鍵判斷，不是「沒有作者」**：匿名保留的劇本同樣沒有作者，
        它仍是玩家創作的劇本，管理員可以下架；類別或備註不合拋出 `ValueError`；找不到回傳 `None`；
        不是上架中拋出 `InvalidVersionTransitionError`。"""
        category, note = _checked_decision(category, note)
        version = self._session.scalar(select(ScriptVersion).where(ScriptVersion.public_id == version_public_id))
        if version is None:
            return None
        if self.get_script_of(version).seed_key is not None:
            raise BuiltinNotRemovableError
        self._remove(version, REMOVER_ADMIN, category, note, now)
        return version

    def restore_own(self, user_id: int, version_public_id: str) -> ScriptVersion | None:
        """作者恢復自己下架的版本：`removed` → `published`（不重審，內容與 `published_at` 不變），清除四個決定欄位。
        不是自己的回傳 `None`；管理員下架的拋出 `RemovalLockedError`；不是下架狀態拋出 `InvalidVersionTransitionError`；
        同劇本已有另一個上架版本拋出 `LiveVersionExistsError`（版本保持 `removed`）。"""
        version = self._owned_version(user_id, version_public_id)
        if version is None:
            return None
        self._restore(version, REMOVER_AUTHOR)
        return version

    def restore_by_admin(self, version_public_id: str) -> ScriptVersion | None:
        """管理員恢復自己下架的版本（只限下架者為 `admin`）。找不到回傳 `None`；作者自行下架的拋出 `RemovedByAuthorError`；
        其餘例外同 `restore_own`。"""
        version = self._session.scalar(select(ScriptVersion).where(ScriptVersion.public_id == version_public_id))
        if version is None:
            return None
        self._restore(version, REMOVER_ADMIN)
        return version

    def _remove(self, version: ScriptVersion, remover: str, category: str | None, note: str | None, now: datetime) -> None:
        """下架（作者與管理員共用）：轉移成功之後，同一個（請求）交易內把這個版本未結案的檢舉結為 `removed`。轉移沒成功
        （不是上架中、並行搶先）時拋出例外、不會結案；結案本身失敗時例外使請求交易整個回復，不會留下「已下架但檢舉還開著」。
        刻意**不**另開 SAVEPOINT：這裡只有兩個單一語句的 UPDATE，請求交易已提供原子性，而多包一層會踩到 pysqlite 在沒有進行中的
        交易時 RELEASE 直接提交的已知行為（只影響本機 SQLite，見 `tests/test_admin_service_scripts.py` 的註解）。"""
        self._transition(
            version, VERSION_REMOVED, sources=(VERSION_PUBLISHED,),
            removed_by=remover, decision_category=category, decision_note=note, decided_at=now,
        )
        script_events.log_reports_auto_closed(count=self._reports.close_open(version.id, CLOSED_REMOVED, now), result=CLOSED_REMOVED)

    def _restore(self, version: ScriptVersion, remover: str) -> None:
        """恢復：條件式 UPDATE（`status = 'removed'` 且 `removed_by` 等於恢復者）在 SAVEPOINT 內執行。沒有命中時讀回現況，
        區分「不是下架狀態」與「下架者不同」；唯一索引衝突（同劇本已有另一個上架版本）轉成 `LiveVersionExistsError`。"""
        try:
            with self._session.begin_nested():
                self._transition(
                    version, VERSION_PUBLISHED, sources=(VERSION_REMOVED,), where=(ScriptVersion.removed_by == remover,),
                    removed_by=None, decision_category=None, decision_note=None, decided_at=None,
                )
        except IntegrityError as exc:
            raise LiveVersionExistsError from exc
        except InvalidVersionTransitionError:
            current = self._session.execute(
                select(ScriptVersion.status, ScriptVersion.removed_by).where(ScriptVersion.id == version.id)
            ).one_or_none()
            if current is not None and current.status == VERSION_REMOVED:
                raise (RemovalLockedError if remover == REMOVER_AUTHOR else RemovedByAuthorError) from None
            raise

    def _lock_author_name(self, script_id: int, now: datetime) -> None:
        """第一次發佈時鎖定筆名；已鎖定的不改動（新版本沿用原筆名）。"""
        self._session.execute(
            update(Script).where(Script.id == script_id, Script.author_locked_at.is_(None)).values(author_locked_at=now)
            .execution_options(synchronize_session=False)
        )

    # ---- 內建劇本（種子）---------------------------------------------------------------

    def get_builtin(self, seed_key: str) -> tuple[Script, ScriptVersion | None] | None:
        """以種子鍵找內建劇本與它目前上架的版本；沒有這個種子鍵回傳 `None`。"""
        script = self._session.scalar(select(Script).where(Script.seed_key == seed_key))
        if script is None:
            return None
        live = self._session.scalar(select(ScriptVersion).where(ScriptVersion.live_script_id == script.id))
        return script, live

    def publish_builtin(
        self, seed_key: str, *, content: Mapping[str, Any], content_hash: str, total_chars: int, now: datetime
    ) -> tuple[ScriptVersion, bool]:
        """內建劇本直接上架（不經審核——唯一繞過審核的路徑，只有種子服務呼叫）。

        沒有這個種子鍵就建立劇本（沒有作者、筆名「官方」並鎖定）與版本 1；已有則新增下一個版本並取代舊的上架版本。
        回傳 `(版本, 是否新建立劇本)`。
        """
        existing = self._session.scalar(select(Script).where(Script.seed_key == seed_key))
        superseded_ids = [] if existing is None else list(self._session.scalars(     # 在 SAVEPOINT 之前讀（理由見 `approve_version`）
            select(ScriptVersion.id).where(ScriptVersion.script_id == existing.id, ScriptVersion.status == VERSION_PUBLISHED)
        ))
        with self._session.begin_nested():
            created = existing is None
            if existing is None:
                try:
                    existing = Script(seed_key=seed_key, author_name=OFFICIAL_AUTHOR_NAME, author_locked_at=now)
                    self._session.add(existing)
                    self._session.flush()
                except IntegrityError as exc:
                    raise DuplicateSeedKeyError from exc
            self._session.execute(
                update(ScriptVersion)
                .where(ScriptVersion.script_id == existing.id, ScriptVersion.status == VERSION_PUBLISHED)
                .values(status=VERSION_SUPERSEDED, **_helper_columns(existing.id, VERSION_SUPERSEDED))
                .execution_options(synchronize_session=False)
            )
            self._close_reports(superseded_ids, CLOSED_SUPERSEDED, now)   # 內建劇本不可被檢舉，通常沒有；與其他入口同一個不變量
            version = self._insert_version(
                existing.id, self._next_version_no(existing.id), VERSION_PUBLISHED, content, content_hash, total_chars, published_at=now
            )
        self._session.expire_all()
        return version, created

    # ---- 匿名保留（rpg-script-reports 的 design D6）-------------------------------------------------

    def retain_published_anonymously(self, user_id: int) -> RetainResult:
        """作者的帳號即將被抹除、且選擇保留時：只留下**目前上架**的版本並解除作者關聯，其餘一律刪除。回傳保留與刪除的版本數。

        只由 `AccountErasureService` 在**刪除使用者之前、同一個交易內**呼叫（失敗與條件式刪除沒刪到時由呼叫端回復整個交易）。
        保留集合 K：作者的劇本中 `published` 的版本（`live_script_id` 非空；下架的版本不論下架者是誰都不算上架）；
        刪除集合 D：作者其餘所有版本（草稿、待審、被退回、被取代、已下架）。步驟的**順序不能換**，因為兩個無動作外鍵會擋下反過來的刪除：
        ① D 的版本被存檔參照的來源參照清為空值、D 的檢舉刪除 → ② 刪除 D 的版本（標籤串聯）→ ③ 刪除沒有保留版本的劇本 →
        ④ 保留的劇本解除作者關聯（筆名「已註銷的作者」、短代號清空）並清除 K 的審核備註。保留版本的檢舉與別人存檔對它的參照原樣保留。
        沒有任何劇本時什麼都不做。全部是 ORM 表達式，編號集合分批以避開 SQL Server 的參數上限。
        """
        self._session.flush()
        rows = self._session.execute(
            select(ScriptVersion.id, ScriptVersion.script_id, ScriptVersion.status)
            .join(Script, Script.id == ScriptVersion.script_id)
            .where(Script.creator_id == user_id)
        ).all()
        kept_ids = [row.id for row in rows if row.status == VERSION_PUBLISHED]
        kept_scripts = {row.script_id for row in rows if row.status == VERSION_PUBLISHED}
        deleted_ids = [row.id for row in rows if row.status != VERSION_PUBLISHED]
        all_scripts = list(self._session.scalars(select(Script.id).where(Script.creator_id == user_id)))
        if not all_scripts:
            return RetainResult(kept=0, deleted=0)
        for chunk in _chunks(deleted_ids):                                           # ① 無動作外鍵：先清參照與檢舉
            self._session.execute(
                update(RpgSession).where(RpgSession.source_version_id.in_(chunk)).values(source_version_id=None)
                .execution_options(synchronize_session=False)
            )
        self._reports.delete_for_versions(deleted_ids)
        for chunk in _chunks(deleted_ids):                                           # ② 刪除 D 的版本（標籤串聯）
            self._session.execute(
                delete(ScriptVersion).where(ScriptVersion.id.in_(chunk)).execution_options(synchronize_session=False)
            )
        for chunk in _chunks([script_id for script_id in all_scripts if script_id not in kept_scripts]):
            self._session.execute(                                                   # ③ 沒有保留版本的劇本（此時已沒有版本）
                delete(Script).where(Script.id.in_(chunk)).execution_options(synchronize_session=False)
            )
        for chunk in _chunks(sorted(kept_scripts)):                                  # ④ 保留的劇本解除作者關聯
            self._session.execute(
                update(Script).where(Script.id.in_(chunk))
                .values(creator_id=None, author_name=RETIRED_AUTHOR_NAME, author_code=None)
                .execution_options(synchronize_session=False)
            )
        for chunk in _chunks(kept_ids):
            self._session.execute(
                update(ScriptVersion).where(ScriptVersion.id.in_(chunk)).values(decision_note=None)
                .execution_options(synchronize_session=False)
            )
        self._session.expire_all()
        return RetainResult(kept=len(kept_ids), deleted=len(deleted_ids))

    # ---- 公開讀取：只回傳上架中的版本 ----------------------------------------------------------

    def get_published(self, version_public_id: str) -> ScriptVersion | None:
        return self._session.scalar(
            select(ScriptVersion).where(ScriptVersion.public_id == version_public_id, ScriptVersion.status == VERSION_PUBLISHED)
        )

    def get_published_by_script(self, script_public_id: str) -> ScriptVersion | None:
        """這個劇本目前上架的版本（沒有上架中的版本回傳 `None`）。"""
        return self._session.scalar(
            select(ScriptVersion)
            .join(Script, Script.id == ScriptVersion.script_id)
            .where(Script.public_id == script_public_id, ScriptVersion.status == VERSION_PUBLISHED)
        )

    def list_published(
        self, *, limit: int = 20, offset: int = 0, preference: str | None = None,
    ) -> list[ScriptVersion]:
        """上架中的全部版本，不依內容模式篩選（由新到舊）。

        `preference`（故事偏好：`male`／`female`）只影響順序：符合偏好的頻道排最前，其次全讀者，其餘仍在清單內；同一層維持由新到舊。
        排序在 LIMIT／OFFSET 之前完成，所以分頁之間的順序一致。空值、`all` 或無法辨識時與不帶偏好的查詢完全相同。
        """
        statement = _published_window(select(ScriptVersion), limit=limit, offset=offset, preference=preference)
        return list(self._session.scalars(statement))

    def list_catalog(
        self, *, terms: Sequence[str] = (), tag: str | None = None, audience: str | None = None,
        after: tuple[int, datetime, int] | None = None, preference: str | None = None, limit: int = 12,
    ) -> list[tuple[ScriptVersion, Script, int]]:
        """公開目錄的一頁（rpg-script-catalog 規格；取代入口最小版的 `list_published_cards`）。回傳 `(版本, 劇本, 偏好層)`，**一次查詢**
        連同劇本（筆名與短代號）取回；呼叫端要標籤時另用 `tags_of_versions` 一次取回整頁，所以整頁的查詢次數固定、不隨筆數增加。

        - **集合**：只含 `published`，不依內容模式篩選（所有玩家都看得到所有上架的劇本）。`terms`（已正規化的關鍵字詞，全部要符合）
          只比對標題、簡介、世界名稱與標籤；`tag` 是不分大小寫的完整標籤比對；`audience` 為 `male`／`female` 時只含該頻道，
          為 `all` 時含全讀者與舊的空值。三者 AND 組合。
        - **順序**：偏好層（`preference` 為 `male`／`female` 時才分層）、發佈時間由新到舊、版本編號由新到舊。
        - **游標**：`after = (偏好層, 發佈時間, 版本編號)` 是上一頁最後一筆的排序鍵，這一頁只含排在它**之後**的項目；條件與排序用同三個鍵
          （偏好層用同一個 `case` 表達式），所以分頁之間有新上架、下架或恢復時，已讀過之後的項目不重複也不遺漏（恢復不改發佈時間）。
          不分層時（偏好為全讀者）忽略 `after` 的偏好層。
        - 偏好層由查詢一併回傳（不在 Python 重算，兩處規則不會分歧）；不分層時一律 0。呼叫端取 `limit + 1` 筆決定有沒有下一頁。
        """
        rank = _audience_rank(preference)
        columns: list[Any] = [ScriptVersion, Script]
        if rank is not None:
            columns.append(rank.label("catalog_rank"))
        statement = (
            select(*columns)
            .join(Script, Script.id == ScriptVersion.script_id)
            .where(ScriptVersion.status == VERSION_PUBLISHED)
        )
        for term in terms:
            statement = statement.where(_keyword_filter(term))
        if tag is not None:
            statement = statement.where(
                exists().where(ScriptVersionTag.version_id == ScriptVersion.id, func.lower(ScriptVersionTag.tag, type_=Unicode) == tag)
            )
        if audience in (AUDIENCE_MALE, AUDIENCE_FEMALE):
            statement = statement.where(ScriptVersion.audience == audience)
        elif audience == AUDIENCE_ALL:
            statement = statement.where(or_(ScriptVersion.audience.is_(None), ScriptVersion.audience == AUDIENCE_ALL))
        if after is not None:
            after_rank, after_time, after_id = after
            later = or_(
                ScriptVersion.published_at < after_time,
                and_(ScriptVersion.published_at == after_time, ScriptVersion.id < after_id),
            )
            statement = statement.where(later if rank is None else or_(rank > after_rank, and_(rank == after_rank, later)))
        statement = statement.order_by(*_audience_order(preference), ScriptVersion.published_at.desc(), ScriptVersion.id.desc()).limit(limit)
        return [(row[0], row[1], row[2] if rank is not None else 0) for row in self._session.execute(statement).all()]

    def get_published_card(self, version_public_id: str) -> tuple[ScriptVersion, Script] | None:
        """上架中的版本連同它的劇本（詳情要顯示筆名與短代號），一次查詢；非上架的版本（含下架的、他人的草稿）與不存在的一樣回傳 `None`。"""
        row = self._session.execute(
            select(ScriptVersion, Script)
            .join(Script, Script.id == ScriptVersion.script_id)
            .where(ScriptVersion.public_id == version_public_id, ScriptVersion.status == VERSION_PUBLISHED)
        ).first()
        return None if row is None else (row[0], row[1])

    def popular_tags(self, *, limit: int = 30) -> list[tuple[str, int]]:
        """上架版本中最常用的標籤：`(小寫標籤, 使用該標籤的上架版本數)`，依數量由多到少、同數依標籤排序，至多 `limit` 筆。

        只計 `published`（下架、待審、草稿、被取代與被退回的版本不計）；以 `lower()` 分組，所以只差大小寫的標籤合併成一筆。
        同一版本內不會有只差大小寫的兩個標籤（驗證器去重），所以筆數就是版本數。同數時的標籤順序依資料庫的定序
        （SQLite 是字碼順序、SQL Server 依其定序，只影響同數量標籤的先後）。"""
        lowered = func.lower(ScriptVersionTag.tag, type_=Unicode)
        used = func.count(ScriptVersionTag.id)
        rows = self._session.execute(
            select(lowered, used)
            .select_from(ScriptVersionTag)
            .join(ScriptVersion, ScriptVersion.id == ScriptVersionTag.version_id)
            .where(ScriptVersion.status == VERSION_PUBLISHED)
            .group_by(lowered)
            .order_by(used.desc(), lowered.asc())
            .limit(limit)
        ).all()
        return [(row[0], int(row[1])) for row in rows]

    def list_published_by_tag(
        self, tag: str, *, limit: int = 20, offset: int = 0, preference: str | None = None,
    ) -> list[ScriptVersion]:
        """依標籤查詢上架中的版本（不分大小寫）；`preference` 的意義同 `list_published`。"""
        statement = (
            select(ScriptVersion)
            .join(ScriptVersionTag, ScriptVersionTag.version_id == ScriptVersion.id)
            .where(
                ScriptVersion.status == VERSION_PUBLISHED,
                func.lower(ScriptVersionTag.tag) == tag.casefold(),
            )
            .order_by(*_audience_order(preference), ScriptVersion.published_at.desc(), ScriptVersion.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement))

    def get_startable(self, user_id: int, version_public_id: str) -> ScriptVersion | None:
        """可以拿來開存檔的版本：上架中的任何版本，或呼叫者自己的任何狀態的版本（**下架的除外**，包含作者自己）；
        別人的非上架版本、下架的版本與不存在的一樣回傳 `None`。"""
        return self._session.scalar(
            select(ScriptVersion)
            .join(Script, Script.id == ScriptVersion.script_id)
            .where(
                ScriptVersion.public_id == version_public_id,
                ScriptVersion.status != VERSION_REMOVED,
                (ScriptVersion.status == VERSION_PUBLISHED) | (Script.creator_id == user_id),
            )
        )

    # ---- 審查結果（script-review 規格；唯一寫入審查欄位的方法）---------------------------------

    def record_review(self, version_id: int, content_hash: str, verdict: str, categories: Sequence[str], now: datetime) -> bool:
        """記錄模型層對「這份內容」的審查結果。條件式 UPDATE（`content_hash` 必須仍等於審查時的雜湊）：審查期間內容被修改時
        不寫入，回傳 `False`，舊結果不會蓋在新內容上。結果是中繼資料，不改變內容，所以任何狀態的版本都可以記錄。
        結果與類別必須是固定列舉（程式錯誤時拋出 `ValueError`）。"""
        if verdict not in REVIEW_VERDICTS or any(category not in REVIEW_CATEGORIES for category in categories):
            raise ValueError("審查結果與類別必須是固定列舉。")
        result = self._session.execute(
            update(ScriptVersion)
            .where(ScriptVersion.id == version_id, ScriptVersion.content_hash == content_hash)
            .values(review_hash=content_hash, review_verdict=verdict, review_categories_json=list(categories), reviewed_at=now)
            .execution_options(synchronize_session=False)
        )
        self._expire(ScriptVersion, version_id)
        return result.rowcount == 1

    # ---- 匯出（個人資料匯出用）-------------------------------------------------------------

    def export_scripts(self, user_id: int, *, page_size: int = 50) -> Iterator[dict[str, Any]]:
        """這位使用者創作的全部劇本，每個劇本一筆，含**全部**版本（草稿、待審、被退回與已被取代的也在內），由舊到新逐筆產生
        （以劇本編號為游標分頁，所以記憶體用量與劇本總數無關）。

        只含使用者自己的內容：劇本的筆名與時間、每個版本的版本編號、狀態（含 `removed`）、審核與下架的結果（固定類別、時間、下架者）、
        全部固定規格欄位與標籤。**不含**審核備註、種子鍵、內容雜湊、內容字數、輔助欄位與內部整數編號（對外只有 UUID）。
        時間是 `datetime`（UTC），由呼叫端決定格式。
        """
        after = 0
        while True:
            scripts = list(self._session.scalars(
                select(Script).where(Script.creator_id == user_id, Script.id > after).order_by(Script.id).limit(page_size)
            ))
            if not scripts:
                return
            for script in scripts:
                versions = list(self._session.scalars(
                    select(ScriptVersion).where(ScriptVersion.script_id == script.id).order_by(ScriptVersion.version_no)
                ))
                tags: dict[int, list[str]] = {}
                for version_id, tag in self._session.execute(
                    select(ScriptVersionTag.version_id, ScriptVersionTag.tag)
                    .join(ScriptVersion, ScriptVersion.id == ScriptVersionTag.version_id)
                    .where(ScriptVersion.script_id == script.id)
                    .order_by(ScriptVersionTag.id)
                ).all():
                    tags.setdefault(version_id, []).append(tag)
                yield {
                    "id": script.public_id, "author_name": script.author_name, "author_code": script.author_code,
                    "author_locked_at": script.author_locked_at, "created_at": script.created_at,
                    "versions": [
                        {
                            "version_no": v.version_no, "status": v.status, "spec_version": v.spec_version, "title": v.title,
                            "summary": v.summary, "world_name": v.world_name, "content_tier": v.content_tier,
                            "audience": v.audience or AUDIENCE_ALL,
                            "background": v.background, "opening": v.opening, "rules": v.rules, "tags": tags.get(v.id, []),
                            "calendar": {"name": v.calendar_name, "start_year": v.calendar_start_year} if v.calendar_name is not None else None,
                            "ending_mode": v.ending_mode, "ending_text": v.ending_text, "ending_age": v.ending_age,
                            "characters": list(v.characters_json or []), "locations": list(v.locations_json or []),
                            "milestones": list(v.milestones_json or []), "created_at": v.created_at, "published_at": v.published_at,
                            # 審核與下架的結果：固定類別、時間與下架者。**不含審核備註**（只有管理員看得到，見 script-takedown 規格）
                            "decision_category": v.decision_category, "decided_at": v.decided_at, "removed_by": v.removed_by,
                        }
                        for v in versions
                    ],
                }
            after = scripts[-1].id

    # ---- 版本內容 -----------------------------------------------------------------------

    def tags_of(self, version: ScriptVersion) -> list[str]:
        return list(self._session.scalars(select(ScriptVersionTag.tag).where(ScriptVersionTag.version_id == version.id).order_by(ScriptVersionTag.id)))

    def tags_of_versions(self, version_ids: Sequence[int]) -> dict[int, list[str]]:
        """多個版本的標籤，一次 `IN` 查詢取回（順序規則同 `tags_of`）。每個傳入的版本都有一個鍵，沒有標籤的為空串列；
        傳入空清單回傳空字典、不查詢。呼叫端傳入一頁的版本編號（至多數十個），不是全部版本。"""
        ids = list(dict.fromkeys(version_ids))
        if not ids:
            return {}
        grouped: dict[int, list[str]] = {version_id: [] for version_id in ids}
        for version_id, tag in self._session.execute(
            select(ScriptVersionTag.version_id, ScriptVersionTag.tag).where(ScriptVersionTag.version_id.in_(ids)).order_by(ScriptVersionTag.id)
        ).all():
            grouped[version_id].append(tag)
        return grouped

    def content_of(self, version: ScriptVersion) -> dict[str, Any]:
        """由欄位還原標準化內容（與 `validate_script` 的輸出相同，雜湊也相同）：存檔開局時複製的就是它。"""
        calendar = (
            {"name": version.calendar_name, "start_year": version.calendar_start_year} if version.calendar_name is not None else None
        )
        content = {
            "spec_version": version.spec_version, "title": version.title, "summary": version.summary, "world_name": version.world_name,
            "content_tier": version.content_tier, "background": version.background, "opening": version.opening, "rules": version.rules,
            "tags": self.tags_of(version), "calendar": calendar, "ending_mode": version.ending_mode, "ending_text": version.ending_text,
            "ending_age": version.ending_age, "characters": list(version.characters_json or []), "locations": list(version.locations_json or []),
            "milestones": list(version.milestones_json or []), "cover_image": None,
        }
        if version.audience in (AUDIENCE_MALE, AUDIENCE_FEMALE):      # 全讀者與空值（舊版本）不帶這個鍵，雜湊才與標準化內容相同
            content["audience"] = version.audience
        return content
