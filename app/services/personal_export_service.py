"""匯出個人資料（見 personal-data-export 規格與 privacy-lifecycle 的 design D8、D9）。

- JSON：UTF-8（無 BOM）、繁體中文保持原字元；逐段寫入暫存檔，不先組成一個大物件。
- CSV：一個 zip，內含 `account.csv`（帳號資料，一列）與 `messages.csv`（每則訊息一列，含所屬對話的識別碼與標題），皆為 UTF-8-sig，
  讓 Excel 開啟繁體中文不亂碼。儲存格以 `=`、`+`、`-`、`@`、Tab 或歸位字元開頭時，前面加單引號，避免被試算表當成公式執行
  （訊息內容是使用者與模型寫的，不可信任）。
- 訊息以序號為游標每頁 500 則讀取，寫入 `SpooledTemporaryFile`（超過 8 MB 才落地），所以記憶體用量與訊息總數無關。
- 範圍：帳號欄位、對話標題與摘要、訊息序號／角色／內容／時間，以及**自己送出的申訴**（內容、時間、處理狀態；CSV 為 `appeals.csv`）。**不含**密碼雜湊、`token_version`、驗證權杖、Google 的識別值、
  內部整數編號、帳號鎖定的計次，以及意圖、實體、信心值、建議動作與對話狀態等模型內部欄位（不是使用者提供的資料，而是系統推論的副產品）。
"""
from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Callable

from app.models.user import PROVIDER_GOOGLE, User
from app.observability import lifecycle_events
from app.repositories.chat_repository import ChatRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.age_policy import taipei_today
from app.services.current_user import CurrentUser, iso_utc
from app.services.errors import Unauthenticated, ValidationError

FORMAT_JSON = "json"
FORMAT_CSV = "csv"
FORMATS = (FORMAT_JSON, FORMAT_CSV)
EXPORT_VERSION = 1
PAGE_SIZE = 500
SPOOL_LIMIT_BYTES = 8 * 1024 * 1024

ACCOUNT_COLUMNS = (
    "email", "display_name", "member_tier", "email_verified_at", "created_at", "last_login_at", "terms_version",
    "terms_accepted_at", "birth_date", "age_range", "age_confirmed_at", "age_policy_version", "google_linked",
)
MESSAGE_COLUMNS = ("conversation_id", "conversation_title", "seq", "role", "content", "created_at")
APPEAL_COLUMNS = ("appeal_id", "content", "created_at", "status")

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> Any:
    """字串以公式字元開頭時前面加單引號（OWASP 的 CSV 注入防護）；其他型別原樣交給 csv 模組。"""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


@dataclass
class ExportFile:
    """要回傳的檔案。`handle` 是已回到開頭的暫存檔，呼叫端在回應結束時要關閉它。"""

    handle: BinaryIO
    filename: str
    mimetype: str


class PersonalExportService:
    def __init__(
        self,
        *,
        users: UserRepository,
        identities: UserIdentityRepository,
        chats: ChatRepository,
        appeals: LockAppealRepository,
        clock: Callable[[], datetime],
    ) -> None:
        self._appeals = appeals
        self._users = users
        self._identities = identities
        self._chats = chats
        self._clock = clock

    def build(self, current: CurrentUser, fmt: object) -> ExportFile:
        if fmt not in FORMATS:
            raise ValidationError(fields={"format": "格式必須是 json 或 csv。"})
        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        now = self._clock()
        stamp = taipei_today(now).strftime("%Y%m%d")
        handle = tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT_BYTES)       # noqa: SIM115 - 由呼叫端在回應結束時關閉
        try:
            account = self._account(user)
            if fmt == FORMAT_JSON:
                self._write_json(handle, user, account, now)
                result = ExportFile(handle, f"my-data-{stamp}.json", "application/json; charset=utf-8")
            else:
                self._write_csv_zip(handle, user, account)
                result = ExportFile(handle, f"my-data-{stamp}.zip", "application/zip")
        except BaseException:
            handle.close()
            raise
        handle.seek(0)
        lifecycle_events.log_exported(user_id=user.id, fmt=fmt)
        return result

    # ---- 內容 --------------------------------------------------------------------

    def _account(self, user: User) -> dict[str, Any]:
        def when(moment: datetime | None) -> str | None:
            return None if moment is None else iso_utc(moment)

        return {
            "email": user.email,
            "display_name": user.display_name,
            "member_tier": user.member_tier,
            "email_verified_at": when(user.email_verified_at),
            "created_at": when(user.created_at),
            "last_login_at": when(user.last_login_at),
            "terms_version": user.terms_version,
            "terms_accepted_at": when(user.terms_accepted_at),
            "birth_date": None if user.birth_date is None else user.birth_date.isoformat(),
            "age_range": user.age_range,
            "age_confirmed_at": when(user.age_confirmed_at),
            "age_policy_version": user.age_policy_version,
            "google_linked": self._identities.get_for_user(user.id, PROVIDER_GOOGLE) is not None,
        }

    def _appeal_rows(self, user_id: int) -> list[dict[str, Any]]:
        """自己送出的申訴（由舊到新）；內容是使用者撰寫的個資，匯出時原樣交還給本人。"""
        return [
            {
                "id": appeal.public_id, "content": appeal.content, "created_at": iso_utc(appeal.created_at),
                "status": "handled" if appeal.handled_at is not None else "pending",
            }
            for appeal in self._appeals.list_for_user(user_id)
        ]

    def _messages(self, user_id: int, conversation):
        """依序號由小到大逐頁讀取一個對話的全部訊息。"""
        after = 0
        while True:
            page, more = self._chats.messages_after(user_id, conversation, after_seq=after, limit=PAGE_SIZE)
            for message in page:
                yield {
                    "seq": message.seq,
                    "role": message.role,
                    "content": message.content,
                    "created_at": iso_utc(message.created_at),
                }
            if not more:
                return
            after = page[-1].seq

    # ---- JSON --------------------------------------------------------------------

    def _write_json(self, handle: BinaryIO, user: User, account: dict[str, Any], now: datetime) -> None:
        def put(text: str) -> None:
            handle.write(text.encode("utf-8"))

        def dump(value: Any) -> str:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

        put('{"export_version":' + str(EXPORT_VERSION) + ',"exported_at":' + dump(iso_utc(now)))
        put(',"account":' + dump(account) + ',"conversations":[')
        for index, conversation in enumerate(self._chats.export_conversations(user.id)):
            head = {
                "id": conversation.public_id,
                "title": conversation.title,
                "summary": conversation.summary,
                "created_at": iso_utc(conversation.created_at),
                "updated_at": iso_utc(conversation.updated_at),
            }
            put(("," if index else "") + dump(head)[:-1] + ',"messages":[')
            for position, message in enumerate(self._messages(user.id, conversation)):
                put(("," if position else "") + dump(message))
            put("]}")
        put(']')
        put(',"appeals":' + dump(self._appeal_rows(user.id)))
        put('}')

    # ---- CSV（zip）------------------------------------------------------------------

    def _write_csv_zip(self, handle: BinaryIO, user: User, account: dict[str, Any]) -> None:
        with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            with archive.open("account.csv", "w") as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                    writer = csv.writer(text)
                    writer.writerow(ACCOUNT_COLUMNS)
                    writer.writerow([csv_safe(account[name]) for name in ACCOUNT_COLUMNS])
            with archive.open("appeals.csv", "w") as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                    writer = csv.writer(text)
                    writer.writerow(APPEAL_COLUMNS)
                    for row in self._appeal_rows(user.id):
                        writer.writerow([row["id"], csv_safe(row["content"]), row["created_at"], row["status"]])
            with archive.open("messages.csv", "w") as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                    writer = csv.writer(text)
                    writer.writerow(MESSAGE_COLUMNS)
                    for conversation in self._chats.export_conversations(user.id):
                        for message in self._messages(user.id, conversation):
                            writer.writerow([
                                conversation.public_id, csv_safe(conversation.title), message["seq"], message["role"],
                                csv_safe(message["content"]), message["created_at"],
                            ])
