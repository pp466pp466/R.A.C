"""Repository 層的資料錯誤：把資料庫的完整性錯誤轉成 Service 可以處理的類型。

Service 不需要知道 SQLAlchemy 或各資料庫的錯誤長什麼樣子，只處理「這筆資料重複了」。
"""


class DuplicateUserError(Exception):
    """email 或其重複判斷用的正規形式已有帳號（包含並行註冊時被唯一索引擋下的情況）。"""


class DuplicateIdentityError(Exception):
    """這個第三方身分已綁定到某個帳號，或這個帳號已綁定同一供應商的另一個身分。"""


class DuplicateConversationError(Exception):
    """同一位使用者以相同的前端識別碼建立對話時，被唯一約束擋下（並行重送）。呼叫端須先回復交易，再讀取既有的對話。"""


class DuplicateMessageError(Exception):
    """同一對話已有相同序號或相同訊息識別碼的訊息（唯一約束）。

    序號：處理標記過期被接手的邊界上，兩個請求同時附加；識別碼：兩個 worker 同時寫入同一則助理回覆。
    發生後 Session 處於失敗狀態，呼叫端須先回復交易，再重新讀取。
    """
