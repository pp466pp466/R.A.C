"""SQL Server `project` 資料庫結構的標準寫法（docs/project資料結構文件.md 與其測試共用）。

不連線到任何資料庫：把 ORM 的 metadata 以 SQL Server 方言編譯，得到實際資料庫會有的型別、可空、主外鍵與索引。
`deprecate_large_types` 與伺服器版本與實際連線時 SQLAlchemy 自動選擇的相同（NVARCHAR(max) 而不是 NTEXT，DATE 而不是 DATETIME）。
這個結果已與實際的 `project` 資料庫逐欄比對過（`alembic check` 沒有差異）。

新增或修改資料表、欄位時，用下面的指令印出該表在文件中應有的列，貼進 docs/project資料結構文件.md 後補上說明：

    python -X utf8 -m tests.support.db_schema users          # 一張表的資料字典列與索引列
    python -X utf8 -m tests.support.db_schema                # 列出全部資料表的名稱與欄位數
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from sqlalchemy import JSON, Column, Table
from sqlalchemy.dialects import mssql
from sqlalchemy.schema import UniqueConstraint

# 資料表的排列順序（文件的章節順序）：依領域分群，與文件第 2 節一致
DOMAIN_ORDER: dict[str, tuple[str, ...]] = {
    "帳號與身分": ("users", "user_identities", "auth_tokens", "deletion_tombstones"),
    "鎖定與申訴": ("lock_strikes", "lock_appeals"),
    "客服對話": ("chat_conversations", "chat_messages"),
    "模型用量與營運": ("llm_call_logs", "admin_audit_logs", "alert_states", "runtime_limits"),
    "RPG 劇本": ("scripts", "script_versions", "script_version_tags", "script_submissions"),
    "RPG 存檔": ("sessions", "memory_logs"),
    "遊戲幣": ("wallets", "wallet_entries"),
}
ORDERED_TABLES: tuple[str, ...] = tuple(name for names in DOMAIN_ORDER.values() for name in names)

_DIALECT = mssql.dialect(deprecate_large_types=True)
_DIALECT.server_version_info = (17, 0, 0)

YES, NO, NONE = "是", "否", "—"


def metadata_tables() -> dict[str, Table]:
    from app.models import Base

    return dict(Base.metadata.tables)


def sql_type(column: Column) -> str:
    """欄位在 SQL Server 上的型別（FLOAT 一律寫成 FLOAT(53)，與反射出來的寫法相同）。"""
    text = column.type.compile(dialect=_DIALECT)
    return "FLOAT(53)" if text == "FLOAT" else text


def is_json(column: Column) -> bool:
    return isinstance(column.type, JSON)


def type_cell(column: Column) -> str:
    """資料字典「型別」欄的寫法：自動遞增的主鍵加 IDENTITY(1,1)，JSON 欄位註明（JSON）。"""
    text = sql_type(column)
    if column.primary_key and column.autoincrement in (True, "auto") and text == "INTEGER":
        text += " IDENTITY(1,1)"
    if is_json(column):
        text += "（JSON）"
    return text


def single_column_unique(table: Table, column: Column) -> bool:
    if column.unique:
        return True
    return any(
        isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == [column.name] for c in table.constraints
    )


def key_tokens(table: Table, column: Column) -> list[str]:
    tokens: list[str] = []
    if column.primary_key:
        tokens.append("PK")
    for fk in column.foreign_keys:
        tokens.append(f"FK→{fk.column.table.name}.{fk.column.name}")
    if single_column_unique(table, column):
        tokens.append("UK")
    return tokens


def key_cell(table: Table, column: Column) -> str:
    return "、".join(key_tokens(table, column)) or NONE


def default_cell(column: Column) -> str:
    """資料庫層的預設值（程式層的預設寫在說明裡）。"""
    if column.server_default is not None and hasattr(column.server_default, "arg"):
        value = column.server_default.arg
        return f"`'{value}'`" if isinstance(value, str) else f"`{value}`"
    return NONE


@dataclass(frozen=True)
class IndexInfo:
    name: str
    kind: str          # 索引｜唯一索引｜篩選式唯一索引｜唯一約束
    columns: tuple[str, ...]
    where: str | None


def index_infos(table: Table) -> list[IndexInfo]:
    rows: list[IndexInfo] = []
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            rows.append(IndexInfo(constraint.name, "唯一約束", tuple(c.name for c in constraint.columns), None))
    for index in table.indexes:
        where = index.dialect_options["mssql"].get("where")
        text = str(where.compile(dialect=_DIALECT, compile_kwargs={"literal_binds": True})) if where is not None else None
        if index.unique:
            kind = "篩選式唯一索引" if text else "唯一索引"
        else:
            kind = "索引"
        rows.append(IndexInfo(index.name, kind, tuple(c.name for c in index.columns), text))
    return sorted(rows, key=lambda row: (row.kind == "索引", row.name))


@dataclass(frozen=True)
class ForeignKeyInfo:
    child: str
    column: str
    parent: str
    parent_column: str
    ondelete: str          # CASCADE｜SET NULL｜NO ACTION（資料庫沒有指定動作時）
    nullable: bool


def foreign_keys() -> list[ForeignKeyInfo]:
    rows: list[ForeignKeyInfo] = []
    tables = metadata_tables()
    for name in ORDERED_TABLES:
        for column in tables[name].columns:
            for fk in column.foreign_keys:
                rows.append(
                    ForeignKeyInfo(name, column.name, fk.column.table.name, fk.column.name, (fk.ondelete or "NO ACTION").upper(), bool(column.nullable))
                )
    return rows


def column_row(table: Table, index: int, column: Column, description: str = "") -> str:
    """資料字典的一列（說明留給人寫）。"""
    return (
        f"| {index} | `{column.name}` | {type_cell(column)} | {YES if column.nullable else NO} | {default_cell(column)} | "
        f"{key_cell(table, column)} | {description} |"
    )


def index_row(info: IndexInfo, purpose: str = "") -> str:
    columns = "、".join(f"`{c}`" for c in info.columns)
    where = f"`{info.where}`" if info.where else NONE
    return f"| `{info.name}` | {info.kind} | {columns} | {where} | {purpose} |"


def enumerations() -> dict[str, tuple[str, ...]]:
    """固定值欄位允許的值（取自程式常數，不手抄）：鍵是「資料表.欄位」。文件第 6 節必須列出每一個值。"""
    from app.llm.gemini import GeminiProvider
    from app.llm.openai_compatible import PROVIDER_NAME
    from app.models import (
        admin_audit_log, alert_state, chat, deletion_tombstone, llm_call_log, lock_strike, rpg_session, script, script_submission, user,
        wallet,
    )
    from app.services import cs_intents
    from app.services.age_policy import DEFAULT_AGE_POLICY
    from app.services.runtime_limits import LIMIT_KEYS

    ledger_statuses = tuple(value for key, value in vars(llm_call_log).items() if key.startswith("STATUS_") and isinstance(value, str))
    return {
        "users.role": user.ROLES,
        "users.member_tier": (user.TIER_FREE, user.TIER_PLUS, user.TIER_VIP),
        "users.status": (user.STATUS_ACTIVE, user.STATUS_SUSPENDED, user.STATUS_DELETION_PENDING),
        "users.lock_reason": lock_strike.STRIKE_KINDS,
        "users.content_tier_preference": user.CONTENT_TIERS,
        "users.age_range": tuple(age_range.code for age_range in DEFAULT_AGE_POLICY.ranges),
        "users.adult_confirm_method": (user.ADULT_CONFIRM_SELF_DECLARED,),
        "user_identities.provider": (user.PROVIDER_GOOGLE,),
        "auth_tokens.purpose": user.TOKEN_PURPOSES,
        "deletion_tombstones.reason": deletion_tombstone.DELETION_REASONS,
        "lock_strikes.kind": lock_strike.STRIKE_KINDS,
        "chat_messages.role": (chat.ROLE_USER, chat.ROLE_ASSISTANT),
        "chat_messages.msg_status": (chat.MSG_STATUS_OK, chat.MSG_STATUS_FALLBACK),
        "chat_messages.intent": tuple(cs_intents.INTENTS),
        "llm_call_logs.kind": llm_call_log.CALL_KINDS,
        "llm_call_logs.provider": (GeminiProvider.name, PROVIDER_NAME),
        "llm_call_logs.status": ledger_statuses,
        "admin_audit_logs.action": admin_audit_log.AUDIT_ACTIONS,
        "alert_states.alert_key": alert_state.ALERT_KEYS,
        "alert_states.state": alert_state.ALERT_STATES,
        "alert_states.severity": alert_state.ALERT_SEVERITIES,
        "runtime_limits.key": tuple(LIMIT_KEYS),
        "script_versions.status": script.VERSION_STATUSES,
        "script_versions.content_tier": user.CONTENT_TIERS,
        "script_versions.ending_mode": script.ENDING_MODES,
        "script_versions.review_verdict": script.REVIEW_VERDICTS,
        "script_versions.review_categories_json": script.REVIEW_CATEGORIES,
        "script_submissions.outcome": script_submission.SUBMISSION_OUTCOMES,
        "script_submissions.categories_json": script.REVIEW_CATEGORIES,
        "sessions.content_tier": user.CONTENT_TIERS,
        "sessions.status": rpg_session.SESSION_STATUSES,
        "memory_logs.turn_status": rpg_session.TURN_STATUSES,
        "wallet_entries.kind": wallet.ENTRY_KINDS,
        "wallet_entries.resolution": wallet.RESOLUTIONS,
    }


def _main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    tables = metadata_tables()
    if not argv:
        for name in ORDERED_TABLES:
            print(f"{name:<24} 欄位 {len(tables[name].columns):>2}")
        missing = set(tables) - set(ORDERED_TABLES)
        if missing:
            print("\n尚未歸入 DOMAIN_ORDER 的資料表（請加進 tests/support/db_schema.py 並寫進文件）：", sorted(missing))
        return 0
    table = tables[argv[0]]
    print("| # | 欄位 | 型別 | 可空 | 資料庫預設 | 鍵 | 說明 |")
    print("|---|---|---|---|---|---|---|")
    for number, column in enumerate(table.columns, start=1):
        print(column_row(table, number, column))
    print("\n| 名稱 | 種類 | 欄位 | 篩選條件 | 用途 |")
    print("|---|---|---|---|---|")
    for info in index_infos(table):
        print(index_row(info))
    return 0


if __name__ == "__main__":
    import os

    os.environ.setdefault("APP_ENV", "testing")
    sys.exit(_main(sys.argv[1:]))
