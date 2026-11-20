"""rpg-script-reports 的模型與常數（script-reports、script-anonymous-retention 規格）：檢舉的固定列舉、紅線集合、結案結果、自寫文字上限、
資料表的外鍵設計（版本無動作、檢舉人串聯）、唯一約束與索引，以及 `users.keep_published_scripts` 的預設。
SQLite 與真實 SQL Server（標記 sqlserver）各跑一次的部分用 `member_session`。"""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Base, ScriptReport, User
from app.models.script import OFFICIAL_AUTHOR_NAME, RETIRED_AUTHOR_NAME
from app.models.script_report import (
    CLOSED_DISMISSED, CLOSED_REMOVED, CLOSED_RESULTS, CLOSED_SUPERSEDED, RED_LINE_REASONS, REPORT_NOTE_MAX_CHARS, REPORT_REASONS,
)


def test_the_reasons_are_the_seven_documented_values_in_screen_order():
    assert REPORT_REASONS == (
        "minor_sexual", "sexual_violence", "real_person_sexual", "tier_mismatch", "plagiarism", "privacy_harassment", "other",
    )
    assert len(set(REPORT_REASONS)) == len(REPORT_REASONS)


def test_the_red_line_reasons_are_the_three_sexual_categories_and_a_subset_of_the_reasons():
    assert set(RED_LINE_REASONS) == {"minor_sexual", "sexual_violence", "real_person_sexual"}
    assert set(RED_LINE_REASONS) <= set(REPORT_REASONS)
    assert not {"tier_mismatch", "plagiarism", "privacy_harassment", "other"} & set(RED_LINE_REASONS)      # 其餘四項不升高優先度


def test_the_closing_results_are_removed_dismissed_and_the_system_only_superseded():
    assert (CLOSED_REMOVED, CLOSED_DISMISSED, CLOSED_SUPERSEDED) == ("removed", "dismissed", "superseded")
    assert CLOSED_RESULTS == (CLOSED_REMOVED, CLOSED_DISMISSED, CLOSED_SUPERSEDED)
    longest = max(len(value) for value in CLOSED_RESULTS)
    assert longest <= ScriptReport.__table__.c.closed_result.type.length        # 欄位放得下最長的結果


def test_the_free_text_limit_is_200_characters():
    assert REPORT_NOTE_MAX_CHARS == 200


def test_the_retired_author_name_is_distinct_from_the_official_one_and_fits_a_pen_name_column():
    from app.models import Script

    assert RETIRED_AUTHOR_NAME == "已註銷的作者" and RETIRED_AUTHOR_NAME != OFFICIAL_AUTHOR_NAME
    assert len(RETIRED_AUTHOR_NAME) <= Script.__table__.c.author_name.type.length


def test_the_report_table_has_the_documented_foreign_keys_unique_constraint_and_indexes():
    table = ScriptReport.__table__
    foreign_keys = {fk.parent.name: fk for fk in table.foreign_keys}
    assert set(foreign_keys) == {"version_id", "reporter_id"}
    assert foreign_keys["version_id"].column.table.name == "script_versions" and foreign_keys["version_id"].ondelete is None   # 無動作
    assert foreign_keys["reporter_id"].column.table.name == "users" and foreign_keys["reporter_id"].ondelete == "CASCADE"
    uniques = [c for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"]
    assert [(c.name, [column.name for column in c.columns]) for c in uniques] == [("uq_script_reports_reporter_id", ["reporter_id", "version_id"])]
    assert {i.name: [column.name for column in i.columns] for i in table.indexes} == {
        "ix_script_reports_version_id_closed_at": ["version_id", "closed_at"],
        "ix_script_reports_reporter_id_created_at": ["reporter_id", "created_at"],
        "ix_script_reports_closed_at": ["closed_at"],
    }
    assert not any(i.unique for i in table.indexes)


def test_the_report_columns_have_the_documented_types_and_nullability():
    columns = ScriptReport.__table__.c
    assert {name for name, column in columns.items() if column.nullable} == {"note", "closed_at", "closed_result"}
    assert "public_id" not in columns                                           # 檢舉不被個別定址：管理員結案是對版本整批做
    assert type(columns.reasons_json.type).__name__ == "JSON" and type(columns.red_line.type).__name__ == "Boolean"
    assert type(columns.note.type).__name__ == "UnicodeText"                    # 一般文字欄位用 Unicode（SQL Server 對應 NVARCHAR）


def test_the_report_table_is_part_of_the_metadata():
    assert "script_reports" in Base.metadata.tables


def test_the_retention_preference_defaults_to_off_in_python_and_in_the_database_definition():
    column = User.__table__.c.keep_published_scripts
    assert column.nullable is False and column.default.arg is False
    assert str(column.server_default.arg) == "0"                                # 舊版程式的 INSERT（不含此欄）取得關閉


def test_a_user_created_without_the_preference_has_it_off(member_session):
    user = User(email="p1@example.com", email_canonical="p1@example.com", display_name="偏好", status="active")
    member_session.add(user)
    member_session.flush()
    member_session.expire_all()
    assert member_session.get(User, user.id).keep_published_scripts is False


def test_an_insert_that_omits_the_preference_gets_the_database_default(member_session):
    """舊版程式形狀的 INSERT（完全不帶新欄位）在兩種資料庫都取得假值：用「從資料庫反射出來的資料表」寫入，沒有模型的 Python 預設值可依賴。"""
    from sqlalchemy import MetaData, Table
    from datetime import datetime

    connection = member_session.connection()
    users = Table("users", MetaData(), autoload_with=connection)
    now = datetime(2026, 11, 21, 0, 0, 0)
    connection.execute(users.insert().values(
        public_id="00000000-0000-4000-8000-0000000000e1", email="old@example.com", email_canonical="old@example.com", display_name="舊版",
        role="user", member_tier="free", status="active", failed_login_count=0, token_version=1, content_tier_preference="safe",
        story_preference="all", created_at=now, updated_at=now,
    ))
    assert "keep_published_scripts" in users.c
    assert member_session.scalar(select(User.keep_published_scripts).where(User.email == "old@example.com")) is False


def test_the_unique_constraint_rejects_a_duplicate_pair_but_not_a_second_reporter(member_session):
    from app.repositories.script_repository import ScriptRepository
    from tests.support.script_data import validated

    users = [User(email=f"r{n}@example.com", email_canonical=f"r{n}@example.com", display_name=f"人{n}", status="active") for n in range(3)]
    member_session.add_all(users)
    member_session.flush()
    spec = validated()
    version = ScriptRepository(member_session).create_script(
        users[0].id, author_name="筆名", author_code="a1b2c3", content=spec.content, content_hash=spec.content_hash,
        total_chars=spec.total_chars,
    )
    for reporter in users[1:]:
        member_session.add(ScriptReport(version_id=version.id, reporter_id=reporter.id, reasons_json=["other"], red_line=False))
    member_session.flush()
    member_session.add(ScriptReport(version_id=version.id, reporter_id=users[1].id, reasons_json=["other"], red_line=False))
    with pytest.raises(IntegrityError):
        member_session.flush()
    member_session.rollback()
