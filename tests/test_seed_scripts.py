"""builtin-script-seed 規格：`flask seed-scripts`——冪等建立、內容改變時新增版本、預覽、全有或全無、輸出不含內容、首發劇本。

服務層的測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；指令層用測試應用程式與 CLI runner。
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import cli, create_app
from app.config import load_config
from app.models import Base, RpgSession, Script, ScriptVersion
from app.models.script import OFFICIAL_AUTHOR_NAME, VERSION_PUBLISHED, VERSION_SUPERSEDED
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.script_seed_service import (
    BUILTIN_MAX_TIER, SEED_CREATED, SEED_DIRECTORY, ScriptSeedService, SeedDataError,
)
from app.services.script_spec import TOTAL_CHARS_MAX, validate_script
from app.services.session_service import SessionService
from tests.support.auth import FakeClock, add_user
from tests.support.protagonist import start_kwargs
from tests.support.review import PROTAGONIST_MARKER, PassingProtagonistReviews, PassingReviews
from tests.support.script_data import valid

SECRET_TEXT = "只該出現在資料檔裡的劇本文字SEED-SECRET"


def write_seed(directory: Path, name: str, seed_key: str | None = None, **changes) -> Path:
    """寫一份種子資料檔（UTF-8，無 BOM）；`seed_key` 預設取檔名。"""
    content = valid(**changes)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"seed_key": seed_key or name, "content": content}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def make_service(session, directory, clock=None, markers=()):
    clock = clock or FakeClock()
    return ScriptSeedService(
        scripts=ScriptRepository(session), clock=clock, commit=session.commit, rollback=session.rollback, directory=directory,
        forbidden_markers=markers,
    ), clock


def snapshot_of(session):
    session.expire_all()
    scripts = [(s.id, s.seed_key, s.author_name, s.author_locked_at, s.updated_at) for s in session.scalars(select(Script).order_by(Script.id))]
    versions = [(v.id, v.version_no, v.status, v.content_hash, v.updated_at) for v in session.scalars(select(ScriptVersion).order_by(ScriptVersion.id))]
    return scripts, versions


@pytest.fixture()
def seeds(tmp_path):
    directory = tmp_path / "seeds"
    directory.mkdir()
    return directory


# ---- 第一次執行與重複執行 -------------------------------------------------------------------

def test_the_first_run_creates_a_script_without_an_author_and_a_published_first_version(member_session, seeds):
    write_seed(seeds, "alpha")
    service, clock = make_service(member_session, seeds)
    report = service.run()
    assert [(r.seed_key, r.action) for r in report.results] == [("alpha", SEED_CREATED)] and (report.created, report.updated, report.unchanged) == (1, 0, 0)
    script = member_session.scalar(select(Script).where(Script.seed_key == "alpha"))
    (version,) = member_session.scalars(select(ScriptVersion).where(ScriptVersion.script_id == script.id))
    assert (script.creator_id, script.author_name, script.author_locked_at) == (None, OFFICIAL_AUTHOR_NAME, clock.now)
    assert (version.version_no, version.status, version.live_script_id) == (1, VERSION_PUBLISHED, script.id)


def test_running_again_changes_nothing_at_all(member_session, seeds):
    write_seed(seeds, "alpha")
    write_seed(seeds, "beta")
    service, clock = make_service(member_session, seeds)
    service.run()
    before = snapshot_of(member_session)
    clock.advance(days=3)
    for _ in range(2):
        report = service.run()
        assert (report.created, report.updated, report.unchanged) == (0, 0, 2)
    assert snapshot_of(member_session) == before


def test_the_preview_reports_what_would_happen_and_writes_nothing(member_session, seeds):
    write_seed(seeds, "alpha")
    service, _ = make_service(member_session, seeds)
    report = service.run(dry_run=True)
    assert report.dry_run is True and (report.created, report.updated, report.unchanged) == (1, 0, 0)
    assert member_session.scalar(select(func.count()).select_from(Script)) == 0
    service.run()
    write_seed(seeds, "alpha", title="改過的標題")
    again = service.run(dry_run=True)
    assert (again.created, again.updated, again.unchanged) == (0, 1, 0)
    assert member_session.scalar(select(func.count()).select_from(ScriptVersion)) == 1


# ---- 內容改變 ---------------------------------------------------------------------------------

def test_a_changed_file_adds_a_second_published_version_and_supersedes_the_first_keeping_its_content(member_session, seeds):
    write_seed(seeds, "alpha")
    service, _ = make_service(member_session, seeds)
    service.run()
    write_seed(seeds, "alpha", background="改過的一段背景文字")
    report = service.run()
    assert (report.created, report.updated, report.unchanged) == (0, 1, 0)
    script = member_session.scalar(select(Script).where(Script.seed_key == "alpha"))
    versions = list(member_session.scalars(select(ScriptVersion).where(ScriptVersion.script_id == script.id).order_by(ScriptVersion.version_no)))
    assert [(v.version_no, v.status) for v in versions] == [(1, VERSION_SUPERSEDED), (2, VERSION_PUBLISHED)]
    assert versions[0].background.startswith("靈氣復甦") and versions[1].background == "改過的一段背景文字"
    assert member_session.scalar(select(func.count()).select_from(Script)) == 1


def test_only_layout_differences_count_as_no_change(member_session, seeds):
    write_seed(seeds, "alpha")
    service, _ = make_service(member_session, seeds)
    service.run()
    write_seed(seeds, "alpha", title="  青雲問道\n", summary="少年入宗門，一步步走向自己的道。  ", background="\n\n靈氣復甦的年代，宗門林立。\r\n凡人亦可修行。  \n")
    report = service.run()
    assert (report.created, report.updated, report.unchanged) == (0, 0, 1)
    assert member_session.scalar(select(func.count()).select_from(ScriptVersion)) == 1


def test_a_save_opened_from_the_first_version_keeps_its_content_after_the_seed_is_updated(member_session, seeds):
    write_seed(seeds, "alpha")
    service, clock = make_service(member_session, seeds)
    service.run()
    player = CurrentUser.from_model(add_user(member_session, 1))
    sessions = SessionService(
        scripts=ScriptRepository(member_session), sessions=SessionRepository(member_session), policy=AccessPolicy(clock=clock),
        reviews=PassingReviews(), protagonist_reviews=PassingProtagonistReviews(), protagonist_marker=PROTAGONIST_MARKER, clock=clock,
    )
    first = member_session.scalar(select(ScriptVersion))
    save = sessions.start_session(player, first.public_id, **start_kwargs())
    write_seed(seeds, "alpha", title="第二版標題")
    service.run()
    member_session.expire_all()
    assert member_session.get(RpgSession, save.id).script_snapshot_json["title"] == "青雲問道"
    assert member_session.scalar(select(func.count()).select_from(ScriptVersion)) == 2


# ---- 資料不合規：整批失敗、不寫入、不回顯 ---------------------------------------------------------

def test_one_invalid_file_fails_the_whole_batch_names_the_file_and_the_path_and_writes_nothing(member_session, seeds):
    write_seed(seeds, "alpha")
    write_seed(seeds, "broken", summary=SECRET_TEXT * 10)
    service, _ = make_service(member_session, seeds)
    with pytest.raises(SeedDataError) as caught:
        service.run()
    problems = "\n".join(caught.value.problems)
    assert "broken.json" in problems and "content.summary" in problems and "alpha.json" not in problems
    assert SECRET_TEXT not in problems
    assert member_session.scalar(select(func.count()).select_from(Script)) == 0 and member_session.scalar(select(func.count()).select_from(ScriptVersion)) == 0


def test_all_problems_are_reported_together_not_just_the_first(member_session, seeds):
    write_seed(seeds, "one", title="", summary=SECRET_TEXT * 10)
    (seeds / "two.json").write_text("{ 不是 JSON", encoding="utf-8")
    service, _ = make_service(member_session, seeds)
    with pytest.raises(SeedDataError) as caught:
        service.load()
    joined = "\n".join(caught.value.problems)
    assert "one.json：content.title" in joined and "one.json：content.summary" in joined and "two.json：不是合法的 JSON" in joined
    assert SECRET_TEXT not in joined


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('[1, 2]', "最外層必須是物件"),
        ('{"content": {}}', "seed_key"),
        ('{"seed_key": "Has Space", "content": {}}', "seed_key"),
        ('{"seed_key": "ok", "content": {}, "extra": 1}', "extra"),
        ('{"seed_key": "ok"}', "content"),
    ],
)
def test_malformed_seed_files_are_reported_by_file_name(member_session, seeds, payload, expected):
    (seeds / "bad.json").write_text(payload, encoding="utf-8")
    service, _ = make_service(member_session, seeds)
    with pytest.raises(SeedDataError) as caught:
        service.load()
    assert any(p.startswith("bad.json：") and expected in p for p in caught.value.problems), caught.value.problems


def test_a_duplicate_seed_key_across_files_is_rejected(member_session, seeds):
    write_seed(seeds, "alpha", seed_key="same")
    write_seed(seeds, "beta", seed_key="same")
    service, _ = make_service(member_session, seeds)
    with pytest.raises(SeedDataError) as caught:
        service.load()
    assert any("beta.json" in p and "重複" in p for p in caught.value.problems)


def test_a_missing_or_empty_directory_is_an_error_because_it_means_the_files_were_not_packaged(member_session, seeds, tmp_path):
    for directory in (seeds, tmp_path / "nowhere"):
        service, _ = make_service(member_session, directory)
        with pytest.raises(SeedDataError):
            service.run()


def test_the_declared_mode_must_stay_within_what_a_builtin_script_may_declare(member_session, seeds):
    assert BUILTIN_MAX_TIER == "standard"
    write_seed(seeds, "fine", content_tier="standard")
    write_seed(seeds, "too-open", content_tier="adult")
    service, _ = make_service(member_session, seeds)
    with pytest.raises(SeedDataError) as caught:
        service.run()
    assert [p for p in caught.value.problems if "content_tier" in p] and not any("fine.json" in p for p in caught.value.problems)
    assert member_session.scalar(select(func.count()).select_from(Script)) == 0


def test_a_secret_marker_in_a_seed_file_is_rejected_without_being_shown(member_session, seeds):
    marker = "MARKER-7f3a9c2b"
    write_seed(seeds, "leaky", background=f"前文{marker}後文")
    service, _ = make_service(member_session, seeds, markers=(marker,))
    with pytest.raises(SeedDataError) as caught:
        service.load()
    assert any("content.background" in p for p in caught.value.problems) and marker not in "\n".join(caught.value.problems)


def test_a_file_with_a_byte_order_mark_is_read_correctly_and_bad_encodings_are_reported(member_session, seeds):
    path = write_seed(seeds, "bom")
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    (seeds / "latin.json").write_bytes('{"seed_key": "latin", "content": "é"}'.encode("cp950", errors="ignore") + b"\xff\xfe")
    service, _ = make_service(member_session, seeds)
    with pytest.raises(SeedDataError) as caught:
        service.load()
    assert any(p.startswith("latin.json：") for p in caught.value.problems) and not any(p.startswith("bom.json") for p in caught.value.problems)


# ---- 首發的古風仙俠劇本 -----------------------------------------------------------------------------

def shipped() -> dict:
    path = SEED_DIRECTORY / "xianxia-qingyun.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_the_shipped_files_are_utf8_without_a_bom_and_pass_the_fixed_spec():
    files = sorted(SEED_DIRECTORY.glob("*.json"))
    assert [f.name for f in files] == ["xianxia-qingyun.json"]
    for path in files:
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        raw.decode("utf-8")
    result = validate_script(shipped()["content"], max_tier="safe")
    assert result.total_chars <= TOTAL_CHARS_MAX


def test_the_shipped_script_is_safe_has_no_ending_and_has_the_required_structure():
    content = validate_script(shipped()["content"], max_tier="safe").content
    assert content["content_tier"] == "safe" and content["ending_mode"] == "none"
    assert len(content["characters"]) >= 3 and any(c["hidden_trait"] for c in content["characters"])
    assert len(content["locations"]) >= 3
    kinds = {m["trigger"]["kind"] for m in content["milestones"]}
    assert kinds == {"age", "year"} and content["calendar"]["name"] == "乾歷朝"
    assert any(m["trigger"] == {"kind": "age", "value": 20} for m in content["milestones"])
    assert all(c["image"] is None for c in content["characters"]) and content["cover_image"] is None


def test_the_shipped_script_has_no_real_personal_data_or_secret_markers():
    text = (SEED_DIRECTORY / "xianxia-qingyun.json").read_text(encoding="utf-8")
    for pattern in ("@", "http://", "https://", "ZZ", "SECRET"):
        assert pattern not in text


def test_a_fifteen_year_old_can_start_a_save_from_the_shipped_script(member_session):
    service, clock = make_service(member_session, SEED_DIRECTORY)
    service.run()
    teen = CurrentUser.from_model(add_user(member_session, 1, age=15))
    sessions = SessionService(
        scripts=ScriptRepository(member_session), sessions=SessionRepository(member_session), policy=AccessPolicy(clock=clock),
        reviews=PassingReviews(), protagonist_reviews=PassingProtagonistReviews(), protagonist_marker=PROTAGONIST_MARKER, clock=clock,
    )
    version = member_session.scalar(select(ScriptVersion).where(ScriptVersion.status == VERSION_PUBLISHED))
    save = sessions.start_session(teen, version.public_id, **start_kwargs(age=16))
    assert save.content_tier == "safe" and save.script_snapshot_json["title"] == "青雲問道"


def test_running_the_shipped_seed_twice_is_idempotent(member_session):
    service, _ = make_service(member_session, SEED_DIRECTORY)
    assert service.run().created == 1
    again = service.run()
    assert (again.created, again.updated, again.unchanged) == (0, 0, 1)


# ---- 指令 ---------------------------------------------------------------------------------------

@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def counts(app) -> tuple[int, int]:
    with Session(app.extensions["db_engine"]) as s:
        return s.scalar(select(func.count()).select_from(Script)), s.scalar(select(func.count()).select_from(ScriptVersion))


def test_the_command_is_registered_documented_and_classified_as_manual(app):
    assert "seed-scripts" in app.cli.commands and "seed-scripts" in (cli.__doc__ or "")
    assert "seed-scripts" in cli.MANUAL_COMMANDS and "seed-scripts" not in cli.SCHEDULED_COMMANDS


def test_creating_the_application_seeds_nothing(app):
    assert counts(app) == (0, 0)


def test_the_command_creates_the_shipped_script_then_reports_no_change(app):
    runner = app.test_cli_runner()
    first = runner.invoke(args=["seed-scripts"])
    assert first.exit_code == 0, first.output
    assert "建立 1、更新 0、沒有變動 0" in first.output and "xianxia-qingyun：建立" in first.output
    assert counts(app) == (1, 1)
    second = runner.invoke(args=["seed-scripts"])
    assert second.exit_code == 0 and "建立 0、更新 0、沒有變動 1" in second.output and "xianxia-qingyun：沒有變動" in second.output
    assert counts(app) == (1, 1)


def test_the_command_preview_changes_nothing(app):
    result = app.test_cli_runner().invoke(args=["seed-scripts", "--dry-run"])
    assert result.exit_code == 0 and "預覽" in result.output and "建立 1" in result.output
    assert counts(app) == (0, 0)


def test_the_output_contains_only_seed_keys_and_counts_never_the_script_text(app):
    result = app.test_cli_runner().invoke(args=["seed-scripts"])
    content = shipped()["content"]
    for text in (content["title"], content["summary"], content["background"][:20], content["opening"][:20], content["characters"][0]["name"]):
        assert text not in result.output


def test_a_failing_validation_exits_non_zero_names_the_file_and_the_path_and_changes_nothing(app, tmp_path, monkeypatch):
    directory = tmp_path / "seeds"
    directory.mkdir()
    write_seed(directory, "alpha")
    write_seed(directory, "broken", summary=SECRET_TEXT * 10)
    monkeypatch.setattr("app.services.registry.SEED_DIRECTORY", directory)
    result = app.test_cli_runner().invoke(args=["seed-scripts"])
    assert result.exit_code != 0
    assert "broken.json" in result.output and "content.summary" in result.output and "沒有寫入任何資料" in result.output
    assert SECRET_TEXT not in result.output and counts(app) == (0, 0)


def test_the_command_runs_from_a_changed_directory_and_updates_when_the_content_changes(app, tmp_path, monkeypatch):
    directory = tmp_path / "seeds"
    directory.mkdir()
    write_seed(directory, "alpha")
    monkeypatch.setattr("app.services.registry.SEED_DIRECTORY", directory)
    runner = app.test_cli_runner()
    assert runner.invoke(args=["seed-scripts"]).exit_code == 0
    write_seed(directory, "alpha", title="第二版")
    result = runner.invoke(args=["seed-scripts"])
    assert "建立 0、更新 1、沒有變動 0" in result.output and "alpha：更新（新增版本）" in result.output
    assert counts(app) == (1, 2)
