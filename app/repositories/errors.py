"""Repository 層的資料錯誤：把資料庫的完整性錯誤轉成 Service 可以處理的類型。

Service 不需要知道 SQLAlchemy 或各資料庫的錯誤長什麼樣子，只處理「這筆資料重複了」。
"""


class DuplicateUserError(Exception):
    """email 或其重複判斷用的正規形式已有帳號（包含並行註冊時被唯一索引擋下的情況）。"""


class DuplicateIdentityError(Exception):
    """這個第三方身分已綁定到某個帳號，或這個帳號已綁定同一供應商的另一個身分。"""
