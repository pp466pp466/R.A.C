"""docs/project資料結構文件.md 與 SQL Server `project` 資料庫結構（ORM 以 SQL Server 方言編譯）的逐項一致性檢查。

對資料庫新增、修改或刪除資料表、欄位、型別、可空、鍵、外鍵、索引、固定值或 migration 而沒有同步更新這份文件，就會在這裡失敗，
訊息會指出哪張表、哪個欄位、哪個值沒對上。測試只比對「結構事實」；說明文字寫得對不對仍靠修改時對照程式檢視。

結構事實的標準寫法在 tests/support/db_schema.py（已與實際的 `project` 資料庫逐欄比對過）；新增欄位時用
`python -X utf8 -m tests.support.db_schema <資料表>` 印出該表應有的列。
"""
import re

import pytest

from app.config import PROJECT_ROOT
from tests.support import db_schema as s
from tests.test_design_docs import DESIGN_DOCS, mermaid_blocks, migration_revisions

DOC_NAME = "project資料結構文件.md"
DOC = PROJECT_ROOT / "docs" / DOC_NAME
TABLES = s.metadata_tables()


def doc_text() -> str:
    return DOC.read_text(encoding="utf-8-sig")


def cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def table_section(text: str, table: str) -> str:
    match = re.search(rf"^### 5\.\d+ `{table}`.*?$(.*?)(?=^### |^## |\Z)", text, re.S | re.M)
    assert match, f"{DOC_NAME} 第 5 節沒有「{table}」這一節（資料字典）"
    return match.group(1)


def dictionary_rows(section: str) -> dict[str, list[str]]:
    rows = {}
    for line in section.splitlines():
        parts = cells(line) if line.startswith("|") else []
        if len(parts) == 7 and parts[0].isdigit() and re.fullmatch(r"`\w+`", parts[1]):
            rows[parts[1].strip("`")] = parts
    return rows


def index_rows(section: str) -> dict[str, list[str]]:
    rows = {}
    for line in section.splitlines():
        parts = cells(line) if line.startswith("|") else []
        if len(parts) == 5 and re.fullmatch(r"`(?:ix|uq)_\w+`", parts[0]):
            rows[parts[0].strip("`")] = parts
    return rows


def compare_table(text: str, table: str) -> list[str]:
    """文件裡這張資料表的字典與索引，和 ORM 不一致的地方（空清單＝一致）。"""
    problems: list[str] = []
    orm = TABLES[table]
    section = table_section(text, table)
    rows = dictionary_rows(section)
    expected = {column.name: column for column in orm.columns}
    for name in sorted(set(expected) - set(rows)):
        problems.append(f"{table}.{name}：資料庫有這個欄位，文件的資料字典沒有")
    for name in sorted(set(rows) - set(expected)):
        problems.append(f"{table}.{name}：文件有這個欄位，資料庫（ORM）已經沒有")
    for name in sorted(set(expected) & set(rows)):
        column, row = expected[name], rows[name]
        wanted = {2: s.type_cell(column), 3: s.YES if column.nullable else s.NO, 4: s.default_cell(column), 5: s.key_cell(orm, column)}
        labels = {2: "型別", 3: "可空", 4: "資料庫預設", 5: "鍵"}
        for position, value in wanted.items():
            if row[position] != value:
                problems.append(f"{table}.{name}：{labels[position]}應為「{value}」，文件寫「{row[position]}」")
        if not row[6]:
            problems.append(f"{table}.{name}：說明是空的")
    documented = index_rows(section)
    infos = {info.name: info for info in s.index_infos(orm)}
    for name in sorted(set(infos) - set(documented)):
        problems.append(f"{table}：索引或約束 {name} 沒有寫進「索引與約束」")
    for name in sorted(set(documented) - set(infos)):
        problems.append(f"{table}：文件的 {name} 在資料庫（ORM）已經不存在")
    for name in sorted(set(infos) & set(documented)):
        info, row = infos[name], documented[name]
        wanted_columns = "、".join(f"`{c}`" for c in info.columns)
        wanted_filter = f"`{info.where}`" if info.where else s.NONE
        if (row[1], row[2], row[3]) != (info.kind, wanted_columns, wanted_filter):
            problems.append(f"{table}.{name}：應為「{info.kind}／{wanted_columns}／{wanted_filter}」，文件寫「{row[1]}／{row[2]}／{row[3]}」")
        if not row[4]:
            problems.append(f"{table}.{name}：用途是空的")
    return problems


# ---- 結構 ------------------------------------------------------------------------------------------------------------


def test_the_document_is_registered_as_a_design_doc():
    assert DOC_NAME in DESIGN_DOCS
    assert f"]({DOC_NAME})" in (PROJECT_ROOT / "docs" / "README.md").read_text(encoding="utf-8-sig")


def test_every_table_belongs_to_exactly_one_domain_in_the_support_module():
    assert sorted(s.ORDERED_TABLES) == sorted(TABLES), f"請把新資料表加進 tests/support/db_schema.py 的 DOMAIN_ORDER：{sorted(set(TABLES) - set(s.ORDERED_TABLES))}"
    assert len(s.ORDERED_TABLES) == len(set(s.ORDERED_TABLES))


def test_the_header_names_the_current_migration_and_the_table_and_column_counts():
    text = doc_text()
    _, head = migration_revisions()
    header = "\n".join(text.splitlines()[:5])
    assert f"資料庫版本：`{head}`" in header, f"標頭的資料庫版本不是最新的 migration：{head}"
    tables, columns = len(TABLES), sum(len(table.columns) for table in TABLES.values())
    assert f"**{tables} 張資料表、{columns} 個欄位**" in text, f"標頭的表數與欄位數應為 {tables} 張、{columns} 個"
    assert f"| 資料表與欄位 | {tables} 張業務資料表、{columns} 個欄位" in text


def test_the_dictionary_has_one_section_per_table_in_the_documented_order():
    headings = re.findall(r"^### 5\.\d+ `(\w+)`", doc_text(), re.M)
    assert headings == list(s.ORDERED_TABLES)


@pytest.mark.parametrize("table", s.ORDERED_TABLES)
def test_each_tables_columns_indexes_and_constraints_match_the_database_structure(table):
    problems = compare_table(doc_text(), table)
    assert problems == [], "\n" + "\n".join(problems)


def test_the_tables_overview_lists_every_table_with_its_column_count():
    text = doc_text()
    for name, table in TABLES.items():
        assert re.search(rf"^\| `{name}` \|.*\| {len(table.columns)} \|$", text, re.M), f"第 2 節總覽的「{name}」欄位數應為 {len(table.columns)}"


# ---- 關聯 ------------------------------------------------------------------------------------------------------------


def test_the_foreign_key_list_matches_the_database():
    section = re.search(r"^### 4\.1 .*?$(.*?)(?=^### |\Z)", doc_text(), re.S | re.M).group(1)
    documented = set()
    for line in section.splitlines():
        parts = cells(line) if line.startswith("|") else []
        if len(parts) == 5 and re.fullmatch(r"`\w+\.\w+`", parts[0]):
            documented.add((parts[0].strip("`"), parts[1].strip("`"), parts[2], parts[3]))
    expected = {(f"{fk.child}.{fk.column}", f"{fk.parent}.{fk.parent_column}", fk.ondelete, "是" if fk.nullable else "否") for fk in s.foreign_keys()}
    assert documented == expected, f"文件多寫：{sorted(documented - expected)}；漏寫：{sorted(expected - documented)}"


def test_the_overview_erd_declares_every_table_and_every_foreign_key_relationship():
    blocks = [block for block in mermaid_blocks(doc_text()) if block.lstrip().startswith("erDiagram")]
    assert len(blocks) >= 5, "應有全庫總覽與四個領域的 ERD"
    overview = blocks[0]
    for name in TABLES:
        assert re.search(rf"^\s*{name}\s*\{{", overview, re.M), f"全庫 ERD 沒有宣告實體 {name}"
    for fk in s.foreign_keys():
        pattern = rf"^\s*{fk.parent}\s+[|o{{}}.\-]+\s+{fk.child}\s*:\s*\"{fk.column} {fk.ondelete}\""
        assert re.search(pattern, overview, re.M), f"全庫 ERD 沒有 {fk.parent} → {fk.child}（{fk.column} {fk.ondelete}）這條關聯"


def test_every_domain_erd_lists_all_columns_of_its_tables():
    text = doc_text()
    blocks = [block for block in mermaid_blocks(text) if block.lstrip().startswith("erDiagram")][1:]
    declared: dict[str, set[str]] = {}
    for block in blocks:
        for entity, body in re.findall(r"^\s{4}(\w+) \{\n(.*?)^\s{4}\}", block, re.S | re.M):
            names = set(re.findall(r"^\s+\w+ (\w+)", body, re.M))
            if len(names) > 1:                      # 只有 id 的是其他領域圖裡的替身實體
                declared[entity] = names
    for name, table in TABLES.items():
        assert name in declared, f"領域 ERD 沒有完整列出 {name} 的欄位"
        assert declared[name] == {column.name for column in table.columns}, f"領域 ERD 的 {name} 欄位與資料庫不一致：{sorted(declared[name] ^ {c.name for c in table.columns})}"


# ---- 固定值、JSON、migration -------------------------------------------------------------------------------------------


def test_every_fixed_value_is_listed_in_the_enumeration_dictionary():
    text = doc_text()
    section = re.search(r"^## 6\. .*?$(.*?)(?=^## 7\. )", text, re.S | re.M).group(1)
    rows = {}
    for line in section.splitlines():
        parts = cells(line) if line.startswith("|") else []
        if len(parts) == 3 and re.fullmatch(r"`\w+\.\w+`", parts[0]):
            rows[parts[0].strip("`")] = set(re.findall(r"`([^`]+)`", parts[1]))
    enumerations = s.enumerations()
    assert set(rows) == set(enumerations), f"第 6 節多寫或漏寫的欄位：{sorted(set(rows) ^ set(enumerations))}"
    for key, values in enumerations.items():
        assert rows[key] == set(values), f"{key} 的允許值不一致：文件多寫 {sorted(rows[key] - set(values))}；漏寫 {sorted(set(values) - rows[key])}"
        table, column = key.split(".")
        assert column in TABLES[table].columns, f"{key} 不是資料庫的欄位"


def test_every_json_column_has_a_structure_row():
    section = re.search(r"^## 7\. .*?$(.*?)(?=^## 8\. )", doc_text(), re.S | re.M).group(1)
    documented = {parts[0].strip("`") for parts in (cells(line) for line in section.splitlines() if line.startswith("|")) if len(parts) == 3 and re.fullmatch(r"`\w+\.\w+`", parts[0])}
    expected = {f"{name}.{column.name}" for name, table in TABLES.items() for column in table.columns if s.is_json(column)}
    assert documented == expected, f"JSON 欄位結構多寫：{sorted(documented - expected)}；漏寫：{sorted(expected - documented)}"


def test_the_migration_history_lists_every_revision_in_order():
    section = re.search(r"^## 12\. .*?$(.*?)(?=^## 13\. )", doc_text(), re.S | re.M).group(1)
    documented = re.findall(r"^\| `(\w+)` \|", section, re.M)
    revisions, head = migration_revisions()
    assert documented == revisions, f"migration 歷程與實際的 revision 不一致：文件 {documented}；實際 {revisions}"
    assert documented[-1] == head


def test_the_maintenance_section_names_the_tools_and_the_checks():
    text = doc_text()
    for needle in ("tests.support.db_schema", "tests/test_project_db_schema_doc.py", "alembic check", "alembic current", "**何時更新本文**"):
        assert needle in text, needle



# ---- 異動紀錄（第 14 節）------------------------------------------------------------------------------------------------------

CHANGE_TYPES = {"新增", "修改", "刪除", "套用", "文件"}
STRUCTURE_TYPES = {"新增", "修改", "刪除"}


def migration_dates() -> dict[str, str]:
    """revision -> migration 檔名裡的日期（YYYY-MM-DD）。"""
    dates = {}
    for path in sorted((PROJECT_ROOT / "migrations" / "versions").glob("*.py")):
        source = path.read_text(encoding="utf-8-sig")
        revision = re.search(r'^revision\s*(?::\s*str)?\s*=\s*"([^"]+)"', source, re.M).group(1)
        stamp = re.match(r"(\d{4})(\d{2})(\d{2})_", path.name)
        dates[revision] = f"{stamp.group(1)}-{stamp.group(2)}-{stamp.group(3)}"
    return dates


def change_log(text: str) -> list[list[str]]:
    section = text.split("\n## 14. ", 1)[1]
    rows = []
    for line in section.splitlines():
        parts = cells(line) if line.startswith("|") else []
        if len(parts) == 5 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]):
            rows.append(parts)
    return rows


def change_log_problems(text: str) -> list[str]:
    problems: list[str] = []
    headings = re.findall(r"^## (\d+)\. ", text, re.M)
    if not headings or headings[-1] != "14":
        problems.append(f"異動紀錄必須是檔案的最後一節（第 14 節），目前最後一節是第 {headings[-1] if headings else '?'} 節")
        return problems
    rows = change_log(text)
    if not rows:
        return ["第 14 節沒有任何異動紀錄"]
    for number, row in enumerate(rows, start=1):
        if row[1] not in CHANGE_TYPES:
            problems.append(f"第 {number} 列（{row[0]}）：類型「{row[1]}」不是 {sorted(CHANGE_TYPES)} 之一")
        for position, label in ((2, "對象"), (3, "內容"), (4, "依據")):
            if not row[position]:
                problems.append(f"第 {number} 列（{row[0]}）：{label}是空的")
    dates = [row[0] for row in rows]
    if dates != sorted(dates):
        problems.append("異動紀錄必須由舊到新排列（新的列追加在最下方，不改寫舊的列）")
    for revision, date in migration_dates().items():
        if not any(row[0] == date and row[1] in STRUCTURE_TYPES and f"`{revision}`" in row[4] for row in rows):
            problems.append(f"migration {revision}（{date}）沒有對應的異動紀錄（需要一列：日期 {date}、類型新增／修改／刪除、依據寫 `{revision}`）")
    header = re.search(r"最後更新：(\d{4}-\d{2}-\d{2})", "\n".join(text.splitlines()[:5]))
    if header and header.group(1) != dates[-1]:
        problems.append(f"標頭的「最後更新」是 {header.group(1)}，但最後一列異動紀錄的日期是 {dates[-1]}")
    return problems


def test_the_change_log_is_the_last_section_and_records_every_migration_in_order():
    problems = change_log_problems(doc_text())
    assert problems == [], "\n" + "\n".join(problems)


def test_the_change_log_notices_a_missing_migration_a_bad_order_a_foreign_type_a_misplaced_section_and_a_stale_header():
    text = doc_text()
    assert change_log_problems(text) == []

    # 移除 0013 的全部「新增／修改／刪除」列（只留下「套用」列）：沒有結構異動的紀錄，就等於這個 migration 沒有被記錄
    no_head = "\n".join(
        line for line in text.splitlines()
        if not (re.match(r"\| 2026-11-04 \| (新增|修改|刪除) \|", line) and "`0013_rpg_script_authoring`" in line)
    )
    assert no_head != text
    assert any("0013_rpg_script_authoring" in p and "沒有對應" in p for p in change_log_problems(no_head))

    bad_type = text.replace("| 2026-10-27 | 新增 | 資料表 `alembic_version`", "| 2026-10-27 | 變更 | 資料表 `alembic_version`", 1)
    assert bad_type != text
    assert any("變更" in p for p in change_log_problems(bad_type))

    rows = [line for line in text.splitlines() if re.match(r"\| 2026-10-2[78] \|", line)]
    swapped = text.replace(rows[0], "@@FIRST@@").replace(rows[-1], rows[0]).replace("@@FIRST@@", rows[-1])
    assert swapped != text
    assert any("由舊到新" in p for p in change_log_problems(swapped))

    stale_header = re.sub(r"最後更新：\d{4}-\d{2}-\d{2}", "最後更新：2000-01-01", text, count=1)          # 不寫死日期：每次追加異動紀錄都會換日期
    assert stale_header != text
    assert any("最後更新" in p for p in change_log_problems(stale_header))

    moved = text.replace("## 14. 異動紀錄（Change Log）", "## 14. 異動紀錄（Change Log）", 1) + "\n## 15. 其他\n\n內容\n"
    assert any("最後一節" in p for p in change_log_problems(moved))


# ---- 這些檢查真的會抓到問題 ---------------------------------------------------------------------------------------------------


def test_the_checks_notice_a_missing_column_a_wrong_type_a_dropped_index_and_a_stale_column():
    text = doc_text()
    assert compare_table(text, "users") == []

    without_nickname = re.sub(r"^\| \d+ \| `nickname` \|.*\n", "", text, flags=re.M)
    assert any("users.nickname" in p and "文件的資料字典沒有" in p for p in compare_table(without_nickname, "users"))

    wrong_type = re.sub(r"(\| `nickname_code` \| )NVARCHAR\(8\)", r"\1NVARCHAR(4)", text)
    assert any("users.nickname_code" in p and "型別" in p for p in compare_table(wrong_type, "users"))

    wrong_null = re.sub(r"(\| `password_hash` \| NVARCHAR\(255\) \| )是", r"\1否", text)
    assert any("users.password_hash" in p and "可空" in p for p in compare_table(wrong_null, "users"))

    no_index = re.sub(r"^\| `ix_users_locked_at` \|.*\n", "", text, flags=re.M)
    assert any("ix_users_locked_at" in p for p in compare_table(no_index, "users"))

    stale = re.sub(r"(^\| 35 \| `updated_at` \|.*\n)", r"\1| 36 | `old_column` | INTEGER | 否 | — | — | 已經刪除的欄位 |\n", text, count=1, flags=re.M)
    assert stale != text, "反向驗證的替換沒有生效"
    assert any("users.old_column" in p and "已經沒有" in p for p in compare_table(stale, "users"))

    no_description = re.sub(r"(^\| \d+ \| `nickname` \| [^|]+\| [^|]+\| [^|]+\| [^|]+\| )[^|]+\|", r"\1 |", text, count=1, flags=re.M)
    assert no_description != text, "反向驗證的替換沒有生效"
    assert any("users.nickname" in p and "說明是空的" in p for p in compare_table(no_description, "users"))
