"""rpg-sessions 規格：開存檔（誰可以用哪個版本、內容模式的取捨、複本與來源一致）與開始回合的錯誤對應。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import MemoryLog, RpgSession, ScriptVersion
from app.models.script import VERSION_PUBLISHED
from app.models.user import ROLE_ADMIN
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.services.access_policy import AccessPolicy
from app.services.content_tier import ADULT_RULES_VERSION
from app.services.current_user import CurrentUser
from app.services.errors import (
    AgeConfirmationRequired, Conflict, ContentTierRestricted, NotFound, SessionEnded, ValidationError,
)
from app.services.session_service import SessionService
from tests.support.age import TODAY_UTC
from tests.support.auth import FakeClock, add_user
from tests.support.protagonist import start_kwargs
from tests.support.review import PROTAGONIST_MARKER, PassingProtagonistReviews, PassingReviews
from tests.support.script_data import validated

GHOST = "00000000-0000-4000-8000-000000000000"


def service_for(session, adult_mode=False, reviews=None, protagonist_reviews=None):
    clock = FakeClock()
    return SessionService(
        scripts=ScriptRepository(session), sessions=SessionRepository(session),
        policy=AccessPolicy(clock=clock, adult_mode_enabled=adult_mode), reviews=reviews or PassingReviews(),
        protagonist_reviews=protagonist_reviews or PassingProtagonistReviews(), protagonist_marker=PROTAGONIST_MARKER, clock=clock,
    ), clock


def player(session, n=1, **overrides) -> CurrentUser:
    return CurrentUser.from_model(add_user(session, n, **overrides))


def publish_script(session, owner=None, **changes):
    """上架一個劇本；`owner` 為空時是內建劇本。回傳版本。"""
    repo = ScriptRepository(session)
    result = validated(**changes)
    if owner is None:
        version, _ = repo.publish_builtin(f"seed-{changes.get('title', 'x')}", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=TODAY_UTC)
        return version
    version = repo.create_script(owner.id, author_name="甲", author_code="aaaaaa", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars)
    repo.submit_for_review(owner.id, version.public_id)
    return repo.approve_version(version.public_id, now=TODAY_UTC)


def draft_script(session, owner, **changes):
    result = validated(**changes)
    return ScriptRepository(session).create_script(
        owner.id, author_name="甲", author_code="aaaaaa", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars
    )


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


def start(service, user, version, **overrides):
    """用 `start_kwargs()` 的預設主角（旅人、18 歲、明確略過錨定事實）開存檔；`overrides` 覆蓋 `name`／`age` 等角色欄位。"""
    return service.start_session(user, version.public_id, **start_kwargs(**overrides))


# ---- 誰可以用哪個版本 -------------------------------------------------------------------------

def test_an_adult_starts_a_save_from_a_published_builtin_script(member_session):
    service, clock = service_for(member_session)
    user = player(member_session)
    version = publish_script(member_session)
    save = start(service, user, version)
    assert (save.status, save.version, save.user_id, save.source_version_id) == ("active", 1, user.id, version.id)
    assert (save.player_name, save.character_age, save.last_active_at) == ("旅人", 18, clock.now)


def test_a_player_who_has_not_confirmed_the_age_gets_the_age_error_and_no_save(member_session):
    service, _ = service_for(member_session)
    version = publish_script(member_session)
    for user in (player(member_session, 1, age=None), player(member_session, 2, age=None, role=ROLE_ADMIN)):
        with pytest.raises(AgeConfirmationRequired) as caught:
            start(service, user, version)
        assert caught.value.code == "age_confirmation_required"
    assert count(member_session, RpgSession) == 0


def test_the_owner_may_start_from_their_own_draft_but_another_player_gets_a_404(member_session):
    service, _ = service_for(member_session)
    author, other = player(member_session, 1), player(member_session, 2)
    draft = draft_script(member_session, author)
    assert start(service, author, draft).source_version_id == draft.id
    with pytest.raises(NotFound) as for_draft:
        start(service, other, draft)
    with pytest.raises(NotFound) as for_missing:
        service.start_session(other, GHOST, **start_kwargs())
    assert (for_draft.value.code, for_draft.value.message) == (for_missing.value.code, for_missing.value.message)
    assert count(member_session, RpgSession) == 1


@pytest.mark.parametrize("status_label", ["pending", "rejected", "superseded"])
def test_another_player_cannot_start_from_any_non_published_version(member_session, status_label):
    service, _ = service_for(member_session)
    author, other = player(member_session, 1), player(member_session, 2)
    repo = ScriptRepository(member_session)
    version = draft_script(member_session, author)
    if status_label == "pending":
        repo.submit_for_review(author.id, version.public_id)
    elif status_label == "rejected":
        repo.submit_for_review(author.id, version.public_id)
        repo.reject_version(version.public_id)
    else:
        repo.submit_for_review(author.id, version.public_id)
        repo.approve_version(version.public_id, now=TODAY_UTC)
        second = repo.create_draft_version(
            author.id, repo.get_script_of(version).public_id, content=validated(title="第二版").content, content_hash="b" * 64, total_chars=10,
        )
        repo.submit_for_review(author.id, second.public_id)
        repo.approve_version(second.public_id, now=TODAY_UTC)
    member_session.commit()
    with pytest.raises(NotFound):
        start(service, other, version)


def test_a_non_string_version_id_is_a_404_not_an_error(member_session):
    service, _ = service_for(member_session)
    user = player(member_session)
    for bad in (None, 7, ["x"]):
        with pytest.raises(NotFound):
            service.start_session(user, bad, **start_kwargs())


# ---- 內容模式 -----------------------------------------------------------------------------

def test_a_safe_mode_player_cannot_start_a_script_declared_standard(member_session):
    service, _ = service_for(member_session)
    version = publish_script(member_session, content_tier="standard")
    safe_adult = player(member_session, 1, content_tier_preference="safe")
    teenager = player(member_session, 2, age=15)
    for user in (safe_adult, teenager):
        with pytest.raises(ContentTierRestricted) as caught:
            start(service, user, version)
        assert caught.value.code == "content_tier_restricted"
    assert count(member_session, RpgSession) == 0


def test_a_script_declared_at_or_below_the_effective_mode_can_be_started(member_session):
    service, _ = service_for(member_session)
    standard_adult = player(member_session, 1)                       # 確認時已成年：偏好 standard
    assert start(service, standard_adult, publish_script(member_session, title="一般", content_tier="standard")).content_tier == "standard"
    assert start(service, standard_adult, publish_script(member_session, title="安全", content_tier="safe")).content_tier == "safe"


def test_an_adult_declared_script_needs_the_adult_mode_to_be_active(member_session):
    adult_script = publish_script(member_session, content_tier="adult")
    off, _ = service_for(member_session, adult_mode=False)
    with pytest.raises(ContentTierRestricted):
        start(off, player(member_session, 1, content_tier_preference="adult", adult_confirmed_at=TODAY_UTC, adult_rules_version=ADULT_RULES_VERSION), adult_script)
    on, _ = service_for(member_session, adult_mode=True)
    with pytest.raises(ContentTierRestricted):                       # 開關開了但尚未確認
        start(on, player(member_session, 2, content_tier_preference="adult"), adult_script)
    confirmed = player(member_session, 3, content_tier_preference="adult", adult_confirmed_at=TODAY_UTC, adult_rules_version=ADULT_RULES_VERSION)
    assert start(on, confirmed, adult_script).content_tier == "adult"


def test_a_fifteen_year_old_can_play_a_safe_builtin_script(member_session):
    service, _ = service_for(member_session)
    save = start(service, player(member_session, age=15), publish_script(member_session, content_tier="safe"))
    assert save.content_tier == "safe"


def test_the_save_takes_the_more_conservative_of_the_player_mode_and_the_script_declaration(member_session):
    service, _ = service_for(member_session, adult_mode=True)
    confirmed = dict(adult_confirmed_at=TODAY_UTC, adult_rules_version=ADULT_RULES_VERSION)
    adult_player = player(member_session, 1, content_tier_preference="adult", **confirmed)
    standard_player = player(member_session, 2)
    assert start(service, standard_player, publish_script(member_session, title="安全", content_tier="safe")).content_tier == "safe"
    assert start(service, adult_player, publish_script(member_session, title="一般", content_tier="standard")).content_tier == "standard"
    assert start(service, adult_player, publish_script(member_session, title="成人", content_tier="adult")).content_tier == "adult"


# ---- 複本 -----------------------------------------------------------------------------------

def test_the_copy_in_the_save_equals_the_source_version(member_session):
    service, _ = service_for(member_session)
    repo = ScriptRepository(member_session)
    version = publish_script(member_session, tags=["仙俠", "Fantasy"], ending_mode="custom", ending_text="壽終。", ending_age=80)
    save = start(service, player(member_session), version)
    member_session.commit()
    member_session.expire_all()
    stored = member_session.get(RpgSession, save.id)
    assert stored.script_snapshot_json == repo.content_of(version) and stored.snapshot_spec_version == version.spec_version


def test_a_later_version_replacing_the_source_leaves_the_save_untouched(member_session):
    service, _ = service_for(member_session)
    author, reader = player(member_session, 1), player(member_session, 2)
    first = publish_script(member_session, author, title="第一版")
    save = start(service, reader, first)
    repo = ScriptRepository(member_session)
    second = repo.create_draft_version(
        author.id, repo.get_script_of(first).public_id, content=validated(title="第二版").content, content_hash="c" * 64, total_chars=10
    )
    repo.submit_for_review(author.id, second.public_id)
    repo.approve_version(second.public_id, now=TODAY_UTC)
    member_session.commit()
    member_session.expire_all()
    assert member_session.get(ScriptVersion, first.id).status == "superseded"
    stored = member_session.get(RpgSession, save.id)
    assert stored.script_snapshot_json["title"] == "第一版" and stored.source_version_id == first.id


def test_deleting_the_source_leaves_the_save_with_its_copy_and_a_null_reference(member_session):
    service, _ = service_for(member_session)
    author, reader = player(member_session, 1), player(member_session, 2)
    draft = draft_script(member_session, author, title="將被刪除的草稿")
    save = start(service, author, draft)
    other_save = start(service, author, draft)
    assert ScriptRepository(member_session).delete_draft(author.id, draft.public_id) is True
    member_session.commit()
    member_session.expire_all()
    for saved in (save, other_save):
        kept = member_session.get(RpgSession, saved.id)
        assert kept.source_version_id is None and kept.script_snapshot_json["title"] == "將被刪除的草稿"
    assert reader.id != author.id


def test_a_new_save_starts_with_an_empty_notebook_even_when_another_save_has_one(member_session):
    service, _ = service_for(member_session)
    user = player(member_session)
    version = publish_script(member_session)
    first = start(service, user, version)
    first.notebook_text = "已有內容的記事簿"
    member_session.commit()
    assert start(service, user, version).notebook_text == ""


# ---- 主角的資料 -----------------------------------------------------------------------------

def test_the_protagonist_name_is_normalised_and_the_age_is_a_character_age_not_the_players(member_session):
    service, _ = service_for(member_session)
    teenager = player(member_session, age=15)
    save = start(service, teenager, publish_script(member_session), name="  旅\n  人 ", age=3)
    assert (save.player_name, save.character_age) == ("旅 人", 3)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"name": ""}, "protagonist.name"), ({"name": "   "}, "protagonist.name"), ({"name": None}, "protagonist.name"),
        ({"name": "字" * 21}, "protagonist.name"), ({"name": 5}, "protagonist.name"),
        ({"age": "十八"}, "protagonist.age"), ({"age": 18.5}, "protagonist.age"), ({"age": True}, "protagonist.age"),
        ({"age": -1}, "protagonist.age"), ({"age": 10000}, "protagonist.age"), ({"age": None}, "protagonist.age"),
        ({"appearance": ...}, "protagonist.appearance"), ({"personality": "字" * 61}, "protagonist.personality"),
        ({"image": "https://example.com/a.png"}, "protagonist.image"),
    ],
)
def test_an_invalid_protagonist_is_a_400_naming_the_field_and_no_save_is_created(member_session, overrides, field):
    service, _ = service_for(member_session)
    version = publish_script(member_session)
    with pytest.raises(ValidationError) as caught:
        start(service, player(member_session), version, **overrides)
    assert field in caught.value.fields
    assert count(member_session, RpgSession) == 0


# ---- 開始回合 -------------------------------------------------------------------------------

def test_beginning_a_turn_creates_a_streaming_turn_and_resending_a_completed_one_replays_it(member_session):
    service, clock = service_for(member_session)
    user = player(member_session)
    save = start(service, user, publish_script(member_session))
    started = service.begin_turn(user, save.public_id, client_msg_id="m-1", user_action="我走進山門")
    assert (started.outcome, started.turn.turn_status, started.turn.seq) == ("created", "streaming", 1)
    SessionRepository(member_session).complete_turn(user.id, save.public_id, "m-1", llm_raw_response="晨霧散開。", structured_result=None, now=clock.now)
    assert service.begin_turn(user, save.public_id, client_msg_id="m-1", user_action="x").outcome == "replayed"
    assert count(member_session, MemoryLog) == 1


def test_a_turn_in_an_ended_save_is_a_409_with_the_session_ended_code(member_session):
    service, clock = service_for(member_session)
    user = player(member_session)
    save = start(service, user, publish_script(member_session))
    SessionRepository(member_session).end_session(user.id, save.public_id, now=clock.now)
    with pytest.raises(SessionEnded) as caught:
        service.begin_turn(user, save.public_id, client_msg_id="m-1", user_action="想繼續")
    assert caught.value.code == "session_ended" and isinstance(caught.value, Conflict)
    assert count(member_session, MemoryLog) == 0


def test_resending_a_turn_that_is_still_streaming_is_a_409(member_session):
    service, _ = service_for(member_session)
    user = player(member_session)
    save = start(service, user, publish_script(member_session))
    service.begin_turn(user, save.public_id, client_msg_id="m-1", user_action="a")
    with pytest.raises(Conflict) as caught:
        service.begin_turn(user, save.public_id, client_msg_id="m-1", user_action="a")
    assert caught.value.code == "turn_in_progress"


def test_another_player_or_a_missing_save_is_a_404_for_a_turn(member_session):
    service, _ = service_for(member_session)
    owner, other = player(member_session, 1), player(member_session, 2)
    save = start(service, owner, publish_script(member_session))
    for user, public_id in ((other, save.public_id), (owner, GHOST)):
        with pytest.raises(NotFound):
            service.begin_turn(user, public_id, client_msg_id="m-1", user_action="a")
    assert count(member_session, MemoryLog) == 0


def test_a_turn_needs_the_age_gate_and_valid_ids(member_session):
    service, _ = service_for(member_session)
    owner = player(member_session, 1)
    save = start(service, owner, publish_script(member_session))
    with pytest.raises(AgeConfirmationRequired):
        service.begin_turn(player(member_session, 2, age=None, role=ROLE_ADMIN), save.public_id, client_msg_id="m-1", user_action="a")
    for bad_id in ("", "x" * 65, None, 5):
        with pytest.raises(ValidationError) as caught:
            service.begin_turn(owner, save.public_id, client_msg_id=bad_id, user_action="a")
        assert "client_msg_id" in caught.value.fields
    with pytest.raises(ValidationError):
        service.begin_turn(owner, save.public_id, client_msg_id="m-1", user_action=None)
    assert count(member_session, MemoryLog) == 0
