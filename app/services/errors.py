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


class RateLimited(TooManyRequests):
    """最近 60 秒內送出的訊息已達等級的每分鐘上限（429）。`retry_after` 是最早可再送出的秒數。"""

    code = "rate_limited"
    default_message = "您傳送訊息的速度太快了，請稍候幾秒再試。"

    def __init__(self, retry_after: int = 1, message: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class DailyQuotaExceeded(TooManyRequests):
    """今日的對話輪數已達等級的上限（429）。`retry_after` 是距離明日 00:00（台灣時間）的秒數。"""

    code = "daily_quota_exceeded"
    default_message = "您今日的對話額度已用完，將於明日 00:00（台灣時間）重置。"

    def __init__(self, retry_after: int = 1, message: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class AdminTargetForbidden(Forbidden):
    """後台的停權、解除與調整等級只適用於一般使用者；對管理員帳號（含管理員自己）的操作一律拒絕（403）。"""

    code = "admin_target_forbidden"
    default_message = "不能對管理員帳號執行這個操作。"


class ConversationLimitReached(Conflict):
    """對話數已達等級的上限，須先刪除不需要的對話（409）。"""

    code = "conversation_limit_reached"
    default_message = "對話數已達上限，請先刪除不需要的對話。"


class ChatSuspended(Forbidden):
    """客服對話因反覆被內容安全過濾阻擋而暫停（403）。`until` 是恢復的時間（帶時區）；訊息以台灣時間說明。

    不是「閘門」代碼：補填年齡或換帳號都沒有幫助，只能等到期，所以前端不引導去帳號設定。
    """

    code = "chat_suspended"
    default_message = "由於多次觸發內容安全限制，客服對話已暫停一段時間，請稍後再試。"

    def __init__(self, until=None, message: str | None = None) -> None:
        super().__init__(message)
        self.until = until


class ServiceUnavailable(ServiceError):
    """服務暫時無法使用（503）：例如沒有設定模型供應商（本機開發沒有金鑰）。發生時不得寫入任何資料。"""

    code = "service_unavailable"
    default_message = "服務暫時無法使用，請稍後再試。"


class SystemBusy(ServiceUnavailable):
    """全站今日的用量上限已到（503）。文案不透露上限或用量的數字；`retry_after` 是建議的重試間隔（秒）。"""

    code = "system_busy"
    default_message = "目前使用人數較多，請稍後再試。"
    retry_after = 60
