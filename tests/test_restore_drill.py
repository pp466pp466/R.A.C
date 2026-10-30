"""deletion-replay 規格「還原演練可重複執行並驗證」：暫存資料庫名稱守門、日誌轉檔只留編號與時間、輸出沒有使用者資料。

演練本體需要 SQL Server 與伺服器可讀的備份檔，由本機驗收實跑（README「驗收紀錄」）；這裡驗證所有不需要資料庫的決策。
"""
import json

import pytest

from app.config import PROJECT_ROOT
from tests.support.scripts import load_script

drill = load_script("restore_drill")

GOOD_URL = "mssql+pyodbc://user:pw@localhost/project_test?driver=ODBC+Driver+18+for+SQL+Server"


# ---- 暫存資料庫名稱的守門 ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["project_restore_drill", "a_restore_drill", "My_DB_2_restore_drill"])
def test_scratch_names_are_accepted(name):
    drill.assert_scratch(name)


@pytest.mark.parametrize("name", [
    "", "project", "master", "restore_drill", "project_restore_drill_extra", "project_restore_drill; DROP DATABASE x --",
    "[project]_restore_drill", "pro ject_restore_drill", "project-restore_drill", "專案_restore_drill", "x" * 120 + "_restore_drill",
])
def test_anything_that_is_not_a_scratch_name_is_refused(name):
    with pytest.raises(drill.DrillError):
        drill.assert_scratch(name)


def test_the_target_may_not_be_the_connected_database():
    with pytest.raises(drill.DrillError):
        drill.assert_scratch("app_restore_drill", connected_database="app_restore_drill")
    with pytest.raises(drill.DrillError):
        drill.assert_scratch("app_restore_drill", connected_database="APP_RESTORE_DRILL")
    drill.assert_scratch("app_restore_drill", connected_database="app")


def test_the_scratch_name_always_ends_with_the_suffix():
    assert drill.scratch_name("project_test") == "project_test_restore_drill"


@pytest.fixture()
def never_restores(monkeypatch):
    calls = []
    monkeypatch.setattr(drill, "restore_scratch", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(drill, "drop_scratch_database", lambda *args, **kwargs: calls.append(args))
    return calls


@pytest.mark.parametrize("source", ["a]; DROP DATABASE x --", "bad name", "x'y", "專案", ""])
def test_the_drill_refuses_a_source_name_that_would_make_an_unsafe_target_and_restores_nothing(monkeypatch, never_restores, source):
    monkeypatch.setenv(drill.URL_ENV, GOOD_URL)
    messages: list[str] = []
    code = drill.run_drill("C:/backups/x.bak", source, None, False, messages.append)
    assert code == 2 and never_restores == []
    assert any("拒絕" in message for message in messages)


def test_the_drill_refuses_when_the_target_would_be_the_connected_database(monkeypatch, never_restores):
    monkeypatch.setenv(drill.URL_ENV, GOOD_URL.replace("/project_test?", "/app_restore_drill?"))
    messages: list[str] = []
    assert drill.run_drill("C:/backups/x.bak", "app", None, False, messages.append) == 2
    assert never_restores == [] and any("同名" in message for message in messages)


def test_the_drill_needs_the_connection_in_the_environment_never_on_the_command_line(monkeypatch, never_restores):
    monkeypatch.delenv(drill.URL_ENV, raising=False)
    messages: list[str] = []
    assert drill.run_drill("C:/backups/x.bak", "project_test", None, False, messages.append) == 1
    assert never_restores == [] and drill.URL_ENV in " ".join(messages)
    assert "--url" not in drill.build_parser().format_help() and "--password" not in drill.build_parser().format_help()


def test_the_restore_function_itself_refuses_a_non_scratch_target_before_connecting(monkeypatch):
    def no_engine(*args, **kwargs):
        raise AssertionError("不該連線")

    monkeypatch.setattr("sqlalchemy.create_engine", no_engine)
    with pytest.raises(drill.DrillError):
        drill.restore_scratch(GOOD_URL, "C:/backups/x.bak", "project_test")
    with pytest.raises(drill.DrillError):
        drill.drop_scratch_database(GOOD_URL, "project_test")


# ---- 日誌轉檔 ---------------------------------------------------------------------------

SECRET_EMAIL = "secret.person@example.com"
CLOUD_LOGGING = [
    {"timestamp": "2026-10-20T01:02:03.456Z", "severity": "INFO", "resource": {"labels": {"service_name": "cs-web-production"}},
     "jsonPayload": {"event": "account.deleted_by_request", "user_id": 12, "message": SECRET_EMAIL, "request_id": "abc", "path": "/api/x"}},
    {"timestamp": "2026-10-21T04:00:00+08:00", "jsonPayload": {"event": "account.inactive_purged", "user_id": 7, "extra": {"email": SECRET_EMAIL}}},
    {"timestamp": "2026-10-22T00:00:00Z", "jsonPayload": {"event": "account.exported", "user_id": 99}},       # 不是刪除事件
    {"timestamp": "2026-10-22T00:00:00Z", "jsonPayload": {"event": "account.deleted_by_request", "user_id": "12"}},   # 編號不是整數
    {"timestamp": "2026-10-22T00:00:00Z", "jsonPayload": {"event": "account.deleted_by_request", "user_id": True}},
    {"timestamp": "2026-10-22T00:00:00Z", "jsonPayload": {"event": "account.deleted_by_request", "user_id": 0}},
    {"timestamp": "not a time", "jsonPayload": {"event": "account.deleted_by_request", "user_id": 5}},
    {"jsonPayload": {"event": "account.deleted_by_request", "user_id": 5}},                                      # 沒有時間
]


def test_only_the_id_and_the_time_of_deletion_events_survive():
    pairs = drill.logs_to_ids(json.dumps(CLOUD_LOGGING))
    assert pairs == [(7, "2026-10-20T20:00:00+00:00"), (12, "2026-10-20T01:02:03.456000+00:00")]


def test_application_stdout_lines_are_understood_too_and_noise_lines_are_skipped():
    lines = "\n".join([
        "Starting gunicorn",
        json.dumps({"time": "2026-10-20T01:02:03+00:00", "level": "INFO", "event": "account.deleted_by_request", "user_id": 31, "request_id": "r"}),
        "",
        json.dumps({"time": "2026-10-20T01:02:03+00:00", "event": "retention.purged", "reason": "ledger", "count": 4}),
        "{broken json",
    ])
    assert drill.logs_to_ids(lines) == [(31, "2026-10-20T01:02:03+00:00")]


def test_duplicates_collapse_and_the_result_is_sorted_by_id():
    entry = lambda uid: {"timestamp": "2026-10-20T00:00:00Z", "jsonPayload": {"event": "account.deleted_by_request", "user_id": uid}}  # noqa: E731
    assert [uid for uid, _ in drill.logs_to_ids(json.dumps([entry(9), entry(3), entry(9)]))] == [3, 9]


def test_the_output_file_has_only_lines_of_id_and_time_utf8_without_bom_and_nothing_else(tmp_path):
    source, target = tmp_path / "logs.json", tmp_path / "ids.txt"
    source.write_text(json.dumps(CLOUD_LOGGING), encoding="utf-8")
    messages: list[str] = []
    assert drill.main(["logs-to-ids", "--input", str(source), "--output", str(target)], out=messages.append) == 0
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8") == "7,2026-10-20T20:00:00+00:00\n12,2026-10-20T01:02:03.456000+00:00\n"
    for secret in (SECRET_EMAIL, "request_id", "cs-web-production", "/api/x"):
        assert secret not in raw.decode("utf-8") and secret not in " ".join(messages)
    assert messages == ["已取出 2 筆「使用者編號,刪除時間」（其餘欄位全部丟棄）。"]


def test_the_file_it_writes_is_accepted_by_the_replay_command_parser(tmp_path):
    from app.services.deletion_replay_service import parse_replay_file

    source, target = tmp_path / "logs.json", tmp_path / "ids.txt"
    source.write_text(json.dumps(CLOUD_LOGGING), encoding="utf-8-sig")
    drill.main(["logs-to-ids", "--input", str(source), "--output", str(target)], out=lambda text: None)
    assert [(r.user_id, r.deleted_at.tzinfo is not None) for r in parse_replay_file(target.read_text(encoding="utf-8-sig"))] == [(7, True), (12, True)]


def test_an_unreadable_or_invalid_log_fails_without_echoing_the_path_or_content(tmp_path):
    messages: list[str] = []
    assert drill.main(["logs-to-ids", "--input", str(tmp_path / "secret-folder" / "missing.json"), "--output", str(tmp_path / "o.txt")], out=messages.append) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("[ not json", encoding="utf-8")
    assert drill.main(["logs-to-ids", "--input", str(bad), "--output", str(tmp_path / "o.txt")], out=messages.append) == 1
    assert "secret-folder" not in " ".join(messages) and "not json" not in " ".join(messages)
    assert not (tmp_path / "o.txt").exists()


# ---- 腳本不在應用程式裡 -----------------------------------------------------------------


def test_the_restore_tooling_lives_in_scripts_and_never_in_the_application_package():
    assert (PROJECT_ROOT / "scripts" / "restore_drill.py").is_file()
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        assert "RESTORE DATABASE" not in text and "restore_drill" not in text, path.name
