"""RPG 存檔與回合（見 rpg-sessions 規格、AGENTS.md 第 6、7、10、11 節、rpg-data-and-script 的 design D3、D4）。

- 資料表名稱 `sessions` 與 `memory_logs` 沿用 AGENTS.md 與 RPG.md；類別叫 `RpgSession`，避免與 SQLAlchemy 的 `Session` 同名。
- 開存檔時把劇本版本的標準化內容整份複製進 `script_snapshot_json`，之後存檔不再讀取劇本：作者改版、下架或刪除帳號都不影響存檔。
  `source_version_id` 只是可為空的參照（無動作的外鍵；來源被刪除前由流程先清為空值，見欄位旁的說明），不阻止刪除。
- 存檔與回合以串聯刪除隨使用者消失；對外只用存檔的 `public_id`（UUID），回合不對外暴露。
- 回合：`(session_id, client_msg_id)` 與 `(session_id, seq)` 唯一，使冪等與順序由資料庫保證；`turn_number`（輪數）可為空，
  只在回合完成時才指派，所以中斷與被擋的回合不計輪。
- JSON 欄位（劇本複本、NPC 狀態、目標、里程碑進度、回合的結構化結果）由 Repository 序列化，**不得依其內容做篩選查詢**。
- 存檔與回合的內容（行動、劇情原文、記事簿、世界狀態、劇本文字）是個資：不進日誌、不進管理員預設可檢視的範圍。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, Unicode, UnicodeText, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PublicIdMixin, TimestampMixin, UTCDateTime, utcnow
from app.models.user import CONTENT_TIER_SAFE

SESSION_ACTIVE = "active"
SESSION_ENDED = "ended"                    # 已完結：歷史仍可讀，不得再新增回合，不可恢復
SESSION_STATUSES = (SESSION_ACTIVE, SESSION_ENDED)

TURN_STREAMING = "streaming"               # 生成中
TURN_COMPLETE = "complete"                 # 完整的 AI 回應；只有這個狀態計入輪數與上下文
TURN_INTERRUPTED = "interrupted"           # 串流中斷，保留已收到的片段
TURN_BLOCKED = "blocked"                   # 被審查擋下，不進上下文
TURN_STATUSES = (TURN_STREAMING, TURN_COMPLETE, TURN_INTERRUPTED, TURN_BLOCKED)


class RpgSession(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "sessions"
    __table_args__ = (
        # 「我的存檔」依最後活動由新到舊
        Index("ix_sessions_user_id_last_active_at", "user_id", "last_active_at"),
        # 期限清除（data-retention）：全表依最後活動時間找出 12 個月前的存檔
        Index("ix_sessions_last_active_at", "last_active_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # 不能是 `ON DELETE SET NULL`：刪除使用者時有兩條路徑到達 sessions（users → sessions 的串聯，與 users → scripts →
    # script_versions → sessions），SQL Server 不允許多重串聯路徑（錯誤 1785，專案的用量帳本也因此沒有外鍵）。
    # 所以是無動作的外鍵，由刪除版本與抹除帳號的流程先把參照清為空值；忘了清除時刪除會直接失敗，不會留下懸空的編號。
    source_version_id: Mapped[int | None] = mapped_column(ForeignKey("script_versions.id"), nullable=True)
    script_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)         # 開存檔當時的標準化劇本內容（含 spec_version）
    snapshot_spec_version: Mapped[int] = mapped_column(Integer, default=1)

    # 主角：劇中角色的姓名與年齡（與使用者的年齡無關，使用者年齡一律由生日計算）
    player_name: Mapped[str] = mapped_column(Unicode(50))
    character_age: Mapped[int] = mapped_column(Integer)
    # 主角的完整資料（rpg-protagonist）：`{"character": {name, personality, age, appearance, occupation, speech_style, hidden_trait, image},
    # "anchor": {kind, ref, text} 或 None}`。`player_name`、`character_age` 由角色卡於同一個 INSERT 寫入（expand 階段保留兩欄，
    # 舊程式與匯出照常運作）；遷移之前建立的存檔此欄為空。個資：不進日誌、不進管理員預設範圍；不得依內容篩選查詢。
    protagonist_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # 世界狀態：更新時以 `version` 做條件式更新（成功加 1），不一致以後端為準
    current_location: Mapped[str] = mapped_column(Unicode(100), default="")
    status_summary: Mapped[str] = mapped_column(UnicodeText, default="")
    entities_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    goals_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)               # {"active": [], "archived": []}
    milestone_progress_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    elapsed_months: Mapped[int] = mapped_column(Integer, default=0)           # 故事開始後經過的遊戲內月數
    version: Mapped[int] = mapped_column(Integer, default=1)

    # 記事簿（AGENTS.md 第 6 節）：這個 change 只建立欄位；行為屬回合引擎
    notebook_text: Mapped[str] = mapped_column(UnicodeText, default="")
    notebook_version: Mapped[int] = mapped_column(Integer, default=1)
    notebook_upto_turn: Mapped[int] = mapped_column(Integer, default=0)

    content_tier: Mapped[str] = mapped_column(Unicode(10), default=CONTENT_TIER_SAFE)   # 開存檔當下的生效模式，只能往保守方向調整
    status: Mapped[str] = mapped_column(Unicode(10), default=SESSION_ACTIVE)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)          # 玩家自行封存，與劇情完結是兩件事
    last_active_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MemoryLog(Base, TimestampMixin):
    __tablename__ = "memory_logs"
    __table_args__ = (
        UniqueConstraint("session_id", "client_msg_id", name="uq_memory_logs_session_client_msg"),
        UniqueConstraint("session_id", "seq", name="uq_memory_logs_session_seq"),
        Index("ix_memory_logs_session_id_turn_number", "session_id", "turn_number"),
        # 期限清除：中斷或被擋的回合超過 14 天，清除部分內容
        Index("ix_memory_logs_turn_status_updated_at", "turn_status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    client_msg_id: Mapped[str] = mapped_column(Unicode(64))        # 前端產生的唯一編號，同一存檔內唯一
    seq: Mapped[int] = mapped_column(Integer)                      # 後端指派、同一存檔內單調遞增
    turn_number: Mapped[int | None] = mapped_column(Integer, nullable=True)     # 輪數：只有 complete 的回合才有
    turn_status: Mapped[str] = mapped_column(Unicode(12), default=TURN_STREAMING)
    user_action: Mapped[str] = mapped_column(UnicodeText, default="")
    llm_raw_response: Mapped[str] = mapped_column(UnicodeText, default="")      # 中斷時保留已收到的片段；超過 14 天清為空字串
    structured_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
