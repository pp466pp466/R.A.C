"""rpg-entry-api 規格：RPG 入口的唯讀資料（可玩劇本清單、我的存檔清單、存檔摘要）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。這裡直接測 Service（Controller 與端點由 B 批測）。
"""
import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.models.user import ROLE_ADMIN
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.services.access_policy import AccessPolicy
from app.services.content_mode_service import state_of
from app.services.content_tier import ADULT_RULES_VERSION
from app.services.current_user import CurrentUser
from app.services.errors import NotFound, ValidationError
from app.services.registry import get_game_entry_service
from app.services.game_entry_service import (
    CARD_FIELDS, CATALOG_DEFAULT_LIMIT, CATALOG_MAX_LIMIT, OFFSET_MAX, SAVE_FIELDS, SESSIONS_DEFAULT_LIMIT, SESSIONS_MAX_LIMIT,
    SUMMARY_FIELDS, GameEntryService,
)
from tests.support.age import TODAY_UTC
from tests.support.auth import FakeClock, add_user
from tests.support.protagonist import protagonist
from tests.support.query_counter import count_statements
from tests.support.script_data import validated
from tests.test_llm_providers_by_purpose import GEMINI_KEY, make_app, request_scope

GHOST = "00000000-0000-4000-8000-000000000000"


def service_for(session, adult_mode=False) -> GameEntryService:
    return GameEntryService(
        scripts=ScriptRepository(session), sessions=SessionRepository(session),
        policy=AccessPolicy(clock=FakeClock(), adult_mode_enabled=adult_mode),
    )


def player(session, n=1, **overrides) -> CurrentUser:
    return CurrentUser.from_model(add_user(session, n, **overrides))


def publish(session, owner=None, now=TODAY_UTC, **changes):
    """上架一個劇本版本；`owner` 為空時是內建劇本。回傳版本。"""
    repo, result = ScriptRepository(session), validated(**changes)
    if owner is None:
        version, _ = repo.publish_builtin(
            f"seed-{changes.get('title', 'x')}", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=now,
        )
        return version
    version = repo.create_script(
        owner.id, author_name="甲", author_code="aaaaaa", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars,
    )
    repo.submit_for_review(owner.id, version.public_id)
    return repo.approve_version(version.public_id, now=now)


def titles(page) -> list[str]:
    return [card["title"] for card in page["scripts"]]


def open_save(session, user, title="青雲問道", now=TODAY_UTC, **overrides):
    values = dict(
        source_version_id=None, snapshot=validated(title=title).content, snapshot_spec_version=1, protagonist=protagonist("林初"),
        content_tier="safe", now=now,
    )
    values.update(overrides)
    return SessionRepository(session).create(user.id, **values)


def finish_turns(session, user, save, count, prefix="t"):
    repo = SessionRepository(session)
    for index in range(1, count + 1):
        repo.start_turn(user.id, save.public_id, client_msg_id=f"{prefix}-{index}", user_action=f"行動{index}", now=TODAY_UTC)
        repo.complete_turn(user.id, save.public_id, f"{prefix}-{index}", llm_raw_response=f"劇情{index}", structured_result=None, now=TODAY_UTC)


def read_all(fetch, limit):
    """依序讀取每一頁直到 `has_more` 為假，回傳各頁串起來的結果。"""
    collected, offset = [], 0
    while True:
        page = fetch(limit=limit, offset=offset)
        collected += page["scripts" if "scripts" in page else "sessions"]
        if not page["has_more"]:
            return collected
        offset += limit


# ---- 可玩劇本清單：範圍 ----------------------------------------------------------------------

def test_the_catalog_lists_only_published_versions_never_drafts_pending_rejected_or_superseded_ones(member_session):
    service, repo = service_for(member_session), ScriptRepository(member_session)
    author, reader = player(member_session, 1), player(member_session, 2)
    publish(member_session, author, title="公開版")
    for title in ("草稿版", "待審版", "退回版"):
        result = validated(title=title)
        version = repo.create_script(author.id, author_name="甲", author_code="aaaaaa", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars)
        if title != "草稿版":
            repo.submit_for_review(author.id, version.public_id)
        if title == "退回版":
            repo.reject_version(version.public_id)
    old = publish(member_session, author, title="舊版")                     # 第二個劇本的舊版：被同一劇本的新版取代
    newer = validated(title="新版")
    draft = repo.create_draft_version(author.id, repo.get_script_of(old).public_id, content=newer.content, content_hash=newer.content_hash, total_chars=newer.total_chars)
    repo.submit_for_review(author.id, draft.public_id)
    repo.approve_version(draft.public_id, now=TODAY_UTC + timedelta(seconds=5))
    member_session.commit()
    assert sorted(titles(service.catalog(reader))) == ["公開版", "新版"]
    assert sorted(titles(service.catalog(author))) == ["公開版", "新版"]         # 作者自己的草稿與待審版本也不在清單


def test_each_card_id_is_the_version_public_id_that_the_setup_and_start_endpoints_take(member_session):
    service = service_for(member_session)
    version = publish(member_session, title="內建")
    card = service.catalog(player(member_session))["scripts"][0]
    assert card["id"] == version.public_id and len(card["id"]) == 36


@pytest.mark.parametrize("overrides, expected", [
    ({"content_tier_preference": "safe"}, ["安全"]),                                    # 安全模式的玩家：只含宣告為安全的版本
    ({}, ["一般", "安全"]),                                                              # 成年玩家預設一般模式：不含成人
    ({"age": 15, "content_tier_preference": "standard"}, ["安全"]),                      # 未滿 18 歲：不論偏好都是安全
])
def test_the_catalog_only_holds_versions_up_to_the_players_effective_mode(member_session, overrides, expected):
    for index, tier in enumerate(("safe", "standard", "adult")):
        publish(member_session, title={"safe": "安全", "standard": "一般", "adult": "成人"}[tier], content_tier=tier, now=TODAY_UTC + timedelta(seconds=index))
    assert titles(service_for(member_session).catalog(player(member_session, **overrides))) == expected


def test_an_adult_who_chose_the_adult_mode_with_the_deployment_switch_on_also_sees_adult_versions(member_session):
    for index, tier in enumerate(("safe", "standard", "adult")):
        publish(member_session, title=tier, content_tier=tier, now=TODAY_UTC + timedelta(seconds=index))
    reader = player(member_session, content_tier_preference="adult", adult_confirmed_at=TODAY_UTC, adult_rules_version=ADULT_RULES_VERSION)
    assert titles(service_for(member_session, adult_mode=True).catalog(reader)) == ["adult", "standard", "safe"]
    assert titles(service_for(member_session, adult_mode=False).catalog(reader)) == ["standard", "safe"]        # 開關關閉時退為一般模式


def test_an_admin_who_has_not_confirmed_the_age_still_gets_a_safe_catalog_instead_of_an_error(member_session):
    publish(member_session, title="安全", content_tier="safe")
    publish(member_session, title="一般", content_tier="standard", now=TODAY_UTC + timedelta(seconds=1))
    admin = player(member_session, age=None, role=ROLE_ADMIN)
    page = service_for(member_session).catalog(admin)
    assert titles(page) == ["安全"] and page["content_mode"] == {"effective": "safe", "is_adult": False}


# ---- 可玩劇本清單：排序與分頁 ----------------------------------------------------------------

def publish_channels(session):
    """由舊到新：男、全、女、空值（遷移之前的舊版本）、男、女。"""
    for index, (label, channel) in enumerate([("m1", "male"), ("a1", "all"), ("f1", "female"), ("n1", None), ("m2", "male"), ("f2", "female")]):
        extra = {"audience": channel} if channel in ("male", "female") else {}
        version = publish(session, title=label, now=TODAY_UTC + timedelta(seconds=index), **extra)
        if channel is None:
            version.audience = None
    session.commit()


@pytest.mark.parametrize("preference, expected", [
    ("male", ["m2", "m1", "n1", "a1", "f2", "f1"]),
    ("female", ["f2", "f1", "n1", "a1", "m2", "m1"]),
    ("all", ["f2", "m2", "n1", "f1", "a1", "m1"]),
])
def test_the_catalog_follows_the_players_story_preference_and_newest_first_for_everyone_else(member_session, preference, expected):
    publish_channels(member_session)
    assert titles(service_for(member_session).catalog(player(member_session, story_preference=preference))) == expected


def test_the_story_preference_never_changes_which_versions_the_catalog_holds(member_session):
    publish_channels(member_session)
    service = service_for(member_session)
    sets = []
    for n, preference in enumerate(("male", "female", "all"), start=1):
        reader = player(member_session, n, story_preference=preference)
        sets.append(frozenset(card["title"] for card in read_all(lambda **page: service.catalog(reader, **page), limit=2)))
    assert len(set(sets)) == 1 and len(sets[0]) == 6


@pytest.mark.parametrize("preference", ["male", "female", "all"])
def test_chained_catalog_pages_equal_one_full_read(member_session, preference):
    publish_channels(member_session)
    service, reader = service_for(member_session), player(member_session, story_preference=preference)
    whole = service.catalog(reader, limit=CATALOG_MAX_LIMIT)
    paged = read_all(lambda **page: service.catalog(reader, **page), limit=2)
    assert [card["id"] for card in paged] == [card["id"] for card in whole["scripts"]] and len(paged) == 6 and whole["has_more"] is False


def test_has_more_is_true_only_when_something_is_left_after_this_page(member_session):
    for index in range(3):
        publish(member_session, title=f"第{index}個", now=TODAY_UTC + timedelta(seconds=index))
    service, reader = service_for(member_session), player(member_session)
    assert [len(p["scripts"]) for p in (service.catalog(reader, limit=3), service.catalog(reader, limit=2), service.catalog(reader, limit=2, offset=2))] == [3, 2, 1]
    assert [service.catalog(reader, limit=3)["has_more"], service.catalog(reader, limit=2)["has_more"], service.catalog(reader, limit=2, offset=2)["has_more"]] == [False, True, False]
    assert service.catalog(reader, offset=OFFSET_MAX) == {"scripts": [], "has_more": False, "content_mode": {"effective": "standard", "is_adult": True}}


def test_an_empty_catalog_is_a_normal_empty_page(member_session):
    page = service_for(member_session).catalog(player(member_session))
    assert page["scripts"] == [] and page["has_more"] is False


# ---- 分頁參數的驗證 ---------------------------------------------------------------------------

def test_the_default_and_maximum_page_sizes_are_the_documented_ones():
    assert (CATALOG_DEFAULT_LIMIT, CATALOG_MAX_LIMIT, SESSIONS_DEFAULT_LIMIT, SESSIONS_MAX_LIMIT) == (12, 24, 20, 50)


@pytest.mark.parametrize("limit, offset, fields", [
    (0, 0, {"limit"}), (25, 0, {"limit"}), (-1, 0, {"limit"}), ("abc", 0, {"limit"}), (None, 0, {"limit"}), (True, 0, {"limit"}), (1.5, 0, {"limit"}),
    (12, -1, {"offset"}), (12, OFFSET_MAX + 1, {"offset"}), (12, "0", {"offset"}), (12, False, {"offset"}),
    (0, -1, {"limit", "offset"}),                                                          # 兩個都錯時一次指出
])
def test_an_invalid_catalog_page_is_a_validation_error_that_names_the_fields(member_session, limit, offset, fields):
    with pytest.raises(ValidationError) as caught:
        service_for(member_session).catalog(player(member_session), limit=limit, offset=offset)
    assert set(caught.value.fields) == fields


@pytest.mark.parametrize("limit", [0, 51, "x"])
def test_an_invalid_session_page_is_a_validation_error_too(member_session, limit):
    with pytest.raises(ValidationError) as caught:
        service_for(member_session).my_sessions(player(member_session), limit=limit)
    assert set(caught.value.fields) == {"limit"}


def test_the_boundary_values_are_accepted(member_session):
    service, reader = service_for(member_session), player(member_session)
    assert service.catalog(reader, limit=1, offset=0)["has_more"] is False and service.catalog(reader, limit=CATALOG_MAX_LIMIT, offset=OFFSET_MAX)["scripts"] == []
    assert service.my_sessions(reader, limit=1)["sessions"] == [] and service.my_sessions(reader, limit=SESSIONS_MAX_LIMIT, offset=OFFSET_MAX)["sessions"] == []


# ---- 卡片只含公開的摘要欄位 -------------------------------------------------------------------

def test_a_card_has_exactly_the_whitelisted_keys_and_only_plain_public_values(member_session):
    author = player(member_session, 1)
    version = publish(member_session, author, title="青雲問道", tags=["仙俠", "成長"], audience="female")
    page = service_for(member_session).catalog(player(member_session, 2))
    card = page["scripts"][0]
    assert set(card) == set(CARD_FIELDS) and set(page) == {"scripts", "has_more", "content_mode"}
    assert card == {
        "id": version.public_id, "title": "青雲問道", "summary": "少年入宗門，一步步走向自己的道。", "world_name": "乾歷朝仙俠世界",
        "tags": ["仙俠", "成長"], "author_name": "甲", "author_code": "aaaaaa", "content_tier": "safe", "audience": "female",
    }
    for value in card.values():                                    # 沒有任何整數或布林：內部編號進不了卡片
        assert value is None or isinstance(value, str) or (isinstance(value, list) and all(isinstance(tag, str) for tag in value))


def test_a_card_never_carries_the_script_body_hash_review_or_internal_ids(member_session):
    author = player(member_session, 1)
    version = publish(member_session, author, title="青雲問道")
    dumped = json.dumps(service_for(member_session).catalog(player(member_session, 2)), ensure_ascii=False)
    for secret in ("靈氣復甦", "林曉", "山門前", "境界分為", "宗門大測", "古劍山莊", version.content_hash, str(author.public_id)):
        assert secret not in dumped
    for key in ("background", "characters", "milestones", "rules", "opening", "review", "content_hash", "creator", "user_id", "script_id"):
        assert f'"{key}"' not in dumped


def test_a_version_from_before_the_channel_existed_is_shown_as_everyone(member_session):
    version = publish(member_session, title="舊劇本")
    version.audience = None
    member_session.commit()
    assert service_for(member_session).catalog(player(member_session))["scripts"][0]["audience"] == "all"


def test_the_builtin_script_is_signed_as_the_official_author_without_a_code(member_session):
    publish(member_session, title="首發劇本")
    card = service_for(member_session).catalog(player(member_session))["scripts"][0]
    assert (card["author_name"], card["author_code"]) == ("官方", None)


def test_a_builtin_script_whose_pen_name_is_empty_is_still_signed_as_official_but_a_player_one_is_not(member_session):
    builtin = publish(member_session, title="內建")
    mine = publish(member_session, player(member_session, 1), title="玩家")
    repo = ScriptRepository(member_session)
    repo.get_script_of(builtin).author_name = None
    repo.get_script_of(mine).author_name = None
    member_session.commit()
    by_title = {card["title"]: card for card in service_for(member_session).catalog(player(member_session, 2))["scripts"]}
    assert by_title["內建"]["author_name"] == "官方" and by_title["玩家"]["author_name"] is None


# ---- 內容模式摘要 -------------------------------------------------------------------------------

def test_a_minor_sees_why_the_catalog_is_safe_and_the_summary_has_no_birthday_or_age(member_session):
    publish(member_session, title="一般劇本", content_tier="standard")
    reader = player(member_session, age=15)
    page = service_for(member_session).catalog(reader)
    assert page["content_mode"] == {"effective": "safe", "is_adult": False} and page["scripts"] == []
    dumped = json.dumps(page, ensure_ascii=False)
    for private in (reader.birth_date.isoformat(), "birth", "age"):
        assert private not in dumped


def test_an_adult_with_the_default_settings_is_in_the_standard_mode(member_session):
    page = service_for(member_session).catalog(player(member_session))
    assert page["content_mode"] == {"effective": "standard", "is_adult": True}


@pytest.mark.parametrize("overrides", [{}, {"age": 15}, {"content_tier_preference": "safe"}, {"age": None, "role": ROLE_ADMIN}])
def test_the_summary_agrees_with_the_content_mode_endpoints_effective_mode(member_session, overrides):
    reader = player(member_session, **overrides)
    policy = AccessPolicy(clock=FakeClock())
    state = state_of(policy.content_access(reader))
    summary = service_for(member_session).catalog(reader)["content_mode"]
    assert summary == {"effective": state["effective"], "is_adult": state["selectable"]["standard"]}


# ---- 一頁的查詢次數固定 -------------------------------------------------------------------------

def test_a_catalog_page_costs_the_same_number_of_queries_for_one_card_and_for_twelve(member_session):
    service, reader = service_for(member_session), player(member_session)

    def statements_for_a_page() -> int:
        member_session.expire_all()
        with count_statements(member_session) as statements:
            page = service.catalog(reader, limit=CATALOG_MAX_LIMIT)
        assert page["scripts"]
        return len(statements)

    publish(member_session, player(member_session, 2), title="只有一個", tags=["甲", "乙"])
    one = statements_for_a_page()
    for index in range(11):
        publish(member_session, player(member_session, 3 + index), title=f"劇本{index}", tags=["甲", "乙", "丙"], now=TODAY_UTC + timedelta(seconds=index + 1))
    assert len(service.catalog(reader, limit=CATALOG_MAX_LIMIT)["scripts"]) == 12
    assert one == statements_for_a_page() == 2


# ---- 我的存檔清單 ---------------------------------------------------------------------------------

def test_my_sessions_holds_only_the_callers_own_saves(member_session):
    alice, bob = player(member_session, 1), player(member_session, 2)
    open_save(member_session, alice, title="甲的存檔")
    open_save(member_session, bob, title="乙的存檔")
    service = service_for(member_session)
    assert [item["title"] for item in service.my_sessions(alice)["sessions"]] == ["甲的存檔"]
    assert [item["title"] for item in service.my_sessions(bob)["sessions"]] == ["乙的存檔"]
    admin = player(member_session, 3, role=ROLE_ADMIN)
    assert service.my_sessions(admin) == {"sessions": [], "has_more": False}              # 管理員沒有特別的路徑


def test_my_sessions_is_newest_activity_first_and_ties_break_by_the_newer_save(member_session):
    alice = player(member_session)
    for title, now in (("一月", TODAY_UTC - timedelta(days=90)), ("三月", TODAY_UTC - timedelta(days=30)), ("二月", TODAY_UTC - timedelta(days=60)),
                       ("同時一", TODAY_UTC), ("同時二", TODAY_UTC)):
        open_save(member_session, alice, title=title, now=now)
    shown = [item["title"] for item in service_for(member_session).my_sessions(alice)["sessions"]]
    assert shown == ["同時二", "同時一", "三月", "二月", "一月"]


def test_an_archived_save_is_not_listed(member_session):
    alice = player(member_session)
    keep, hide = open_save(member_session, alice, title="留著"), open_save(member_session, alice, title="封存")
    SessionRepository(member_session).set_archived(alice.id, hide.public_id, True)
    member_session.commit()
    assert [item["id"] for item in service_for(member_session).my_sessions(alice)["sessions"]] == [keep.public_id]


def test_the_completed_turns_count_only_complete_turns(member_session):
    alice = player(member_session)
    save = open_save(member_session, alice)
    finish_turns(member_session, alice, save, 2)
    repo = SessionRepository(member_session)
    repo.start_turn(alice.id, save.public_id, client_msg_id="b-1", user_action="被擋", now=TODAY_UTC)
    repo.block_turn(alice.id, save.public_id, "b-1")
    repo.start_turn(alice.id, save.public_id, client_msg_id="c-1", user_action="中斷", now=TODAY_UTC)
    repo.interrupt_turn(alice.id, save.public_id, "c-1", partial_response="半截")
    assert [item["completed_turns"] for item in service_for(member_session).my_sessions(alice)["sessions"]] == [2]


def test_a_save_that_has_not_started_playing_shows_zero_turns_even_with_an_opening(member_session):
    alice = player(member_session)
    save = open_save(member_session, alice)
    save.opening_state, save.opening_text = "ready", "第 0 幕的內容"
    member_session.commit()
    assert service_for(member_session).my_sessions(alice)["sessions"][0]["completed_turns"] == 0


def test_an_ended_save_stays_in_the_list_marked_ended_and_an_active_one_is_marked_active(member_session):
    alice = player(member_session)
    done, going = open_save(member_session, alice, title="完結", now=TODAY_UTC - timedelta(days=1)), open_save(member_session, alice, title="進行中")
    SessionRepository(member_session).end_session(alice.id, done.public_id, now=TODAY_UTC)
    member_session.commit()
    items = {item["title"]: item["status"] for item in service_for(member_session).my_sessions(alice)["sessions"]}
    assert items == {"完結": "ended", "進行中": "active"}                                    # 對外的列舉值就是這兩個字串


def test_a_save_item_has_exactly_the_whitelisted_keys_and_none_of_the_save_contents(member_session):
    alice = player(member_session)
    save = open_save(member_session, alice, title="青雲問道", now=TODAY_UTC)
    save.notebook_text, save.status_summary, save.current_location = "記事簿的祕密", "世界摘要的祕密", "祕密地點"
    member_session.commit()
    page = service_for(member_session).my_sessions(alice)
    item = page["sessions"][0]
    assert set(page) == {"sessions", "has_more"} and set(item) == set(SAVE_FIELDS)
    assert item == {
        "id": save.public_id, "title": "青雲問道", "content_tier": "safe", "status": "active", "player_name": "林初", "completed_turns": 0,
        "last_active_at": "2026-10-27T12:00:00Z",
    }
    dumped = json.dumps(page, ensure_ascii=False)
    for secret in ("記事簿的祕密", "世界摘要的祕密", "祕密地點", "靈氣復甦", "林曉", "沉默愛思考", "hidden", "anchor", "user_id", "notebook", "entities"):
        assert secret not in dumped
    assert [key for key, value in item.items() if isinstance(value, int) and not isinstance(value, bool)] == ["completed_turns"]   # 唯一的整數是輪數


def test_my_sessions_pages_chain_together_and_report_has_more(member_session):
    alice = player(member_session)
    for index in range(5):
        open_save(member_session, alice, title=f"存檔{index}", now=TODAY_UTC + timedelta(minutes=index))
    service = service_for(member_session)
    whole = service.my_sessions(alice)
    paged = read_all(lambda **page: service.my_sessions(alice, **page), limit=2)
    assert [item["id"] for item in paged] == [item["id"] for item in whole["sessions"]] and len(paged) == 5 and whole["has_more"] is False
    assert service.my_sessions(alice, limit=5)["has_more"] is False and service.my_sessions(alice, limit=4)["has_more"] is True


def test_a_player_without_saves_gets_an_empty_page(member_session):
    assert service_for(member_session).my_sessions(player(member_session)) == {"sessions": [], "has_more": False}


def test_a_session_page_costs_two_queries_for_one_save_and_for_twelve(member_session):
    alice = player(member_session)
    service = service_for(member_session)

    def statements_for_a_page() -> int:
        member_session.expire_all()
        with count_statements(member_session) as statements:
            page = service.my_sessions(alice, limit=SESSIONS_MAX_LIMIT)
        assert page["sessions"]
        return len(statements)

    finish_turns(member_session, alice, open_save(member_session, alice, title="第一個"), 1)
    one = statements_for_a_page()
    for index in range(11):
        finish_turns(member_session, alice, open_save(member_session, alice, title=f"另外{index}", now=TODAY_UTC + timedelta(seconds=index + 1)), 2, prefix=f"s{index}")
    assert len(service.my_sessions(alice, limit=SESSIONS_MAX_LIMIT)["sessions"]) == 12
    assert one == statements_for_a_page() == 2


# ---- 存檔摘要 -------------------------------------------------------------------------------------

def test_the_owner_reads_the_summary_of_their_own_save(member_session):
    alice = player(member_session)
    save = open_save(member_session, alice, title="青雲問道", content_tier="standard")
    summary = service_for(member_session).session_summary(alice, save.public_id)
    assert set(summary) == set(SUMMARY_FIELDS)
    assert summary == {"id": save.public_id, "title": "青雲問道", "content_tier": "standard", "player_name": "林初", "ended": False}


def test_the_summary_of_an_ended_or_archived_save_is_still_readable(member_session):
    alice = player(member_session)
    save = open_save(member_session, alice)
    repo = SessionRepository(member_session)
    repo.end_session(alice.id, save.public_id, now=TODAY_UTC)
    repo.set_archived(alice.id, save.public_id, True)
    member_session.commit()
    assert service_for(member_session).session_summary(alice, save.public_id)["ended"] is True


def test_another_player_a_missing_save_and_a_malformed_id_all_get_the_identical_not_found(member_session):
    alice, bob = player(member_session, 1), player(member_session, 2)
    save = open_save(member_session, alice)
    service = service_for(member_session)
    caught = []
    for who, public_id in ((bob, save.public_id), (bob, GHOST), (alice, GHOST), (alice, None), (alice, 123), (alice, ["x"])):
        with pytest.raises(NotFound) as error:
            service.session_summary(who, public_id)
        caught.append((error.value.code, error.value.message))
    assert len(set(caught)) == 1 and caught[0][0] == "not_found"
    admin = player(member_session, 3, role=ROLE_ADMIN)
    with pytest.raises(NotFound):
        service.session_summary(admin, save.public_id)                                      # 管理員也不能憑識別碼讀別人的存檔


def test_the_summary_does_not_need_the_age_gate(member_session):
    unconfirmed = player(member_session, age=None)
    save = open_save(member_session, unconfirmed, title="早期的存檔")
    assert service_for(member_session).session_summary(unconfirmed, save.public_id)["title"] == "早期的存檔"


# ---- 唯讀與組裝 -----------------------------------------------------------------------------------

def test_the_service_never_depends_on_the_model_the_wallet_or_the_usage_ledger():
    """入口只是唯讀的清單：不呼叫模型、不碰錢包與額度（也就不會有任何扣款或帳本寫入）。"""
    import app.services.game_entry_service as module

    imported = set()
    for node in ast.walk(ast.parse(Path(module.__file__).read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported |= {node.module, *(f"{node.module}.{alias.name}" for alias in node.names)}
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
    for name in imported:
        assert not any(word in name for word in ("wallet", "llm", "usage", "quota", "review", "runtime_limits")), name


def test_the_registry_assembles_the_service_without_any_model_provider():
    with request_scope(make_app({"GEMINI_API_KEY": GEMINI_KEY})):
        service = get_game_entry_service()
        assert isinstance(service, GameEntryService)
        assert (service._scripts.__class__, service._sessions.__class__) == (ScriptRepository, SessionRepository)
