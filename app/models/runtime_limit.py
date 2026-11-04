"""存放在資料庫的營運限制（見 runtime-limits 規格與 rpg-script-authoring 的 design D10）。

每個鍵一列：劇本數量上限、每日送審次數上限、連續被拒的門檻。程式**每次使用時現讀**，不在行程內快取（AGENTS.md 第 9 節：
多 worker 不得依賴行程記憶體）；鍵的集合、合法範圍與缺值時的保底預設在 `app/services/runtime_limits.py`，不在資料庫約束，
這樣資料被誤改時讀取端仍有保守的後備值。只能由部署者以 flask 指令修改，並寫入稽核紀錄。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, Unicode
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow


class RuntimeLimit(Base):
    __tablename__ = "runtime_limits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(Unicode(60), unique=True)
    int_value: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
