"""Service 層的業務錯誤（見 AGENTS.md 第 3、10 節）。

Service 用這些類型表達「這個請求為什麼不能完成」，不知道 HTTP；端點統一把它們對應成狀態碼與繁體中文回應
（controllers/errors.py）。錯誤訊息是給使用者看的，絕不放堆疊追蹤、SQL、路徑或機密。
"""
from __future__ import annotations


class ServiceError(Exception):
    """業務錯誤基底。`code` 是機器可讀的代碼，`message` 是顯示給使用者的繁體中文說明。"""

    code = "error"
    default_message = "發生錯誤，請稍後再試。"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.message = message or self.default_message
        if code is not None:
            self.code = code
        super().__init__(self.message)


class ValidationError(ServiceError):
    """輸入不合法（400）。`fields` 以欄位名稱對應該欄位的錯誤說明。"""

    code = "validation_error"
    default_message = "輸入的內容不正確，請檢查後再試。"

    def __init__(self, message: str | None = None, *, fields: dict[str, str] | None = None, code: str | None = None) -> None:
        super().__init__(message, code=code)
        self.fields = dict(fields or {})


class Unauthenticated(ServiceError):
    """尚未登入，或登入資訊不正確（401）。"""

    code = "unauthorized"
    default_message = "請先登入。"


class Forbidden(ServiceError):
    """已登入但目前的狀態不允許這個操作（403）。非擁有者存取資源不用這個，要用 `NotFound`。"""

    code = "forbidden"
    default_message = "沒有權限執行這個操作。"


class AgeConfirmationRequired(Forbidden):
    """尚未完成年齡確認，不得使用會呼叫 Gemini 的功能（403）。前端依 `code` 引導使用者到帳號設定補填生日與年齡區間。"""

    code = "age_confirmation_required"
    default_message = "請先到帳號設定填寫生日並選擇年齡區間，才能使用這項功能。"


class AgeRestricted(Forbidden):
    """年齡低於目前年齡規則的最低使用年齡（403）。與「尚未完成確認」不同：補填也沒有用，所以不引導補填。"""

    code = "age_restricted"
    default_message = "您的年齡目前不符合使用這項功能的規定。"


class NotFound(ServiceError):
    """資源不存在，或呼叫者無權得知它存在（404）。非擁有者與不存在必須得到完全相同的回應。"""

    code = "not_found"
    default_message = "找不到您要的內容。"


class Conflict(ServiceError):
    """與目前的狀態衝突（409）。"""

    code = "conflict"
    default_message = "目前無法處理，請稍後再試。"


class ConversationBusy(Conflict):
    """同一個對話已有訊息正在處理（409）。同一對話同時只處理一則；處理標記過期（60 秒）後才可被接手。"""

    code = "conversation_busy"
    default_message = "這個對話正在處理上一則訊息，請稍候再送出。"


class MessageIdReused(Conflict):
    """同一個訊息識別碼帶著不同的內容（409）：識別碼是冪等的依據，不能用在兩段不同的文字上。"""

    code = "message_id_reused"
    default_message = "這則訊息的識別碼已用於不同的內容，請重新整理後再送出。"


class VersionConflict(Conflict):
    """版本號與後端不一致（409）：以後端為準，並附上最新的對話資訊（`conversation`）讓前端更新。"""

    code = "version_conflict"
    default_message = "這個對話已在其他裝置被修改，已為您更新到最新的內容。"

    def __init__(self, conversation: dict, message: str | None = None) -> None:
        super().__init__(message)
        self.conversation = conversation


class TooManyRequests(ServiceError):
    """操作過於頻繁（429）。"""

    code = "too_many_requests"
    default_message = "操作太頻繁，請稍後再試。"


class ServiceUnavailable(ServiceError):
    """服務暫時無法使用（503）：例如沒有設定模型供應商（本機開發沒有金鑰）。發生時不得寫入任何資料。"""

    code = "service_unavailable"
    default_message = "服務暫時無法使用，請稍後再試。"
