"""管理員審核劇本時回傳的資料形狀（見 admin-script-review 規格；純函式，沒有資料庫與狀態）。

回應一律是**明確組出的字典**，欄位在這裡逐一列出（白名單）：沒有任何內部整數編號、作者的 email、生日與年齡區間，
也沒有密碼雜湊。清單的每一筆只有目錄資訊與狀態中繼資料，**沒有劇本內文與審核備註全文**（只有 `has_note`）；
完整的標準化內容與審核備註只在單一版本的詳情（那條路徑先寫 `script.read` 稽核）。

輸入是 `AdminScriptRepository` 回傳的資料列（`ScriptVersionRow`、`ScriptVersionDetail`）。這裡刻意不匯入那個模組：
後台專用的 Repository 只有 `AdminService` 與 `registry` 能匯入（tests/architecture/test_admin_boundaries.py），所以用屬性存取的方式
讀取資料列；欄位的對應由 `tests/test_admin_script_views.py` 以真實的資料列釘住。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.script import VERSION_PENDING
from app.models.user import AUDIENCE_ALL

# 清單每一筆（與詳情的前半段）的鍵：固定的集合，新增欄位須先改規格並同步 `tests/test_admin_script_views.py`
LIST_KEYS = (
    "id", "script_id", "version_no", "status", "title", "summary", "author_name", "author_code", "author_id", "audience",
    "content_tier", "submitted_at", "published_at", "decided_at", "review_verdict", "review_categories", "removed_by",
    "decision_category", "has_note",
)
# 詳情在清單的鍵之外多出的兩個：完整的標準化內容與只供管理員的審核備註
DETAIL_KEYS = LIST_KEYS + ("decision_note", "content")


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat(timespec="seconds")


def _base(row: Any, *, with_review: bool) -> dict[str, Any]:
    """清單與詳情共用的欄位。`with_review`：是否帶模型層的審查結果與有疑慮的類別（清單只有待審才帶；詳情有效時都帶）。"""
    return {
        "id": row.version_public_id,
        "script_id": row.script_public_id,
        "version_no": row.version_no,
        "status": row.status,
        "title": row.title,
        "summary": row.summary,
        "author_name": row.author_name,
        "author_code": row.author_code,
        "author_id": row.author_public_id,                              # 空：內建劇本（沒有作者）
        "audience": row.audience or AUDIENCE_ALL,                       # 空值是遷移之前的版本，視同全讀者
        "content_tier": row.content_tier,
        "submitted_at": _iso(row.submitted_at),
        "published_at": _iso(row.published_at),
        "decided_at": _iso(row.decided_at),
        "review_verdict": row.review_verdict if with_review else None,
        "review_categories": list(row.review_categories) if with_review else [],
        "removed_by": row.removed_by,
        "decision_category": row.decision_category,
        "has_note": bool(row.has_note),
    }


def list_item(row: Any) -> dict[str, Any]:
    """審核清單的一筆：沒有內文與備註全文；模型層結果只在待審的版本帶（其他狀態的審查早已結束）。"""
    return _base(row, with_review=row.status == VERSION_PENDING)


def detail(found: Any) -> dict[str, Any]:
    """單一版本的詳情：清單那一筆的欄位（模型層結果只要與目前內容對應就帶，由 Repository 在雜湊不符時給空值），
    加上完整的標準化內容與審核備註。這是唯一含劇本文字與備註的形狀。"""
    return {**_base(found.row, with_review=True), "decision_note": found.decision_note, "content": found.content}
