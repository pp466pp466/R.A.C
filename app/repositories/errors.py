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


class OpenVersionExistsError(Exception):
    """這個劇本已經有進行中的版本（`draft` 或 `pending_review`）：每個劇本同一時間至多一個，由資料庫的部分唯一索引保證，
    所以並行的兩個請求也只有一個建立得成功。發生後沒有留下任何半套資料。"""


class InvalidVersionTransitionError(Exception):
    """版本的狀態不允許這個轉移（只允許 `draft`→`pending_review`、`pending_review`→`draft`、`pending_review`→`published`
    或 `rejected`、`published`→`superseded`）。版本保持原狀態。"""


class DraftChangedError(Exception):
    """草稿在這次編輯開始之後已被更新（條件式更新沒有命中，而版本仍是草稿）：不覆寫，Service 轉成 `DraftChanged`（409）。"""


class VersionNotDraftError(Exception):
    """只有 `draft` 版本的內容可以修改或刪除；離開草稿之後內容不可變，要修改須建立新的版本。內容保持原樣。"""


class AuthorNameLockedError(Exception):
    """筆名已在劇本第一次發佈時鎖定，不可再更改。"""


class DuplicateSeedKeyError(Exception):
    """已有使用這個種子鍵的劇本（唯一約束）。"""


class SessionEndedError(Exception):
    """存檔已完結：歷史仍可讀，但不得再開始回合或改動世界狀態（資料不變）。Service 轉成 `SessionEnded`（409，`session_ended`）。"""


class StaleSessionVersionError(Exception):
    """存檔世界狀態的版本號與後端不一致（以後端為準）。Service 轉成既有的版本衝突並附上最新的狀態。"""


class StaleNotebookVersionError(Exception):
    """記事簿的版本號與後端不一致（玩家儲存時，AI 已追加或另一個裝置已儲存；資料不變，以後端為準）。
    Service 轉成 `NotebookConflict`（409，`notebook_conflict`）並附上後端最新的文字與版本。"""


class TurnInProgressError(Exception):
    """同一個 `client_msg_id` 的回合仍在串流中：不可重複開始（衝突）。"""


class InvalidTurnTransitionError(Exception):
    """回合的狀態不允許這個轉移（只有 `streaming` 的回合可以完成、中斷或被擋下）。回合保持原狀態。
    修訂的替換（`replace_turn`）也用它：回合不是完成的、`revision` 或 `attempt` 不符、或認領已被釋放或接手（過時的執行者）。"""


class NotRevisableError(Exception):
    """這個回合現在不能被重新生成或編輯：`reason` 為 `ended`、`not_last`、`in_progress` 或 `no_snapshot`
    （rpg-turn-revision 的 design D1）。Service 轉成 409 `not_revisable` 並附上原因；資料不變。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
