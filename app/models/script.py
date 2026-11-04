"""劇本、劇本版本與標籤（見 script-store 與 script-fixed-spec 規格、AGENTS.md 第 7、10、11 節、rpg-data-and-script 的 design D1、D2）。

- 對外只用 `public_id`（UUID）；內部整數編號不外露，授權也不依賴它的不可猜測性。
- `scripts` 是劇本的身分（作者、種子鍵、筆名快照），`script_versions` 是每個版本的內容與狀態：已發佈的版本不可變，
  修改一律產生新版本，所以內容放在版本上而不是劇本上。
- 作者與使用者的外鍵是 `ON DELETE CASCADE`，版本與標籤也串聯：失敗時偏向刪除，不會因為漏掉處理而留下作者的內容。
  `creator_id` 可為空（內建劇本沒有作者），空值不受串聯影響。
- 「每個劇本至多一個上架版本、至多一個進行中的版本」由資料庫保證：`live_script_id`、`open_script_id` 是輔助欄位，
  只在版本分別處於 `published`、`draft／pending_review` 時等於 `script_id`，其餘為空；各自以「只涵蓋非 NULL 列」的
  部分唯一索引（SQL Server 的 UNIQUE 只容許一個 NULL，所以不能直接對 `(script_id, status)` 設唯一）。
  這兩個欄位只由 `ScriptRepository` 的狀態轉移方法寫入。
- 文字欄位用 `Unicode`／`UnicodeText`（SQL Server 對應 NVARCHAR）；角色、地點、里程碑是 JSON，由 Repository 序列化，
  **不得依其內容做篩選查詢**。需要瀏覽與篩選的欄位（標題、世界觀名稱、宣告的內容模式、標籤）都是真正的欄位或獨立的表。
- 值域（狀態、結局模式）由本檔的常數與 Service 驗證，不加資料庫 CHECK 約束。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, Unicode, UnicodeText, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PublicIdMixin, TimestampMixin, UTCDateTime, unique_when_not_null
from app.models.user import CONTENT_TIER_SAFE

# ---- 值域 ---------------------------------------------------------------------

VERSION_DRAFT = "draft"                    # 作者編輯中
VERSION_PENDING = "pending_review"         # 已送審，內容凍結
VERSION_PUBLISHED = "published"            # 上架中（每個劇本至多一個）
VERSION_REJECTED = "rejected"              # 審核退回，內容保留
VERSION_SUPERSEDED = "superseded"          # 被較新的上架版本取代，內容保留
VERSION_STATUSES = (VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_SUPERSEDED)
OPEN_VERSION_STATUSES = (VERSION_DRAFT, VERSION_PENDING)     # 「進行中」：每個劇本至多一個

ENDING_NONE = "none"
ENDING_CUSTOM = "custom"
ENDING_MODES = (ENDING_NONE, ENDING_CUSTOM)

OFFICIAL_AUTHOR_NAME = "官方"               # 內建劇本的筆名（沒有作者）

# 審查結果（script-review 規格）：`pass` 通過、`flag` 有疑慮（進人工並標示類別）、`reject` 明確違反（直接拒絕）
REVIEW_PASS = "pass"
REVIEW_FLAG = "flag"
REVIEW_REJECT = "reject"
REVIEW_VERDICTS = (REVIEW_PASS, REVIEW_FLAG, REVIEW_REJECT)

# 審查的類別（固定列舉；對作者的回應與日誌都只有這些值，不含任何內容）。
# 可導致 `reject` 的四類紅線（AGENTS.md 第 7 節）：涉及未成年的性內容、性暴力、非自願的性情節、以真人名義的性內容。
CATEGORY_MINOR_SEXUAL = "minor_sexual"
CATEGORY_SEXUAL_VIOLENCE = "sexual_violence"
CATEGORY_NON_CONSENSUAL = "non_consensual"
CATEGORY_REAL_PERSON_SEXUAL = "real_person_sexual"
REJECT_CATEGORIES = (
    CATEGORY_MINOR_SEXUAL, CATEGORY_SEXUAL_VIOLENCE, CATEGORY_NON_CONSENSUAL, CATEGORY_REAL_PERSON_SEXUAL,
)
# 只能導致 `flag`（有疑慮，進人工並標示類別）的類別
CATEGORY_MINOR_SUSPECTED = "minor_suspected"
CATEGORY_REAL_PERSON_SUSPECTED = "real_person_suspected"
CATEGORY_TIER_MISMATCH = "tier_mismatch"                  # 宣告的內容模式與內容不符
CATEGORY_OTHER_CONCERN = "other_concern"
FLAG_CATEGORIES = (
    CATEGORY_MINOR_SUSPECTED, CATEGORY_REAL_PERSON_SUSPECTED, CATEGORY_TIER_MISMATCH, CATEGORY_OTHER_CONCERN,
)
REVIEW_CATEGORIES = REJECT_CATEGORIES + FLAG_CATEGORIES


class Script(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "scripts"
    __table_args__ = (
        # 內建劇本的穩定種子鍵：可為空（玩家劇本沒有）、非空時唯一
        unique_when_not_null("uq_scripts_seed_key", "seed_key"),
        Index("ix_scripts_creator_id", "creator_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    creator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    seed_key: Mapped[str | None] = mapped_column(Unicode(60), nullable=True)
    # 筆名與短代號的快照：第一次發佈（審核通過）時鎖定，之後不可更改；發佈前可重新取用目前的暱稱
    author_name: Mapped[str | None] = mapped_column(Unicode(20), nullable=True)
    author_code: Mapped[str | None] = mapped_column(Unicode(8), nullable=True)
    author_locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ScriptVersion(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "script_versions"
    __table_args__ = (
        UniqueConstraint("script_id", "version_no", name="uq_script_versions_script_version_no"),
        unique_when_not_null("uq_script_versions_live_script", "live_script_id"),
        unique_when_not_null("uq_script_versions_open_script", "open_script_id"),
        # 公開目錄：只看上架中的版本，依宣告的內容模式篩選
        Index("ix_script_versions_status_content_tier", "status", "content_tier"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id", ondelete="CASCADE"))
    version_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Unicode(16), default=VERSION_DRAFT)
    spec_version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(Unicode(64))
    total_chars: Mapped[int] = mapped_column(Integer, default=0)

    # 目錄資訊：真正的欄位，之後才能瀏覽與篩選
    title: Mapped[str] = mapped_column(Unicode(30))
    summary: Mapped[str] = mapped_column(Unicode(150))
    world_name: Mapped[str] = mapped_column(Unicode(50))
    content_tier: Mapped[str] = mapped_column(Unicode(10), default=CONTENT_TIER_SAFE)    # 作者宣告的內容模式

    # 設定
    background: Mapped[str] = mapped_column(UnicodeText)
    opening: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    rules: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    ending_mode: Mapped[str] = mapped_column(Unicode(10), default=ENDING_NONE)
    ending_text: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    ending_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calendar_name: Mapped[str | None] = mapped_column(Unicode(12), nullable=True)
    calendar_start_year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 角色、地點、里程碑：JSON，不依內容篩選
    characters_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    locations_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    milestones_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    # 唯一性輔助欄位（見檔案開頭）；只由 ScriptRepository 的狀態轉移方法寫入
    live_script_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_script_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # 審查結果（rpg-script-authoring，script-review 規格）：審查的中繼資料，**不是內容**，所以不受「非草稿的內容不可變」限制。
    # `review_hash` 是審查當時的 `content_hash`，與目前的 `content_hash` 相同才算數，內容有變動就自動失效。
    # 只由 `ScriptRepository.record_review` 寫入（條件式比對雜湊）。類別是固定列舉（`review_categories_json`），不含任何內容。
    review_hash: Mapped[str | None] = mapped_column(Unicode(64), nullable=True)
    review_verdict: Mapped[str | None] = mapped_column(Unicode(8), nullable=True)
    review_categories_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ScriptVersionTag(Base):
    """版本的標籤：獨立成表才能依標籤篩選（不得依 JSON 內容查詢）。標籤已標準化為小寫，同一版本內唯一。"""

    __tablename__ = "script_version_tags"
    __table_args__ = (
        UniqueConstraint("version_id", "tag", name="uq_script_version_tags_version_tag"),
        Index("ix_script_version_tags_tag", "tag"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("script_versions.id", ondelete="CASCADE"))
    tag: Mapped[str] = mapped_column(Unicode(12))
