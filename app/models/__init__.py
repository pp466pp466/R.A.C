"""資料模型與共用欄位慣例。

在這裡匯入全部模型，讓 `Base.metadata` 一定包含所有資料表（Alembic 與測試都依賴它）。
"""
from app.models.base import Base
from app.models.user import AuthToken, User, UserIdentity

__all__ = ["Base", "User", "UserIdentity", "AuthToken"]
