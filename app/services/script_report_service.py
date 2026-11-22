"""玩家檢舉已上架的劇本版本（見 script-reports 規格與 rpg-script-reports 的 design D3）。

`POST /api/sessions/catalog/<版本識別碼>/report` 依序檢查，並在第一個不通過處停止（後面的步驟都不執行，不寫任何資料）：
1. 帳號與年齡閘門（`AccessPolicy.require_can_report`：帳號可用且已完成年齡確認，不論角色與會員等級；受限登入狀態更早就被擋下）；
2. 目標版本必須是**上架中**且**不是內建劇本**（種子鍵非空）：非上架（草稿、待審、被退回、被取代、已下架）、不存在、格式不對與內建劇本一律是
   相同的 `NotFound`（404，判斷內建劇本依種子鍵，不是「沒有作者」——匿名保留的玩家劇本同樣沒有作者，仍可被檢舉）；
3. 不是自己的劇本（`ReportOwnScript`，403）——作者看得到自己的劇本，所以不用 404；
4. 本文驗證（`reasons`、`text`，一次回報全部不合法的欄位，400）——**晚於** 404 與 403，所以無效的識別碼不會洩漏欄位規則；
5. 沒有檢舉過同一個版本（`AlreadyReported`，409；不論先前的檢舉是否已結案）——**早於**每日上限，所以重複的請求不消耗額度；
6. 今日（台灣日）的檢舉筆數未達營運限制 `script_report_daily`（`ReportLimit`，429，附 `retry_after`；每次從資料庫現讀，0 表示暫停接受）；
7. 寫入並記錄事件。寫入時以資料庫的唯一約束 `(檢舉人, 版本)` 為準：兩個並行的相同請求只有一個成功，另一個得到 409。

- **每日上限是軟上限**：兩個並行請求都在計數時看到 4 筆就都會寫入，當日可能是 6 筆；不加鎖（檢舉是低頻操作，鎖會把玩家的請求串起來），
  每版本一次與每日上限兩層仍使洗版面積有限。
- **不呼叫模型**：檢舉的理由與自寫文字不送入任何模型、不進 `system_instruction`；日誌只有內部使用者編號與整數（見 `script_events`），沒有文字。
- 檢舉不改變版本的狀態、不通知任何人；回應只有成功標記，沒有任何統計。
- 自寫文字視為個資（AGENTS.md 第 11 節）：NFC 正規化、去除控制字元與頭尾空白，至多 200 個 Unicode 字元，只被當成純文字保存與顯示。
- `red_line` 由理由算出（有勾選三項紅線理由任一項）並在建立時固定，管理員佇列的優先度只讀它（不依 JSON 欄位內容排序）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from app.models.script_report import REPORT_NOTE_MAX_CHARS, REPORT_REASONS, RED_LINE_REASONS
from app.observability import script_events
from app.repositories.errors import AlreadyReportedError
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import ScriptRepository
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import AlreadyReported, NotFound, ReportLimit, ReportOwnScript, ValidationError
from app.services.input_cleaning import clean_single_line
from app.services.quota_policy import taipei_day_bounds
from app.services.runtime_limits import RuntimeLimits

REASONS_ERROR = f"請選擇 1 至 {len(REPORT_REASONS)} 個檢舉原因，且不可重複。"
TEXT_TOO_LONG_ERROR = f"補充說明不可超過 {REPORT_NOTE_MAX_CHARS} 字。"
TEXT_TYPE_ERROR = "補充說明必須是文字。"
TEXT_UNSAFE_ERROR = "補充說明含有無法儲存的字元。"


def check_report_body(reasons: object, text: object) -> tuple[list[str], str | None]:
    """驗證並整理檢舉本文，回傳 `(理由, 自寫文字或 None)`；不合法拋出 `ValidationError`（`fields` 一次指出全部不合法的欄位）。

    - `reasons`：一至七個**不重複**的列舉值（空陣列、列舉以外的值、非陣列、非字串元素與重複值都不合法；重複值不被靜默去除）。
      通過後依 `REPORT_REASONS` 的順序（也是畫面上的順序）保存，與玩家勾選的先後無關。
    - `text`：選填的字串；沒給、`None`、空字串或清理後為空都視為沒有文字。清理（NFC、去控制字元與頭尾空白）之後至多 200 個 Unicode 字元。
    錯誤訊息不回顯玩家填的內容。
    """
    errors: dict[str, str] = {}
    cleaned_reasons: list[str] = []
    if (
        not isinstance(reasons, list) or not 1 <= len(reasons) <= len(REPORT_REASONS)
        or any(not isinstance(item, str) or item not in REPORT_REASONS for item in reasons)
        or len(set(reasons)) != len(reasons)
    ):
        errors["reasons"] = REASONS_ERROR
    else:
        chosen = set(reasons)
        cleaned_reasons = [reason for reason in REPORT_REASONS if reason in chosen]

    cleaned_text: str | None = None
    if text is not None:
        if not isinstance(text, str):
            errors["text"] = TEXT_TYPE_ERROR
        else:
            cleaned = clean_single_line(text)                # NFC 正規化、去控制字元與頭尾空白；孤立的代理字元（無法存成 UTF-8）回傳 None
            if cleaned is None:
                errors["text"] = TEXT_UNSAFE_ERROR
            elif len(cleaned) > REPORT_NOTE_MAX_CHARS:
                errors["text"] = TEXT_TOO_LONG_ERROR
            else:
                cleaned_text = cleaned or None
    if errors:
        raise ValidationError(fields=errors)
    return cleaned_reasons, cleaned_text


class ScriptReportService:
    def __init__(
        self,
        *,
        scripts: ScriptRepository,
        reports: ScriptReportRepository,
        policy: AccessPolicy,
        limits: RuntimeLimits,
        clock: Callable[[], datetime],
    ) -> None:
        self._scripts = scripts
        self._reports = reports
        self._policy = policy
        self._limits = limits
        self._clock = clock

    def submit(self, current: CurrentUser, version_public_id: object, reasons: object, text: object) -> None:
        """送出一筆檢舉（檢查順序見模組說明）；成功不回傳任何內容。"""
        self._policy.require_can_report(current)
        found = self._scripts.get_published_card(version_public_id) if isinstance(version_public_id, str) else None
        if found is None or found[1].seed_key is not None:
            raise NotFound()
        version, script = found
        if self._policy.is_owner(current, script.creator_id):
            raise ReportOwnScript()
        cleaned_reasons, cleaned_text = check_report_body(reasons, text)
        if self._reports.exists(current.id, version.id):
            raise AlreadyReported()

        now = self._clock()
        start, end = taipei_day_bounds(now)
        if self._reports.count_between(current.id, start, end) >= self._limits.report_daily():
            script_events.log_report_limited(user_id=current.id)
            raise ReportLimit(retry_after=max(1, int((end - now).total_seconds())))

        red_line = any(reason in RED_LINE_REASONS for reason in cleaned_reasons)
        try:
            self._reports.add(current.id, version.id, cleaned_reasons, red_line, cleaned_text, now=now)
        except AlreadyReportedError:
            raise AlreadyReported() from None             # 並行的另一個相同請求搶先寫入：資料庫的唯一約束為準
        script_events.log_report_submitted(
            user_id=current.id, reason_count=len(cleaned_reasons), text_length=len(cleaned_text or ""),
        )
