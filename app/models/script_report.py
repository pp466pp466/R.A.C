"""劇本檢舉（見 script-reports 規格與 rpg-script-reports 的 design D1、D2）。

玩家對「已上架的劇本版本」的檢舉：預設原因（可多選的固定列舉）加選填的自寫文字。檢舉不自動下架，只升高管理員審核佇列的優先度；
是否下架由管理員決定，結案分「下架」「不成立」與系統專用的「被取代」。

- **檢舉掛在版本**：`UNIQUE (reporter_id, version_id)`，同一人對同一版本只能檢舉一次；新版本核可上架後可再檢舉。
- **外鍵設計**：`reporter_id` 是 `ON DELETE CASCADE`（檢舉人刪帳號時其檢舉一併刪除；玩家文字視為個資，漏處理時偏向刪除）。
  `version_id` **刻意沒有 `ondelete`（無動作）**：使用者刪除有兩條路徑會到達這張表——`users → script_reports`（檢舉人）與
  `users → scripts → script_versions → script_reports`（被檢舉版本的作者），SQL Server 不允許多重串聯路徑（錯誤 1785），
  與 `sessions.source_version_id` 同一個做法：由抹除流程在刪除版本之前明確清除，漏掉時刪除會直接失敗、不會留下懸空的編號。
  `tests/architecture/test_account_erasure_coverage.py` 登記這兩個外鍵。
- **`red_line` 為何另存**：優先度要依「是否有人勾選紅線理由」排序，而 AGENTS.md 第 4 節不允許依 JSON 欄位內容篩選或排序；
  `reasons_json` 只用來顯示，排序與聚合只讀 `red_line`。它在建立時由 Service 依理由算出且不可變，所以不會與 `reasons_json` 分歧。
- **結案欄位**（`closed_at`、`closed_result`）只由 `ScriptReportRepository` 寫入（`tests/architecture/test_script_report_writes.py` 守門）；
  版本離開上架（下架、被新版本取代）時由 `ScriptRepository` 的轉移內部呼叫它的方法自動結案。結案是歷史事實，恢復上架不重開。
- **玩家文字視為個資**（AGENTS.md 第 11 節）：`reasons_json` 與 `note` 都不送入模型、不進日誌；保存期限為結案後 12 個月
  （未結案者與所屬版本相同），檢舉人刪除帳號時一併刪除，並納入檢舉人的個人資料匯出；作者看不到檢舉人與檢舉內容。
- 值域（理由、結果）由本檔的常數與 Service 驗證，不加資料庫 CHECK 約束。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, Unicode, UnicodeText, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime

# ---- 值域 ---------------------------------------------------------------------

# 預設原因（可多選）；順序即畫面上的順序
REASON_MINOR_SEXUAL = "minor_sexual"                  # 涉及未成年的性內容（紅線）
REASON_SEXUAL_VIOLENCE = "sexual_violence"            # 性暴力或非自願情節（紅線）
REASON_REAL_PERSON_SEXUAL = "real_person_sexual"      # 以真人名義的性內容（紅線）
REASON_TIER_MISMATCH = "tier_mismatch"                # 內容分級不符
REASON_PLAGIARISM = "plagiarism"                      # 抄襲或侵權
REASON_PRIVACY_HARASSMENT = "privacy_harassment"      # 洩漏個資或騷擾
REASON_OTHER = "other"                                # 其他
REPORT_REASONS = (
    REASON_MINOR_SEXUAL, REASON_SEXUAL_VIOLENCE, REASON_REAL_PERSON_SEXUAL, REASON_TIER_MISMATCH,
    REASON_PLAGIARISM, REASON_PRIVACY_HARASSMENT, REASON_OTHER,
)
# 有人勾選其中任一項，檢舉在管理員佇列中排最前（AGENTS.md 第 7 節「公開目錄與檢舉」）
RED_LINE_REASONS = (REASON_MINOR_SEXUAL, REASON_SEXUAL_VIOLENCE, REASON_REAL_PERSON_SEXUAL)

# 結案結果：`removed` 版本被下架（作者自行下架或管理員下架）、`dismissed` 管理員判定不成立、`superseded` 版本被新版本取代（系統專用）
CLOSED_REMOVED = "removed"
CLOSED_DISMISSED = "dismissed"
CLOSED_SUPERSEDED = "superseded"
CLOSED_RESULTS = (CLOSED_REMOVED, CLOSED_DISMISSED, CLOSED_SUPERSEDED)

REPORT_NOTE_MAX_CHARS = 200                           # 自寫文字上限（Unicode 字元，NFC 正規化後計），純文字、不送入模型


class ScriptReport(Base, TimestampMixin):
    __tablename__ = "script_reports"
    __table_args__ = (
        # 同一人對同一版本只能檢舉一次；新版本核可上架後可再檢舉（唯一性按版本，不按劇本）
        UniqueConstraint("reporter_id", "version_id", name="uq_script_reports_reporter_id"),
        Index("ix_script_reports_version_id_closed_at", "version_id", "closed_at"),          # 管理員佇列與自動結案
        Index("ix_script_reports_reporter_id_created_at", "reporter_id", "created_at"),      # 每人每日上限
        Index("ix_script_reports_closed_at", "closed_at"),                                   # 結案後 12 個月的期限清除
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 無 ondelete：見檔頭「外鍵設計」；抹除流程在刪除版本之前明確清除
    version_id: Mapped[int] = mapped_column(ForeignKey("script_versions.id"))
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reasons_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    red_line: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    closed_result: Mapped[str | None] = mapped_column(Unicode(12), nullable=True)
