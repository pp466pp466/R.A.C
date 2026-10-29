"""組裝層共用的執行期取得函式：讓 Service 的組裝處（services/registry.py）取得設定、時鐘與外部服務。

Service 本身不匯入 flask；外部依賴（寄信器、身分供應商、時鐘）在 `create_app()` 建立或由測試注入，
放在 `app.extensions`，這裡只負責取出。這些物件都是無狀態的用戶端，不保存使用者或流程狀態，
因此多個 worker 各自持有一份不會造成行為分歧。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from flask import current_app, g, has_request_context

from app.config import AppConfig
from app.db.session import defer_until_commit, keep_changes_on_error  # noqa: F401 - 讓 Service 經由 runtime 取得，而不必匯入資料庫層
from app.services.age_policy import AgePolicy

Clock = Callable[[], datetime]


def get_config() -> AppConfig:
    return current_app.extensions["app_config"]


def get_clock() -> Clock:
    """目前時間（帶 UTC 時區）。測試可注入假時鐘，以便不必真的等待就能驗證到期與鎖定。"""
    return current_app.extensions["clock"]


def get_age_policy() -> AgePolicy:
    """年齡規則（最低年齡、成年門檻、區間選項、版本）。測試可在 `create_app` 注入不同的規則。"""
    return current_app.extensions["age_policy"]


def current_request_id() -> str | None:
    """目前請求的識別碼（不在請求中則為 None）。延後動作要在登記時就取好，因為執行時請求已經結束。"""
    return g.get("request_id") if has_request_context() else None


def get_mail_sender() -> Any | None:
    """寄信器；信箱驗證關閉且沒有注入時為 None（此時不寄任何信）。"""
    return current_app.extensions.get("mail_sender")


def get_identity_provider() -> Any | None:
    """Google 身分供應商；Google 登入未啟用且沒有注入時為 None。"""
    return current_app.extensions.get("identity_provider")
