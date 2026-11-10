"""RPG 記事簿的玩家檢視與儲存（見 rpg-notebook 規格與 design D6）。

只處理**呼叫者本人**的存檔：非擁有者與不存在同為 404。這裡不呼叫模型，也不碰已處理到的輪次（`notebook_upto_turn`）；AI 的抓取與追加由
`NotebookCapturer` 負責，兩者靠 `notebook_version`（任一方寫入都加 1）偵測對方的寫入。

`save` 的檢查順序（便宜、不洩漏細節的在前；每個被拒的出口都只記一筆 `rpg_events` 的整數與列舉事件）：

1. 帳號與年齡閘門（`AccessPolicy.require_chat_access`）；
2. 擁有者（404）；
3. 清理（NFC、統一換行、去除控制字元與頭尾空白；無法安全儲存的輸入回 400）與版本號的格式；
4. 存檔已完結 → 409 `session_ended`（已完結的記事簿不可變）；
5. 長度大於容量 → 422 `notebook_too_long`（玩家編輯沒有預備空間；已滿的記事簿原樣再儲存同樣被擋，須一次刪到容量以內）；
6. 規則層（`script_rules.check_text_rules`，不呼叫模型）：**只有必須拒絕的類別**才擋（422 `notebook_blocked`，只帶類別、不回顯內容）；
   「分級不符」與有疑慮者放行——記事簿是玩家自己的設定，輸出層才依模式處理，系統不得自動調高玩家的模式；
7. 條件式寫入（進行中且版本相符）；不成立時重新讀取：已完結 → `session_ended`，否則 409 `notebook_conflict` 並附後端最新的記事簿。

記事簿全文是個資：日誌只記內部編號與整數長度，絕不記錄內容；全文只走 `view`／`save` 的回傳值給擁有者。
"""
from __future__ import annotations

from typing import Any

from app.models.rpg_session import SESSION_ENDED
from app.models.script import REVIEW_REJECT
from app.observability import rpg_events
from app.repositories.errors import SessionEndedError, StaleNotebookVersionError
from app.repositories.session_repository import SessionRepository
from app.services import notebook_policy
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import NotebookBlocked, NotebookConflict, NotebookTooLong, NotFound, SessionEnded, ValidationError
from app.services.script_rules import check_text_rules

INVALID_TEXT_MESSAGE = "記事簿的內容必須是文字。"
INVALID_VERSION_MESSAGE = "版本必須是不小於 0 的整數。"


def clean_expected_version(value: object) -> int:
    """玩家帶來的記事簿版本：整數（布林不算）且不小於 0；其餘回 400。是否與後端一致由條件式寫入判斷。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(fields={"expected_version": INVALID_VERSION_MESSAGE})
    return value


class NotebookService:
    def __init__(self, *, sessions: SessionRepository, policy: AccessPolicy) -> None:
        self._sessions = sessions
        self._policy = policy

    def view(self, current: CurrentUser, session_public_id: str) -> dict[str, Any]:
        """記事簿全文、版本、字數、容量、預備空間與是否已滿。已完結的存檔也可以讀。"""
        self._policy.require_chat_access(current)
        save = self._sessions.get_owned(current.id, session_public_id)
        if save is None:
            raise NotFound()
        return notebook_policy.view_of(save.notebook_text, save.notebook_version, current.member_tier)

    def save(self, current: CurrentUser, session_public_id: str, *, text: object, expected_version: object) -> dict[str, Any]:
        """玩家儲存編輯後的記事簿（見模組說明的順序）。成功回傳新的檢視（版本加 1）；被拒時資料都不變。"""
        self._policy.require_chat_access(current)
        save = self._sessions.get_owned(current.id, session_public_id)
        if save is None:
            raise NotFound()

        cleaned = notebook_policy.clean_notebook_text(text)
        if cleaned is None:
            raise ValidationError(fields={"text": INVALID_TEXT_MESSAGE})
        version = clean_expected_version(expected_version)
        length = notebook_policy.length_of(cleaned)

        if save.status == SESSION_ENDED:
            self._reject(current, save.id, "ended", length)
            raise SessionEnded()

        capacity = notebook_policy.capacity_for(current.member_tier)
        if length > capacity:
            self._reject(current, save.id, "too_long", length)
            raise NotebookTooLong(length, capacity)

        rules = check_text_rules([cleaned], tier=save.content_tier, adult_age=self._policy.adult_age)
        if rules.verdict == REVIEW_REJECT:
            self._reject(current, save.id, "blocked", length, categories=rules.categories)
            raise NotebookBlocked(rules.categories)

        try:
            saved = self._sessions.save_notebook(current.id, session_public_id, text=cleaned, expected_version=version)
        except SessionEndedError:                                    # 檢查之後、寫入之前存檔剛好完結
            self._reject(current, save.id, "ended", length)
            raise SessionEnded() from None
        except StaleNotebookVersionError:                            # AI 追加了內容，或另一個裝置已儲存：以後端為準並附最新內容
            latest = self._sessions.get_owned(current.id, session_public_id)
            if latest is None:
                raise NotFound() from None
            self._reject(current, save.id, "conflict", length)
            raise NotebookConflict(notebook_policy.view_of(latest.notebook_text, latest.notebook_version, current.member_tier)) from None
        if saved is None:                                            # 檢查之後存檔被刪除
            raise NotFound()
        rpg_events.log_notebook_saved(user_id=current.id, session_id=saved.id, version=saved.notebook_version, total=length)
        return notebook_policy.view_of(saved.notebook_text, saved.notebook_version, current.member_tier)

    @staticmethod
    def _reject(current: CurrentUser, session_id: int, reason: str, length: int, *, categories: tuple[str, ...] = ()) -> None:
        rpg_events.log_notebook_save_rejected(user_id=current.id, session_id=session_id, reason=reason, total=length, categories=categories)
