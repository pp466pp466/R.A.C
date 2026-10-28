"""認證與安全事件的記錄（見 AGENTS.md 第 11 節與 service-observability 規格）。

事件名稱是**固定列舉**，失敗原因寫在名稱的最後一段（例如 `auth.login_failed.bad_password`），
因此日誌裡不可能夾帶自由文字，也就不會夾帶 email、密碼、權杖、授權碼或 Cookie。
這些名稱是「登入失敗激增」等告警的資料來源。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("app.security")

# ---- 註冊與登入 ----
REGISTER = "auth.register"
REGISTER_EXISTING = "auth.register_existing"
LOGIN_SUCCESS = "auth.login_success"
LOGIN_FAILED_UNKNOWN_USER = "auth.login_failed.unknown_user"
LOGIN_FAILED_BAD_PASSWORD = "auth.login_failed.bad_password"
LOGIN_FAILED_NO_PASSWORD = "auth.login_failed.no_password"
LOGIN_FAILED_LOCKED = "auth.login_failed.locked"
LOGIN_REFUSED_DISABLED = "auth.login_refused.disabled"
ACCOUNT_LOCKED = "auth.locked"
LOGOUT = "auth.logout"
LOGOUT_ALL = "auth.logout_all"
PASSWORD_CHANGED = "auth.password_changed"
PASSWORD_CHANGE_REFUSED = "auth.password_change_refused"
ADMIN_CREATED = "auth.admin_created"

# ---- 信箱驗證與重設密碼 ----
VERIFICATION_SENT = "auth.verification_sent"
VERIFICATION_RESEND_LIMITED = "auth.verification_resend_limited"
EMAIL_VERIFIED = "auth.email_verified"
EMAIL_TOKEN_REJECTED = "auth.email_token_rejected"
PASSWORD_RESET_REQUESTED = "auth.password_reset_requested"
PASSWORD_RESET_LIMITED = "auth.password_reset_limited"
PASSWORD_RESET = "auth.password_reset"

# ---- Google 登入 ----
GOOGLE_STARTED = "auth.google_started"
GOOGLE_LOGIN = "auth.google_login"
GOOGLE_REGISTERED = "auth.google_registered"
GOOGLE_LINKED = "auth.google_linked"
GOOGLE_LINKED_UNVERIFIED = "auth.google_linked_unverified"
GOOGLE_REJECTED_STATE = "auth.google_rejected.state"
GOOGLE_REJECTED_TOKEN = "auth.google_rejected.token"
GOOGLE_REJECTED_UNAVAILABLE = "auth.google_rejected.unavailable"
GOOGLE_REJECTED_TERMS = "auth.google_rejected.terms"
GOOGLE_REJECTED_DISABLED = "auth.google_rejected.disabled"
GOOGLE_REJECTED_CONFLICT = "auth.google_rejected.conflict"

# ---- 其他安全事件 ----
CSRF_REJECTED = "security.csrf_rejected"
MAIL_SEND_FAILED = "mail.send_failed"

EVENT_NAMES = frozenset(
    value for name, value in dict(globals()).items() if name.isupper() and isinstance(value, str) and "." in value
)

# 這些字樣代表「出了問題」，以警告層級記錄；其餘為一般資訊
_WARNING_MARKERS = ("failed", "refused", "rejected", "locked", "limited", "csrf")


def log_event(event: str, *, user_id: int | None = None, request_id: str | None = None) -> None:
    """記錄一筆安全事件。`user_id` 是內部整數編號（已知時才帶）；在請求之外（例如延後動作）可明確帶 `request_id`。"""
    if event not in EVENT_NAMES:
        raise ValueError(f"未登記的安全事件名稱：{event}")
    level = logging.WARNING if any(marker in event for marker in _WARNING_MARKERS) else logging.INFO
    logger.log(level, "安全事件", extra={"event": event, "user_id": user_id, "request_id": request_id})
