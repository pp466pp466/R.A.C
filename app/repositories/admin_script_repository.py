"""管理員審核劇本用的**唯讀**資料存取（見 admin-script-review 規格與 rpg-script-publishing 的 design D3）。

與 `AdminRepository` 同一個立場：後台本來就是在讀他人的資料，所以這個類別刻意不帶 `user_id`；它的保護來自另一個方向——
- 只被 `AdminService` 與 `registry` 匯入（tests/architecture/test_admin_boundaries.py）；
- `AdminService` 的每個公開方法第一件事是 `AccessPolicy.require_admin`，讀取劇本全文先寫 `script.read` 稽核；
- **只讀**：沒有任何寫入方法。版本狀態的轉移只在 `ScriptRepository`（核可、退回、下架、恢復），所以「狀態只在一個檔案寫入」
  的架構守門不會被這裡破壞。

可讀範圍：**草稿（`draft`）一律讀不到**——它是作者還在編輯的內容，沒有送審就不是管理員的事；其餘（待審、上架中、被退回、被取代、
已下架）才可讀。草稿與不存在的識別碼得到完全相同的結果（`None`）。清單只回傳明確選出的欄位（資料類別），不回傳 ORM 物件，
所以內部編號、劇本文字與審核備註不可能意外外露：清單沒有內文，備註只有 `has_note`；完整內容與備註只在 `get_reviewable`。

排序（只用 ORM 表達式）：
- `pending_review`：模型層結果為 `flag` 且審查雜湊等於目前內容雜湊的排最前（結果對應其他內容就失效），其餘依送審時間由舊到新
  （沒有送審時間的舊資料以建立時間代替），同時間依編號；
- `published`：發佈時間由新到舊；`removed`：決定（下架）時間由新到舊；同時間皆依編號由大到小；
- `reported`（rpg-script-reports）：**上架中**且至少有一筆**未結案**檢舉的版本。依序：有人勾選紅線理由者在前、未結案檢舉數由多到少、
  最早的未結案檢舉時間由舊到新、版本編號由小到大。檢舉只升高優先度，不改變版本狀態。

**檢舉的彙總**（所有清單與詳情都帶 `report_count`、`red_line_reported`）：以一個子查詢對未結案的檢舉依版本分組（筆數、是否有紅線、最早時間），
對版本做外連接（沒有檢舉的版本為 0 與假）。紅線只讀建立時另存的 `red_line` 布林，不依 JSON 欄位內容篩選或排序（AGENTS.md 第 4 節）；
布林一律以 `CASE` 取 0／1（SQL Server 不接受把比較式直接當作值回傳）。
**讀取檢舉內容（`list_reports`）拿不到檢舉人**：查詢根本沒有選取 `reporter_id`，不是「記得不要放」而是「沒有取出」。
Repository 不提交也不回復交易，也不寫稽核（Service 負責）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, func, select, true
from sqlalchemy.orm import Session

from app.models.script import (
    REVIEW_FLAG, VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REMOVED, Script, ScriptVersion,
)
from app.models.script_report import ScriptReport
from app.models.user import User
from app.repositories.script_repository import ScriptRepository

# 「被檢舉」檢視：不是版本的狀態，而是「上架中且有未結案檢舉」的篩選（rpg-script-reports）
LIST_REPORTED = "reported"
# 清單可選的狀態（其他值由 Service 以 400 擋下）
LISTABLE_STATUSES = (VERSION_PENDING, VERSION_PUBLISHED, VERSION_REMOVED, LIST_REPORTED)


@dataclass(frozen=True)
class ScriptVersionRow:
    """清單的一列：標題與簡介等目錄資訊與狀態中繼資料，**沒有**劇本內文、審核備註與任何內部編號。

    `review_verdict` 與 `review_categories` 只有在審查雜湊等於目前內容雜湊時才有值（內容有變動就失效）；
    `author_public_id` 為空代表沒有作者：內建劇本，或**匿名保留**的劇本（作者已註銷帳號，rpg-script-reports）；
    **是不是內建劇本看 `is_builtin`（種子鍵非空）**，不要用有沒有作者推論；`has_note` 只說明有沒有審核備註。
    """

    version_public_id: str
    script_public_id: str
    version_no: int
    status: str
    title: str
    summary: str
    author_name: str | None
    author_code: str | None
    author_public_id: str | None
    audience: str | None
    content_tier: str
    submitted_at: datetime | None
    published_at: datetime | None
    decided_at: datetime | None
    review_verdict: str | None
    review_categories: tuple[str, ...]
    removed_by: str | None
    decision_category: str | None
    has_note: bool
    is_builtin: bool
    report_count: int                    # 未結案的檢舉數（沒有檢舉為 0）；只有數字，沒有檢舉的任何內容
    red_line_reported: bool              # 未結案的檢舉中是否有人勾選紅線理由


@dataclass(frozen=True)
class ScriptReportRow:
    """一筆檢舉（管理員讀取用）：理由、自寫文字、建立時間與結案資訊。**沒有檢舉人**（身分、帳號、內部編號都沒有）：
    查詢沒有選取 `reporter_id`，所以這個形狀無從攜帶。自寫文字是玩家文字（個資），只在讀取檢舉的路徑（先寫稽核）出現。"""

    reasons: tuple[str, ...]
    text: str | None
    created_at: datetime
    closed_at: datetime | None
    closed_result: str | None


@dataclass(frozen=True)
class ScriptVersionDetail:
    """一個可讀版本的全貌：清單那一列、完整的標準化內容（與開存檔時複製的相同）與只供管理員的審核備註。"""

    row: ScriptVersionRow
    content: dict[str, Any]
    decision_note: str | None


@dataclass(frozen=True)
class ScriptVersionRef:
    """版本的最小參照：任何狀態（含草稿）都找得到，只供 Service 判斷「核可、退回、下架、恢復」的前置狀態並組出稽核的目標；
    沒有任何內容。內部編號不在其中。"""

    version_public_id: str
    script_public_id: str
    status: str
    removed_by: str | None
    author_public_id: str | None         # 空：內建劇本或匿名保留的劇本（沒有作者）
    is_builtin: bool                     # 種子鍵非空；不是「沒有作者」


def _review_valid(version: Any) -> Any:
    """審查結果有效：審查當時的雜湊就是目前的內容雜湊（ORM 表達式，傳入模型類別或欄位集合）。"""
    return and_(version.review_hash.is_not(None), version.review_hash == version.content_hash)


def _open_report_summary() -> Any:
    """未結案檢舉的彙總子查詢（依版本分組）：`n` 筆數、`red` 是否有紅線（0／1）、`earliest` 最早的建立時間。
    只含至少有一筆未結案檢舉的版本；紅線只讀 `red_line` 布林，比較用 `true()`（不用 `.is_(True)`：SQL Server 會產生 `IS 1`）。"""
    return (
        select(
            ScriptReport.version_id.label("version_id"),
            func.count(ScriptReport.id).label("n"),
            func.max(case((ScriptReport.red_line == true(), 1), else_=0)).label("red"),
            func.min(ScriptReport.created_at).label("earliest"),
        )
        .where(ScriptReport.closed_at.is_(None))
        .group_by(ScriptReport.version_id)
        .subquery("open_reports")
    )


class AdminScriptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _columns(self, open_reports: Any) -> tuple[Any, ...]:
        """清單與詳情共用的欄位（明確列出，不含內文與備註全文）。審查結果在雜湊不符時以 NULL 代替。
        `open_reports` 是 `_open_report_summary()` 的結果（呼叫端須以 `_joined` 外連接同一個子查詢）。"""
        valid = _review_valid(ScriptVersion)
        return (
            ScriptVersion.public_id, Script.public_id, ScriptVersion.version_no, ScriptVersion.status, ScriptVersion.title,
            ScriptVersion.summary, Script.author_name, Script.author_code, User.public_id, ScriptVersion.audience,
            ScriptVersion.content_tier, ScriptVersion.submitted_at, ScriptVersion.published_at, ScriptVersion.decided_at,
            case((valid, ScriptVersion.review_verdict), else_=None), case((valid, ScriptVersion.review_categories_json), else_=None),
            ScriptVersion.removed_by, ScriptVersion.decision_category,
            # 「有沒有備註」以 CASE 取得 0／1（SQL Server 不接受把比較式直接當作值回傳）
            case((ScriptVersion.decision_note.is_not(None), 1), else_=0),
            # 內建劇本＝種子鍵非空（同一個 CASE 做法）；沒有作者的劇本可能是匿名保留的，不能用作者推論
            case((Script.seed_key.is_not(None), 1), else_=0),
            # 未結案的檢舉數與紅線（沒有檢舉為 0；外連接的空值以 COALESCE 取 0）
            func.coalesce(open_reports.c.n, 0), func.coalesce(open_reports.c.red, 0),
        )

    def _joined(self, statement: Any, open_reports: Any = None) -> Any:
        """版本 ⨝ 劇本 ⟕ 作者（內建劇本與匿名保留的劇本沒有作者，所以是外連接）；給了 `open_reports` 時再外連接未結案檢舉的彙總。"""
        joined = statement.join(Script, Script.id == ScriptVersion.script_id).outerjoin(User, User.id == Script.creator_id)
        if open_reports is not None:
            joined = joined.outerjoin(open_reports, open_reports.c.version_id == ScriptVersion.id)
        return joined

    @staticmethod
    def _row(values: Any) -> ScriptVersionRow:
        (version_id, script_id, version_no, status, title, summary, author_name, author_code, author_public_id, audience, tier,
         submitted_at, published_at, decided_at, verdict, categories, removed_by, category, has_note, is_builtin,
         report_count, red_line) = values
        # 位置參數：欄位名稱（送審時間、下架者、決定…）出現在關鍵字引數會被架構守門當成「寫入」（tests/architecture/test_script_state_writes.py），
        # 而這裡只是組出唯讀的資料列；順序與 `ScriptVersionRow` 的欄位、`_columns()` 的選取順序相同。
        return ScriptVersionRow(
            version_id, script_id, version_no, status, title, summary, author_name, author_code, author_public_id, audience, tier,
            submitted_at, published_at, decided_at, verdict, tuple(categories or ()), removed_by, category, bool(has_note),
            bool(is_builtin), int(report_count), int(red_line) == 1,
        )

    # ---- 清單 ------------------------------------------------------------------------------------

    def list_by_status(self, status: str, *, limit: int, offset: int) -> list[ScriptVersionRow]:
        """某個狀態的版本清單（排序見檔案開頭）。狀態必須是 `LISTABLE_STATUSES` 之一（Service 先驗證；不合拋出 `ValueError`）。
        `reported` 是「被檢舉」檢視：上架中且有未結案檢舉的版本，依優先度排序。"""
        if status not in LISTABLE_STATUSES:
            raise ValueError(f"審核清單不支援的狀態：{status!r}")
        open_reports = _open_report_summary()
        statement = self._joined(select(*self._columns(open_reports)), open_reports)
        if status == LIST_REPORTED:
            # 彙總子查詢只含有未結案檢舉的版本，所以 `n` 不為空就代表至少一筆；紅線者在前、人數多者在前、最早檢舉者在前
            statement = statement.where(ScriptVersion.status == VERSION_PUBLISHED, open_reports.c.n.is_not(None))
            order = (open_reports.c.red.desc(), open_reports.c.n.desc(), open_reports.c.earliest.asc(), ScriptVersion.id.asc())
        else:
            statement = statement.where(ScriptVersion.status == status)
            if status == VERSION_PENDING:
                flagged_first = case((and_(ScriptVersion.review_verdict == REVIEW_FLAG, _review_valid(ScriptVersion)), 0), else_=1)
                order = (
                    flagged_first, func.coalesce(ScriptVersion.submitted_at, ScriptVersion.created_at).asc(), ScriptVersion.id.asc(),
                )
            elif status == VERSION_PUBLISHED:
                order = (ScriptVersion.published_at.desc(), ScriptVersion.id.desc())
            else:
                order = (ScriptVersion.decided_at.desc(), ScriptVersion.id.desc())
        return [self._row(values) for values in self._session.execute(statement.order_by(*order).limit(limit).offset(offset)).all()]

    # ---- 單一版本 ----------------------------------------------------------------------------------

    def get_reviewable(self, version_public_id: str) -> ScriptVersionDetail | None:
        """可讀的版本全貌：只有送審過的版本（狀態不是 `draft`）；草稿與不存在的識別碼一樣回傳 `None`。
        內容取自 `ScriptRepository.content_of`（與開存檔時複製的內容同一個還原函式，所以兩處不會分歧）。"""
        open_reports = _open_report_summary()
        found = self._session.execute(
            self._joined(select(ScriptVersion, *self._columns(open_reports)), open_reports).where(
                ScriptVersion.public_id == version_public_id, ScriptVersion.status != VERSION_DRAFT,
            )
        ).first()
        if found is None:
            return None
        version, *values = found
        return ScriptVersionDetail(self._row(values), ScriptRepository(self._session).content_of(version), version.decision_note)   # 位置參數：見 `_row`

    def list_reports(self, version_public_id: str) -> list[ScriptReportRow]:
        """這個版本的全部檢舉（含已結案），由舊到新（同時間依編號）。**不選取檢舉人**（`reporter_id`），所以回傳的資料列
        無從攜帶檢舉人的任何資訊。呼叫端（`AdminService`）須先確認版本可讀（非草稿）並先寫 `report.read` 稽核再回傳；
        不存在的識別碼回傳空清單。"""
        rows = self._session.execute(
            select(ScriptReport.reasons_json, ScriptReport.note, ScriptReport.created_at, ScriptReport.closed_at, ScriptReport.closed_result)
            .join(ScriptVersion, ScriptVersion.id == ScriptReport.version_id)
            .where(ScriptVersion.public_id == version_public_id)
            .order_by(ScriptReport.created_at.asc(), ScriptReport.id.asc())
        ).all()
        return [ScriptReportRow(tuple(reasons or ()), text, created_at, closed_at, result) for reasons, text, created_at, closed_at, result in rows]

    def find_ref(self, version_public_id: str) -> ScriptVersionRef | None:
        """任何狀態（含草稿）的最小參照。給核可、退回、下架與恢復的前置判斷與稽核目標；不含內容。"""
        found = self._session.execute(
            self._joined(
                select(ScriptVersion.public_id, Script.public_id, ScriptVersion.status, ScriptVersion.removed_by, User.public_id, Script.seed_key)
            ).where(ScriptVersion.public_id == version_public_id)
        ).first()
        if found is None:
            return None
        version_id, script_id, status, removed_by, author_public_id, seed_key = found
        # 內建劇本＝種子鍵非空；沒有作者（`author_public_id` 為空）的匿名保留劇本不是內建，管理員可以下架它
        return ScriptVersionRef(version_id, script_id, status, removed_by, author_public_id, seed_key is not None)   # 位置參數：見 `_row`
