"""劇本與劇本版本的資料存取（見 script-store 規格、AGENTS.md 第 4、7、10 節、rpg-data-and-script 的 design D2、D11）。

只用 ORM 表達式，不提交交易（請求結束時統一提交）。

**讀取範圍**
- 擁有者範圍的方法（名稱含 `owned`，或第一個參數是 `user_id`）一律以 `Script.creator_id == user_id` 為查詢條件；
  別人的資源與不存在的資源得到相同的結果（`None`），Service 據此回 404。
- 公開讀取（`get_published`、`list_published`…）只回傳 `published` 的版本；沒有「只憑識別碼取得任意版本」的方法。
- 管理員讀取他人的待審內容走後續審核流程的專用路徑並寫稽核，不在這裡。

**狀態與不可變**
- 版本的狀態與兩個輔助欄位（`live_script_id`、`open_script_id`）**只在這個檔案裡被寫入**（`tests/architecture` 有測試擋住別處）：
  建立版本與下面的轉移方法。轉移集合固定，每次轉移是單一條件式 UPDATE（`WHERE status IN (允許的來源)`），
  並行的兩個請求只有一個成功。輔助欄位在狀態為 `published` 時等於 `script_id`、為 `draft` 或 `pending_review` 時
  等於 `script_id`（另一個欄位），其餘為空；兩者各有部分唯一索引，所以「每個劇本至多一個上架版本與一個進行中的版本」
  由資料庫保證，不是靠程式檢查。
- 只有 `draft` 的內容可以修改或刪除（條件式 UPDATE／DELETE）；其餘狀態的內容與標籤不可變。
- 核可（`approve_version`）把同一劇本原本上架的版本改為 `superseded` 並上架新版本，在同一個 SAVEPOINT 內完成，
  失敗時兩者都保持原狀態；第一次核可時鎖定筆名。

**標籤**以顯示形式儲存（保留原本的大小寫，這樣版本內容可以由欄位完整還原、雜湊不變）；驗證器已經以 NFC 與不分大小寫
去除重複，所以同一版本內不會有只差大小寫的兩個標籤。依標籤查詢時以小寫比對（`func.lower`，ORM 表達式，雙軌一致）。

**刪除版本與存檔的參照**：`sessions.source_version_id` 是無動作的外鍵（SQL Server 不允許多重串聯路徑），所以刪除版本之前先把
參照它的存檔清為空值；存檔本身不受影響（它有自己的劇本複本）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import util as orm_util

from app.models.rpg_session import RpgSession
from app.models.script import (
    ENDING_NONE, OFFICIAL_AUTHOR_NAME, OPEN_VERSION_STATUSES, VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED,
    VERSION_REJECTED, VERSION_SUPERSEDED, Script, ScriptVersion, ScriptVersionTag,
)
from app.models.user import CONTENT_TIER_SAFE, CONTENT_TIERS
from app.repositories.base import BaseRepository
from app.repositories.errors import (
    AuthorNameLockedError, DuplicateSeedKeyError, InvalidVersionTransitionError, OpenVersionExistsError, VersionNotDraftError,
)

# 目標狀態 → 允許的來源狀態（固定的轉移集合，見 script-store 規格「版本的狀態與轉移」）
_ALLOWED_SOURCES: dict[str, tuple[str, ...]] = {
    VERSION_PENDING: (VERSION_DRAFT,),            # 送審
    VERSION_DRAFT: (VERSION_PENDING,),            # 撤回
    VERSION_PUBLISHED: (VERSION_PENDING,),        # 核可
    VERSION_REJECTED: (VERSION_PENDING,),         # 退回
    VERSION_SUPERSEDED: (VERSION_PUBLISHED,),     # 被新的上架版本取代
}


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


def _tiers_up_to(max_tier: str) -> tuple[str, ...]:
    """不高於 `max_tier` 的全部內容模式；無法判定的上限視為最保守的安全模式。"""
    if max_tier not in CONTENT_TIERS:
        return (CONTENT_TIER_SAFE,)
    return CONTENT_TIERS[: CONTENT_TIERS.index(max_tier) + 1]


class ScriptRepository(BaseRepository[Script]):
    model = Script

    # ---- 內部工具 ---------------------------------------------------------------

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

    def _transition(self, version: ScriptVersion, to_status: str, **extra: Any) -> None:
        """唯一寫入版本狀態（與輔助欄位）的地方：條件式 UPDATE，來源狀態不符時不改任何東西。"""
        result = self._session.execute(
            update(ScriptVersion)
            .where(ScriptVersion.id == version.id, ScriptVersion.status.in_(_ALLOWED_SOURCES[to_status]))
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

    def get_script_of(self, version: ScriptVersion) -> Script:
        script = self._session.get(Script, version.script_id)
        assert script is not None, "版本的外鍵保證劇本存在"
        return script

    # ---- 擁有者：修改與刪除草稿 ---------------------------------------------------------

    def update_draft(
        self, user_id: int, version_public_id: str, *, content: Mapping[str, Any], content_hash: str, total_chars: int
    ) -> ScriptVersion | None:
        """覆寫自己草稿的內容與標籤。不是自己的回傳 `None`；不是草稿拋出 `VersionNotDraftError`，內容保持原樣。"""
        version = self._owned_version(user_id, version_public_id)
        if version is None:
            return None
        with self._session.begin_nested():
            result = self._session.execute(
                update(ScriptVersion)
                .where(ScriptVersion.id == version.id, ScriptVersion.status == VERSION_DRAFT)
                .values(**_content_columns(content, content_hash, total_chars))
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise VersionNotDraftError
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

    def submit_for_review(self, user_id: int, version_public_id: str) -> ScriptVersion | None:
        """擁有者送審：`draft` → `pending_review`（內容從此凍結）。不是自己的回傳 `None`。"""
        version = self._owned_version(user_id, version_public_id)
        if version is None:
            return None
        self._transition(version, VERSION_PENDING)
        return version

    def withdraw(self, user_id: int, version_public_id: str) -> ScriptVersion | None:
        """擁有者撤回：`pending_review` → `draft`（要修改須先撤回）。不是自己的回傳 `None`。"""
        version = self._owned_version(user_id, version_public_id)
        if version is None:
            return None
        self._transition(version, VERSION_DRAFT)
        return version

    def approve_version(self, version_public_id: str, *, now: datetime) -> ScriptVersion | None:
        """核可（審核流程專用，不是玩家路徑）：`pending_review` → `published`，同一劇本原本上架的版本在同一個 SAVEPOINT 內
        改為 `superseded`；第一次核可時鎖定筆名。失敗時兩者都保持原狀態。找不到版本回傳 `None`。"""
        version = self._session.scalar(select(ScriptVersion).where(ScriptVersion.public_id == version_public_id))
        if version is None:
            return None
        script_id = version.script_id
        with self._session.begin_nested():
            self._session.execute(                      # 先讓出上架的位置（部分唯一索引：同一劇本至多一個上架版本）
                update(ScriptVersion)
                .where(ScriptVersion.script_id == script_id, ScriptVersion.status == VERSION_PUBLISHED)
                .values(status=VERSION_SUPERSEDED, **_helper_columns(script_id, VERSION_SUPERSEDED))
                .execution_options(synchronize_session=False)
            )
            self._transition(version, VERSION_PUBLISHED, published_at=now)     # 不是待審就拋出，上面的取代一併回復
            self._lock_author_name(script_id, now)
        self._session.expire_all()
        return version

    def reject_version(self, version_public_id: str) -> ScriptVersion | None:
        """退回（審核流程專用）：`pending_review` → `rejected`，內容保留。找不到版本回傳 `None`。"""
        version = self._session.scalar(select(ScriptVersion).where(ScriptVersion.public_id == version_public_id))
        if version is None:
            return None
        self._transition(version, VERSION_REJECTED)
        return version

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
            version = self._insert_version(
                existing.id, self._next_version_no(existing.id), VERSION_PUBLISHED, content, content_hash, total_chars, published_at=now
            )
        self._session.expire_all()
        return version, created

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

    def list_published(self, max_tier: str, *, limit: int = 20, offset: int = 0) -> list[ScriptVersion]:
        """上架中的版本，宣告的內容模式不高於 `max_tier`（由新到舊）。"""
        statement = (
            select(ScriptVersion)
            .where(ScriptVersion.status == VERSION_PUBLISHED, ScriptVersion.content_tier.in_(_tiers_up_to(max_tier)))
            .order_by(ScriptVersion.published_at.desc(), ScriptVersion.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement))

    def list_published_by_tag(self, tag: str, max_tier: str, *, limit: int = 20, offset: int = 0) -> list[ScriptVersion]:
        """依標籤查詢上架中的版本（不分大小寫）。"""
        statement = (
            select(ScriptVersion)
            .join(ScriptVersionTag, ScriptVersionTag.version_id == ScriptVersion.id)
            .where(
                ScriptVersion.status == VERSION_PUBLISHED,
                ScriptVersion.content_tier.in_(_tiers_up_to(max_tier)),
                func.lower(ScriptVersionTag.tag) == tag.casefold(),
            )
            .order_by(ScriptVersion.published_at.desc(), ScriptVersion.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement))

    def get_startable(self, user_id: int, version_public_id: str) -> ScriptVersion | None:
        """可以拿來開存檔的版本：上架中的任何版本，或呼叫者自己的任何狀態的版本；別人的非上架版本與不存在的一樣回傳 `None`。"""
        return self._session.scalar(
            select(ScriptVersion)
            .join(Script, Script.id == ScriptVersion.script_id)
            .where(
                ScriptVersion.public_id == version_public_id,
                (ScriptVersion.status == VERSION_PUBLISHED) | (Script.creator_id == user_id),
            )
        )

    # ---- 匯出（個人資料匯出用）-------------------------------------------------------------

    def export_scripts(self, user_id: int, *, page_size: int = 50) -> Iterator[dict[str, Any]]:
        """這位使用者創作的全部劇本，每個劇本一筆，含**全部**版本（草稿、待審、被退回與已被取代的也在內），由舊到新逐筆產生
        （以劇本編號為游標分頁，所以記憶體用量與劇本總數無關）。

        只含使用者自己的內容：劇本的筆名與時間、每個版本的版本編號、狀態、全部固定規格欄位與標籤。**不含**種子鍵、內容雜湊、
        內容字數、輔助欄位與內部整數編號（對外只有 UUID）。時間是 `datetime`（UTC），由呼叫端決定格式。
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
                            "background": v.background, "opening": v.opening, "rules": v.rules, "tags": tags.get(v.id, []),
                            "calendar": {"name": v.calendar_name, "start_year": v.calendar_start_year} if v.calendar_name is not None else None,
                            "ending_mode": v.ending_mode, "ending_text": v.ending_text, "ending_age": v.ending_age,
                            "characters": list(v.characters_json or []), "locations": list(v.locations_json or []),
                            "milestones": list(v.milestones_json or []), "created_at": v.created_at, "published_at": v.published_at,
                        }
                        for v in versions
                    ],
                }
            after = scripts[-1].id

    # ---- 版本內容 -----------------------------------------------------------------------

    def tags_of(self, version: ScriptVersion) -> list[str]:
        return list(self._session.scalars(select(ScriptVersionTag.tag).where(ScriptVersionTag.version_id == version.id).order_by(ScriptVersionTag.id)))

    def content_of(self, version: ScriptVersion) -> dict[str, Any]:
        """由欄位還原標準化內容（與 `validate_script` 的輸出相同，雜湊也相同）：存檔開局時複製的就是它。"""
        calendar = (
            {"name": version.calendar_name, "start_year": version.calendar_start_year} if version.calendar_name is not None else None
        )
        return {
            "spec_version": version.spec_version, "title": version.title, "summary": version.summary, "world_name": version.world_name,
            "content_tier": version.content_tier, "background": version.background, "opening": version.opening, "rules": version.rules,
            "tags": self.tags_of(version), "calendar": calendar, "ending_mode": version.ending_mode, "ending_text": version.ending_text,
            "ending_age": version.ending_age, "characters": list(version.characters_json or []), "locations": list(version.locations_json or []),
            "milestones": list(version.milestones_json or []), "cover_image": None,
        }
