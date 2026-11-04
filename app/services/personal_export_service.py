"""匯出個人資料（見 personal-data-export 規格與 privacy-lifecycle 的 design D8、D9）。

- JSON：UTF-8（無 BOM）、繁體中文保持原字元；逐段寫入暫存檔，不先組成一個大物件。
- CSV：一個 zip，內含 `account.csv`（帳號資料，一列）與 `messages.csv`（每則訊息一列，含所屬對話的識別碼與標題），皆為 UTF-8-sig，
  讓 Excel 開啟繁體中文不亂碼。儲存格以 `=`、`+`、`-`、`@`、Tab 或歸位字元開頭時，前面加單引號，避免被試算表當成公式執行
  （訊息內容是使用者與模型寫的，不可信任）。
- 訊息以序號為游標每頁 500 則讀取，寫入 `SpooledTemporaryFile`（超過 8 MB 才落地），所以記憶體用量與訊息總數無關。
- 範圍：帳號欄位、對話標題與摘要、訊息序號／角色／內容／時間，**自己送出的申訴**（內容、時間、處理狀態；CSV 為 `appeals.csv`），
  **自己創作的劇本**（全部版本，含草稿、待審、被退回與已被取代的；CSV 為 `scripts.csv`，每個版本一列）與**自己的存檔**（含記事簿全文與每個回合的
  玩家行動與劇情原文；CSV 為 `sessions.csv` 與 `turns.csv`；NPC 狀態、目標與里程碑進度等世界狀態只在 JSON）。存檔內的劇本複本不匯出，
  它是來源劇本作者的內容，存檔只帶該劇本的標題。劇本與存檔也以產生器逐筆、逐頁讀取，記憶體用量與總量無關。
  **自己的送審紀錄**（結果、被拒絕的類別、時間；CSV 為 `script_submissions.csv`）：紀錄本來就只有固定列舉與時間，沒有任何劇本文字；
  劇本區塊也**不含**審查結果的欄位（判定、類別、審查時間與內容雜湊）——那是系統的判斷而不是使用者提供的資料。
  **不含**密碼雜湊、`token_version`、驗證權杖、Google 的識別值、內部整數編號、種子鍵、內容雜湊、帳號鎖定的計次，以及意圖、實體、信心值、
  建議動作、對話狀態與回合的結構化輸出等模型內部欄位（不是使用者提供的資料，而是系統推論的副產品）。
"""
from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Callable, Iterator

from app.models.user import PROVIDER_GOOGLE, User
from app.observability import lifecycle_events
from app.repositories.chat_repository import ChatRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.session_repository import SessionRepository
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
    "email", "display_name", "nickname", "nickname_code", "member_tier", "email_verified_at", "created_at", "last_login_at", "terms_version",
    "terms_accepted_at", "birth_date", "age_range", "age_confirmed_at", "age_policy_version", "google_linked",
)
MESSAGE_COLUMNS = ("conversation_id", "conversation_title", "seq", "role", "content", "created_at")
APPEAL_COLUMNS = ("appeal_id", "content", "created_at", "status")
SUBMISSION_COLUMNS = ("outcome", "categories", "created_at")
SCRIPT_COLUMNS = (
    "script_id", "version_no", "status", "spec_version", "title", "summary", "world_name", "content_tier", "background", "opening",
    "rules", "tags", "calendar_name", "calendar_start_year", "ending_mode", "ending_text", "ending_age", "characters", "locations",
    "milestones", "author_name", "author_code", "created_at", "published_at",
)
SESSION_COLUMNS = (
    "session_id", "script_title", "status", "content_tier", "player_name", "character_age", "current_location", "status_summary",
    "elapsed_months", "created_at", "last_active_at", "ended_at", "notebook_text",
)
TURN_COLUMNS = ("session_id", "script_title", "seq", "turn_number", "status", "user_action", "story", "created_at")

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
        scripts: ScriptRepository,
        sessions: SessionRepository,
        submissions: ScriptSubmissionRepository,
        clock: Callable[[], datetime],
    ) -> None:
        self._scripts = scripts
        self._submissions = submissions
        self._sessions = sessions
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
            "nickname": user.nickname,
            "nickname_code": user.nickname_code,
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

    @staticmethod
    def _when(moment: datetime | None) -> str | None:
        return None if moment is None else iso_utc(moment)

    def _script_entry(self, script: dict[str, Any]) -> dict[str, Any]:
        """一個劇本（含全部版本）的匯出內容：時間轉成 ISO 8601。"""
        return {
            "id": script["id"], "author_name": script["author_name"], "author_code": script["author_code"],
            "author_locked_at": self._when(script["author_locked_at"]), "created_at": self._when(script["created_at"]),
            "versions": [
                {**version, "created_at": self._when(version["created_at"]), "published_at": self._when(version["published_at"])}
                for version in script["versions"]
            ],
        }

    def _submission_rows(self, user_id: int) -> Iterator[dict[str, Any]]:
        """送審紀錄：結果、類別（固定列舉）與時間。"""
        for row in self._submissions.export_for_user(user_id):
            yield {"outcome": row["outcome"], "categories": row["categories"], "created_at": self._when(row["created_at"])}

    def _session_head(self, save: dict[str, Any], *, world_state: bool) -> dict[str, Any]:
        head = {key: value for key, value in save.items() if key != "world_state"}
        for key in ("created_at", "last_active_at", "ended_at"):
            head[key] = self._when(head[key])
        if world_state:
            head["world_state"] = save["world_state"]
        return head

    def _turns(self, user_id: int, session_public_id: str):
        for turn in self._sessions.export_turns(user_id, session_public_id):
            yield {**turn, "created_at": self._when(turn["created_at"]), "updated_at": self._when(turn["updated_at"])}

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
        put(',"scripts":[')
        for index, script in enumerate(self._scripts.export_scripts(user.id)):
            put(("," if index else "") + dump(self._script_entry(script)))
        put(']')
        put(',"sessions":[')
        for index, save in enumerate(self._sessions.export_sessions(user.id)):
            put(("," if index else "") + dump(self._session_head(save, world_state=True))[:-1] + ',"turns":[')
            for position, turn in enumerate(self._turns(user.id, save["id"])):
                put(("," if position else "") + dump(turn))
            put("]}")
        put(']')
        put(',"appeals":' + dump(self._appeal_rows(user.id)))
        put(',"script_submissions":[')
        for index, row in enumerate(self._submission_rows(user.id)):
            put(("," if index else "") + dump(row))
        put(']')
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
            with archive.open("script_submissions.csv", "w") as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                    writer = csv.writer(text)
                    writer.writerow(SUBMISSION_COLUMNS)
                    for row in self._submission_rows(user.id):
                        writer.writerow([row["outcome"], csv_safe("、".join(row["categories"])), row["created_at"]])
            with archive.open("scripts.csv", "w") as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                    writer = csv.writer(text)
                    writer.writerow(SCRIPT_COLUMNS)
                    for script in self._scripts.export_scripts(user.id):
                        entry = self._script_entry(script)
                        for version in entry["versions"]:
                            calendar = version["calendar"] or {}
                            cells = {
                                "script_id": entry["id"], "version_no": version["version_no"], "status": version["status"],
                                "spec_version": version["spec_version"], "title": version["title"], "summary": version["summary"],
                                "world_name": version["world_name"], "content_tier": version["content_tier"],
                                "background": version["background"], "opening": version["opening"], "rules": version["rules"],
                                "tags": "、".join(version["tags"]), "calendar_name": calendar.get("name"),
                                "calendar_start_year": calendar.get("start_year"), "ending_mode": version["ending_mode"],
                                "ending_text": version["ending_text"], "ending_age": version["ending_age"],
                                "characters": json.dumps(version["characters"], ensure_ascii=False),
                                "locations": json.dumps(version["locations"], ensure_ascii=False),
                                "milestones": json.dumps(version["milestones"], ensure_ascii=False),
                                "author_name": entry["author_name"], "author_code": entry["author_code"],
                                "created_at": version["created_at"], "published_at": version["published_at"],
                            }
                            writer.writerow([csv_safe(cells[name]) for name in SCRIPT_COLUMNS])
            with archive.open("sessions.csv", "w") as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                    writer = csv.writer(text)
                    writer.writerow(SESSION_COLUMNS)
                    for save in self._sessions.export_sessions(user.id):
                        head = self._session_head(save, world_state=False)
                        writer.writerow([csv_safe({**head, "session_id": head["id"]}[name]) for name in SESSION_COLUMNS])
            with archive.open("turns.csv", "w") as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                    writer = csv.writer(text)
                    writer.writerow(TURN_COLUMNS)
                    for save in self._sessions.export_sessions(user.id):
                        for turn in self._turns(user.id, save["id"]):
                            cells = {**turn, "session_id": save["id"], "script_title": save["script_title"]}
                            writer.writerow([csv_safe(cells[name]) for name in TURN_COLUMNS])
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
