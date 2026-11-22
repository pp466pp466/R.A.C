"""rpg-entry-api 規格：RPG 入口的唯讀資料（可玩劇本清單、我的存檔清單、存檔摘要）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。這裡直接測 Service（Controller 與端點由 B 批測）。
"""
import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.models.script import (
    OFFICIAL_AUTHOR_NAME, VERSION_DRAFT, VERSION_PENDING, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED,
)
from app.models.script_report import CLOSED_DISMISSED
from app.models.user import ROLE_ADMIN, User
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.services.access_policy import AccessPolicy
from app.services.catalog_query import encode_cursor
from app.services.content_tier import SAFE_RULES_VERSION
from app.services.current_user import CurrentUser
from app.services.errors import NotFound, ValidationError
from app.services.registry import get_game_entry_service
from app.services.game_entry_service import (
    CARD_FIELDS, CATALOG_DEFAULT_LIMIT, CATALOG_MAX_LIMIT, DETAIL_FIELDS, OFFSET_MAX, REPORT_STATES, SAVE_FIELDS, SESSIONS_DEFAULT_LIMIT,
    SESSIONS_MAX_LIMIT, SUMMARY_FIELDS, GameEntryService,
)
from tests.support.age import TODAY_UTC
from tests.support.auth import FakeClock, add_user
from tests.support.protagonist import protagonist
from tests.support.query_counter import count_statements
from tests.support.script_data import validated
from tests.support.script_review import put_in_status
from tests.test_llm_providers_by_purpose import GEMINI_KEY, make_app, request_scope

GHOST = "00000000-0000-4000-8000-000000000000"


def service_for(session, safe_mode_forced=False) -> GameEntryService:
    return GameEntryService(
        scripts=ScriptRepository(session), sessions=SessionRepository(session),
        policy=AccessPolicy(clock=FakeClock(), safe_mode_forced=safe_mode_forced), reports=ScriptReportRepository(session),
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


def read_catalog(service, reader, limit, **filters):
    """用游標依序讀完目錄的每一頁，回傳各頁串起來的卡片。"""
    collected, cursor = [], None
    for _ in range(50):                                                    # 有上限：被破壞成永遠 has_more 時要失敗，不要卡住
        page = service.catalog(reader, limit=limit, cursor=cursor, **filters)
        collected += page["scripts"]
        if not page["has_more"]:
            return collected
        cursor = page["next_cursor"]
    raise AssertionError("has_more 一直為真：分頁沒有走到盡頭")


def read_all(fetch, limit):
    """（我的存檔，位移分頁）依序讀取每一頁直到 `has_more` 為假，回傳各頁串起來的結果。"""
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
            repo.reject_version(version.public_id, "other_policy", None, TODAY_UTC)
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


AGREED = {"adult_confirmed_at": TODAY_UTC, "adult_rules_version": SAFE_RULES_VERSION}


@pytest.mark.parametrize("overrides", [
    {},                                                          # 預設：安全模式開
    {"content_tier_preference": "adult", **AGREED},              # 已關閉安全模式並同意目前規範
    {"content_tier_preference": "adult"},                        # 偏好 adult 但沒有同意紀錄
    {"age": None, "role": ROLE_ADMIN},                           # 尚未完成年齡確認的管理員
])
def test_the_catalog_holds_every_published_version_whatever_the_players_mode(member_session, overrides):
    for index, tier in enumerate(("safe", "standard", "adult")):
        publish(member_session, title=tier, content_tier=tier, now=TODAY_UTC + timedelta(seconds=index))
    assert titles(service_for(member_session).catalog(player(member_session, **overrides))) == ["adult", "standard", "safe"]
    assert titles(service_for(member_session, safe_mode_forced=True).catalog(player(member_session, 2, **overrides))) == ["adult", "standard", "safe"]


def test_a_card_reports_the_binding_with_the_legacy_standard_read_as_adult(member_session):
    for index, tier in enumerate(("safe", "standard", "adult")):
        publish(member_session, title=tier, content_tier=tier, now=TODAY_UTC + timedelta(seconds=index))
    cards = {card["title"]: card["content_tier"] for card in service_for(member_session).catalog(player(member_session))["scripts"]}
    assert cards == {"safe": "safe", "standard": "adult", "adult": "adult"}


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
        sets.append(frozenset(card["title"] for card in read_catalog(service, reader, limit=2)))
    assert len(set(sets)) == 1 and len(sets[0]) == 6


@pytest.mark.parametrize("preference", ["male", "female", "all"])
def test_chained_catalog_pages_equal_one_full_read(member_session, preference):
    publish_channels(member_session)
    service, reader = service_for(member_session), player(member_session, story_preference=preference)
    whole = service.catalog(reader, limit=CATALOG_MAX_LIMIT)
    paged = read_catalog(service, reader, limit=2)
    assert [card["id"] for card in paged] == [card["id"] for card in whole["scripts"]] and len(paged) == 6 and whole["has_more"] is False


def test_has_more_is_true_only_when_something_is_left_after_this_page(member_session):
    for index in range(3):
        publish(member_session, title=f"第{index}個", now=TODAY_UTC + timedelta(seconds=index))
    service, reader = service_for(member_session), player(member_session)
    everything, first = service.catalog(reader, limit=3), service.catalog(reader, limit=2)
    second = service.catalog(reader, limit=2, cursor=first["next_cursor"])
    assert [len(everything["scripts"]), len(first["scripts"]), len(second["scripts"])] == [3, 2, 1]
    assert [everything["has_more"], first["has_more"], second["has_more"]] == [False, True, False]
    assert isinstance(first["next_cursor"], str) and everything["next_cursor"] is None and second["next_cursor"] is None
    past_the_end = encode_cursor(0, TODAY_UTC - timedelta(days=3650), 1)
    assert service.catalog(reader, cursor=past_the_end) == {
        "scripts": [], "has_more": False, "next_cursor": None, "content_mode": {"effective": "safe", "forced": False},
    }


def test_an_empty_catalog_is_a_normal_empty_page(member_session):
    page = service_for(member_session).catalog(player(member_session))
    assert page["scripts"] == [] and page["has_more"] is False


# ---- 分頁參數的驗證 ---------------------------------------------------------------------------

def test_the_default_and_maximum_page_sizes_are_the_documented_ones():
    assert (CATALOG_DEFAULT_LIMIT, CATALOG_MAX_LIMIT, SESSIONS_DEFAULT_LIMIT, SESSIONS_MAX_LIMIT) == (12, 24, 20, 50)


@pytest.mark.parametrize("limit", [0, 25, -1, "abc", None, True, 1.5])
def test_an_invalid_catalog_page_size_is_a_validation_error_that_names_the_field(member_session, limit):
    with pytest.raises(ValidationError) as caught:
        service_for(member_session).catalog(player(member_session), limit=limit)
    assert set(caught.value.fields) == {"limit"}


@pytest.mark.parametrize("limit, offset, fields", [
    (0, 0, {"limit"}), (51, 0, {"limit"}), ("x", 0, {"limit"}),
    (20, -1, {"offset"}), (20, OFFSET_MAX + 1, {"offset"}), (20, "0", {"offset"}), (20, False, {"offset"}),
    (0, -1, {"limit", "offset"}),                                                          # 兩個都錯時一次指出
])
def test_an_invalid_session_page_is_a_validation_error_that_names_the_fields(member_session, limit, offset, fields):
    with pytest.raises(ValidationError) as caught:
        service_for(member_session).my_sessions(player(member_session), limit=limit, offset=offset)
    assert set(caught.value.fields) == fields


def test_the_boundary_values_are_accepted(member_session):
    service, reader = service_for(member_session), player(member_session)
    assert service.catalog(reader, limit=1)["has_more"] is False and service.catalog(reader, limit=CATALOG_MAX_LIMIT)["scripts"] == []
    assert service.my_sessions(reader, limit=1)["sessions"] == [] and service.my_sessions(reader, limit=SESSIONS_MAX_LIMIT, offset=OFFSET_MAX)["sessions"] == []


# ---- 卡片只含公開的摘要欄位 -------------------------------------------------------------------

def test_a_card_has_exactly_the_whitelisted_keys_and_only_plain_public_values(member_session):
    author = player(member_session, 1)
    version = publish(member_session, author, title="青雲問道", tags=["仙俠", "成長"], audience="female")
    page = service_for(member_session).catalog(player(member_session, 2))
    card = page["scripts"][0]
    assert set(card) == set(CARD_FIELDS) and set(page) == {"scripts", "has_more", "next_cursor", "content_mode"}
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

def test_the_summary_is_only_the_effective_mode_and_the_brake_and_has_no_birthday_age_or_is_adult(member_session):
    publish(member_session, title="未綁定", content_tier="adult")
    reader = player(member_session, age=30)
    page = service_for(member_session).catalog(reader)
    assert page["content_mode"] == {"effective": "safe", "forced": False} and len(page["scripts"]) == 1
    dumped = json.dumps(page, ensure_ascii=False)
    for private in (reader.birth_date.isoformat(), "birth", "age", "is_adult"):
        assert private not in dumped


def test_a_player_who_agreed_and_turned_the_safe_mode_off_has_the_adult_mode_in_the_summary(member_session):
    reader = player(member_session, content_tier_preference="adult", **AGREED)
    assert service_for(member_session).catalog(reader)["content_mode"] == {"effective": "adult", "forced": False}


def test_with_the_emergency_brake_the_summary_is_safe_and_forced(member_session):
    reader = player(member_session, content_tier_preference="adult", **AGREED)
    assert service_for(member_session, safe_mode_forced=True).catalog(reader)["content_mode"] == {"effective": "safe", "forced": True}


@pytest.mark.parametrize("overrides", [{}, {"content_tier_preference": "safe"}, {"content_tier_preference": "adult", **AGREED}, {"age": None, "role": ROLE_ADMIN}])
@pytest.mark.parametrize("forced", [False, True])
def test_the_summary_agrees_with_the_content_mode_endpoints_effective_mode(member_session, overrides, forced):
    reader = player(member_session, **overrides)
    state = AccessPolicy(clock=FakeClock(), safe_mode_forced=forced).mode_state(reader)
    summary = service_for(member_session, safe_mode_forced=forced).catalog(reader)["content_mode"]
    assert summary == {"effective": state.effective, "forced": state.forced}


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


# ---- 公開目錄：搜尋、篩選、游標、常用標籤與詳情（rpg-script-catalog 規格；Service 層，含 SQL Server）-----------------------------------

def cards_of(page) -> list[dict]:
    return page["scripts"]


def test_the_keyword_tag_and_channel_filters_narrow_the_catalog_and_combine(member_session):
    publish(member_session, title="青雲劍訣", world_name="某世界", tags=["仙俠"], audience="male", now=TODAY_UTC)
    publish(member_session, title="都市日常", world_name="某世界", tags=["都市", "SciFi"], audience="female", now=TODAY_UTC + timedelta(seconds=1))
    publish(member_session, title="江湖行", world_name="某世界", tags=["仙俠"], now=TODAY_UTC + timedelta(seconds=2))
    service, reader = service_for(member_session), player(member_session)
    assert titles(service.catalog(reader, q="青雲")) == ["青雲劍訣"]
    assert titles(service.catalog(reader, q="  SCIFI  ")) == ["都市日常"]                    # 正規化：空白與大小寫
    assert titles(service.catalog(reader, tag="仙俠")) == ["江湖行", "青雲劍訣"]
    assert titles(service.catalog(reader, audience="all")) == ["江湖行"]
    assert titles(service.catalog(reader, q="青雲", tag="仙俠", audience="male")) == ["青雲劍訣"]
    assert titles(service.catalog(reader, q="青雲", audience="female")) == []
    assert titles(service.catalog(reader, q="", tag="", audience="", cursor="")) == ["江湖行", "都市日常", "青雲劍訣"]


@pytest.mark.parametrize("term", ["%", "_", "[a-z]", "\\"])
def test_a_wildcard_keyword_matches_literally_and_finds_nothing(member_session, term):
    publish(member_session, title="普通劇本")
    publish(member_session, title="另一個劇本", now=TODAY_UTC + timedelta(seconds=1))
    assert cards_of(service_for(member_session).catalog(player(member_session), q=term)) == []


@pytest.mark.parametrize("kwargs, fields", [
    ({"q": "一 二 三 四 五 六"}, {"q"}), ({"q": "字" * 51}, {"q"}), ({"tag": "字" * 13}, {"tag"}), ({"audience": "other"}, {"audience"}),
    ({"cursor": "garbage"}, {"cursor"}), ({"limit": 0}, {"limit"}), ({"q": 5}, {"q"}),
    ({"q": "一 二 三 四 五 六", "tag": "字" * 13, "audience": "x", "limit": 25, "cursor": "!"}, {"q", "tag", "audience", "limit", "cursor"}),
])
def test_an_invalid_catalog_query_names_every_bad_field_at_once(member_session, kwargs, fields):
    with pytest.raises(ValidationError) as caught:
        service_for(member_session).catalog(player(member_session), **kwargs)
    assert set(caught.value.fields) == fields


def test_the_catalog_does_not_accept_an_offset_any_more(member_session):
    with pytest.raises(TypeError):
        service_for(member_session).catalog(player(member_session), offset=2)


@pytest.mark.parametrize("preference", ["male", "female", "all"])
@pytest.mark.parametrize("size", [1, 2, 4])
def test_chained_cursor_pages_equal_one_full_read_and_the_last_page_has_no_cursor(member_session, preference, size):
    publish_channels(member_session)
    service, reader = service_for(member_session), player(member_session, story_preference=preference)
    whole = service.catalog(reader, limit=CATALOG_MAX_LIMIT)
    assert whole["has_more"] is False and whole["next_cursor"] is None and len(cards_of(whole)) == 6
    collected, cursor, last = [], None, None
    for _ in range(10):
        last = service.catalog(reader, limit=size, cursor=cursor)
        collected += [card["id"] for card in cards_of(last)]
        assert (last["next_cursor"] is not None) == last["has_more"]
        if not last["has_more"]:
            break
        cursor = last["next_cursor"]
    assert collected == [card["id"] for card in cards_of(whole)] and last["next_cursor"] is None


def test_a_new_script_published_between_pages_does_not_repeat_and_a_taken_down_one_does_not_disturb(member_session):
    author = player(member_session, 2)
    for index in range(4):
        publish(member_session, author, title=f"第{index}個", now=TODAY_UTC + timedelta(seconds=index))
    service, reader = service_for(member_session), player(member_session)
    first = service.catalog(reader, limit=2)
    assert titles(first) == ["第3個", "第2個"] and first["has_more"] is True
    publish(member_session, author, title="新上架", now=TODAY_UTC + timedelta(seconds=99))
    second = service.catalog(reader, limit=2, cursor=first["next_cursor"])
    assert titles(second) == ["第1個", "第0個"] and second["has_more"] is False


def test_a_cursor_from_one_preference_still_works_after_the_preference_changed(member_session):
    """玩家在翻頁之間改了偏好：不會 500，也不會因為游標帶著舊的偏好層而得到空頁（不分層時只看發佈時間與編號）。"""
    publish_channels(member_session)
    service = service_for(member_session)
    first = service.catalog(player(member_session, 1, story_preference="female"), limit=2)
    assert titles(first) == ["f2", "f1"] and first["has_more"] is True
    page = service.catalog(player(member_session, 2, story_preference="all"), limit=24, cursor=first["next_cursor"])
    assert titles(page) == ["a1", "m1"]                                                # 比 f1 更早發佈的


def test_the_catalog_page_still_holds_the_content_mode_and_only_card_fields(member_session):
    publish(member_session, title="一", tags=["甲"])
    page = service_for(member_session).catalog(player(member_session), q="一")
    assert set(page) == {"scripts", "has_more", "next_cursor", "content_mode"} and set(page["content_mode"]) == {"effective", "forced"}
    assert set(page["scripts"][0]) == set(CARD_FIELDS)


def test_a_filtered_catalog_page_costs_two_queries_whatever_the_size(member_session):
    service, reader = service_for(member_session), player(member_session)

    def statements_for_a_page() -> int:
        member_session.expire_all()
        with count_statements(member_session) as statements:
            page = service.catalog(reader, q="劇本", tag="甲", audience="all", limit=CATALOG_MAX_LIMIT)
        assert page["scripts"]
        return len(statements)

    publish(member_session, player(member_session, 2), title="劇本零", tags=["甲", "乙"])
    one = statements_for_a_page()
    for index in range(11):
        publish(member_session, player(member_session, 3 + index), title=f"劇本{index}", tags=["甲", "乙", "丙"], now=TODAY_UTC + timedelta(seconds=index + 1))
    assert len(service.catalog(reader, limit=CATALOG_MAX_LIMIT, q="劇本")["scripts"]) == 12
    assert one == statements_for_a_page() == 2


# ---- 常用標籤 -------------------------------------------------------------------------------------

def test_the_popular_tags_count_published_versions_only_and_merge_case(member_session):
    author = player(member_session, 2)
    publish(member_session, author, title="一", tags=["仙俠", "SciFi"], now=TODAY_UTC)
    publish(member_session, author, title="二", tags=["仙俠", "scifi"], now=TODAY_UTC + timedelta(seconds=1))
    publish(member_session, title="三", tags=["都市"], now=TODAY_UTC + timedelta(seconds=2))
    repo = ScriptRepository(member_session)
    result = validated(title="草稿", tags=["仙俠", "草稿標籤"])
    repo.create_script(author.id, author_name="甲", author_code="aaaaaa", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars)
    member_session.commit()
    assert service_for(member_session).catalog_tags() == {"tags": [
        {"tag": "scifi", "count": 2}, {"tag": "仙俠", "count": 2}, {"tag": "都市", "count": 1},         # 同數量時依標籤排序（字碼順序）
    ]}


def test_there_are_no_popular_tags_when_nothing_is_published(member_session):
    assert service_for(member_session).catalog_tags() == {"tags": []}


def test_the_popular_tags_are_capped_at_thirty(member_session):
    for index in range(20):
        publish(member_session, title=f"劇本{index}", tags=[f"標{2 * index:02d}", f"標{2 * index + 1:02d}"], now=TODAY_UTC + timedelta(seconds=index))
    assert len(service_for(member_session).catalog_tags()["tags"]) == 30


# ---- 詳情 ----------------------------------------------------------------------------------------

def hidden_script(**changes):
    """含有「詳情不得公開」的各種內容的劇本：隱藏特質、個性、里程碑、結局說明、規則與開場。"""
    base = dict(
        title="青雲問道", tags=["仙俠"], rules="規則的獨特字串", opening="開場白的獨特字串", ending_mode="custom", ending_text="結局說明的獨特字串", ending_age=80,
        characters=[
            {"name": "林曉", "personality": "活潑好動", "age": 16, "appearance": "身形瘦小", "occupation": "學徒"},
            {"name": "蘇長風", "personality": "沉穩寡言", "age": 168, "appearance": "鬢髮微白", "occupation": "長老", "hidden_trait": "暗中守護青雲宗"},
        ],
    )
    base.update(changes)
    return base


def test_the_detail_has_exactly_the_whitelisted_keys_and_the_public_values(member_session):
    version = publish(member_session, player(member_session, 2), **hidden_script())
    detail = service_for(member_session).script_detail(player(member_session), version.public_id)
    assert set(detail) == set(DETAIL_FIELDS)
    assert (detail["id"], detail["title"], detail["tags"], detail["version_no"]) == (version.public_id, "青雲問道", ["仙俠"], 1)
    assert detail["background"].startswith("靈氣復甦") and detail["locations"] == ["古劍山莊", "青雲宗山門"] and detail["has_custom_ending"] is True
    assert detail["characters"] == [{"name": "林曉", "occupation": "學徒"}, {"name": "蘇長風", "occupation": "長老"}]
    assert detail["published_at"].endswith("Z") and detail["audience"] == "all" and detail["content_tier"] == "safe"
    assert detail["author_code"] == "aaaaaa" and detail["content_mode"] == {"effective": "safe", "forced": False}


def test_the_detail_never_carries_the_hidden_parts_of_a_script(member_session):
    version = publish(member_session, player(member_session, 2), **hidden_script())
    text = json.dumps(service_for(member_session).script_detail(player(member_session), version.public_id), ensure_ascii=False)
    for forbidden in ("暗中守護青雲宗", "沉穩寡言", "鬢髮微白", "宗門大測", "結局說明的獨特字串", "規則的獨特字串", "開場白的獨特字串", "活潑好動",
                      "hidden_trait", "personality", "milestones", "opening", "ending_text", "rules", "content_hash", "review", "creator", "user_id", "seed_key"):
        assert forbidden not in text, forbidden


def test_a_script_without_a_custom_ending_says_so(member_session):
    version = publish(member_session, title="無結局")
    assert service_for(member_session).script_detail(player(member_session), version.public_id)["has_custom_ending"] is False


def test_the_builtin_script_detail_is_signed_as_official_and_a_legacy_channel_reads_as_everyone(member_session):
    version = publish(member_session, title="內建")
    version.audience = None
    member_session.commit()
    detail = service_for(member_session).script_detail(player(member_session), version.public_id)
    assert (detail["author_name"], detail["author_code"], detail["audience"]) == (OFFICIAL_AUTHOR_NAME, None, "all")


def test_the_detail_follows_the_players_effective_mode_and_the_emergency_brake(member_session):
    version = publish(member_session, title="一")
    agreed = {"content_tier_preference": "adult", **AGREED}
    assert service_for(member_session).script_detail(player(member_session, **agreed), version.public_id)["content_mode"] == {"effective": "adult", "forced": False}
    assert service_for(member_session, safe_mode_forced=True).script_detail(player(member_session, 2, **agreed), version.public_id)["content_mode"] == {"effective": "safe", "forced": True}


@pytest.mark.parametrize("status", [VERSION_DRAFT, VERSION_PENDING, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED])
def test_a_script_that_is_not_published_is_not_found_even_for_its_author(member_session, status):
    author = player(member_session, 2)
    version = put_in_status(ScriptRepository(member_session), member_session.get(User, author.id), status, title="非上架")
    member_session.commit()
    service = service_for(member_session)
    for reader in (author, player(member_session, 3)):
        with pytest.raises(NotFound):
            service.script_detail(reader, version.public_id)


@pytest.mark.parametrize("bad", [GHOST, "not-a-uuid", "", None, 123, ["x"], "a" * 400])
def test_a_missing_or_malformed_id_is_not_found(member_session, bad):
    with pytest.raises(NotFound):
        service_for(member_session).script_detail(player(member_session), bad)


def test_the_detail_does_not_need_the_age_gate(member_session):
    version = publish(member_session, title="一")
    admin = player(member_session, 2, age=None, role=ROLE_ADMIN)
    assert service_for(member_session).script_detail(admin, version.public_id)["title"] == "一"


def test_a_taken_down_script_loses_its_detail_and_regains_it_when_restored(member_session):
    author = player(member_session, 2)
    version = publish(member_session, author, title="可下架")
    repo, service = ScriptRepository(member_session), service_for(member_session)
    reader = player(member_session, 3)
    repo.remove_own(author.id, version.public_id, TODAY_UTC + timedelta(hours=1))
    member_session.commit()
    with pytest.raises(NotFound):
        service.script_detail(reader, version.public_id)
    repo.restore_own(author.id, version.public_id)
    member_session.commit()
    assert service.script_detail(reader, version.public_id)["title"] == "可下架"


def test_a_detail_costs_the_same_number_of_queries_whatever_the_size_of_the_script(member_session):
    service, reader = service_for(member_session), player(member_session)
    small = publish(member_session, title="小", tags=["甲"], characters=[{"name": "甲", "personality": "x", "age": 1, "appearance": "y"}])
    big = publish(member_session, now=TODAY_UTC + timedelta(seconds=1), **hidden_script(title="大", tags=["甲", "乙", "丙", "丁", "戊"]))
    counts = []
    for public_id in (small.public_id, big.public_id):                           # 先取出識別碼：過期的物件在迴圈裡讀屬性會多出一次重新載入
        member_session.expire_all()
        with count_statements(member_session) as statements:
            service.script_detail(reader, public_id)
        counts.append(len(statements))
    assert counts[0] == counts[1] == 2


# ---- 詳情的檢舉入口（rpg-script-reports 的 report_state）-----------------------------------------

def report_it(session, reporter, version, closed=None):
    """讓 `reporter` 檢舉過這個版本（直接寫入）；`closed` 非空時再把它結案。"""
    repo = ScriptReportRepository(session)
    repo.add(reporter.id, version.id, ["other"], False, None, now=TODAY_UTC)
    if closed is not None:
        repo.close_open(version.id, closed, TODAY_UTC)
    session.commit()


def test_report_state_is_one_of_three_values_and_the_detail_has_no_report_counts():
    assert "report_state" in DETAIL_FIELDS and REPORT_STATES == ("available", "reported", "unavailable")
    assert not any("count" in field or field.startswith("report_") and field != "report_state" for field in DETAIL_FIELDS)


def test_another_players_script_can_be_reported(member_session):
    version = publish(member_session, player(member_session, 2), title="他人的")
    assert service_for(member_session).script_detail(player(member_session), version.public_id)["report_state"] == "available"


def test_a_builtin_script_cannot_be_reported(member_session):
    version = publish(member_session, title="內建")
    assert service_for(member_session).script_detail(player(member_session), version.public_id)["report_state"] == "unavailable"


def test_the_authors_own_script_cannot_be_reported(member_session):
    author = player(member_session, 2)
    version = publish(member_session, author, title="我的")
    assert service_for(member_session).script_detail(author, version.public_id)["report_state"] == "unavailable"


def test_a_script_the_reader_already_reported_shows_as_reported_even_after_it_was_closed(member_session):
    author, reader = player(member_session, 2), player(member_session, 3)
    version = publish(member_session, author, title="被檢舉")
    report_it(member_session, reader, version, closed=CLOSED_DISMISSED)
    assert service_for(member_session).script_detail(reader, version.public_id)["report_state"] == "reported"


def test_only_the_readers_own_report_counts_not_other_players_reports(member_session):
    """別人檢舉過不影響我的狀態（也不洩漏有人檢舉過）：A 檢舉後，B 看到的仍是 `available`、回應也沒有任何計數。"""
    author, first, second = player(member_session, 2), player(member_session, 3), player(member_session, 4)
    version = publish(member_session, author, title="被檢舉")
    report_it(member_session, first, version)
    service = service_for(member_session)
    assert service.script_detail(first, version.public_id)["report_state"] == "reported"
    detail = service.script_detail(second, version.public_id)
    assert detail["report_state"] == "available" and set(detail) == set(DETAIL_FIELDS)
    assert service.script_detail(author, version.public_id)["report_state"] == "unavailable"       # 作者看不到有人檢舉


def test_a_new_version_starts_available_again_for_someone_who_reported_the_old_one(member_session):
    author, reader = player(member_session, 2), player(member_session, 3)
    repo = ScriptRepository(member_session)
    first = publish(member_session, author, title="第一版")
    report_it(member_session, reader, first)
    script_id = repo.get_script_of(first).public_id
    second = repo.create_draft_version(
        author.id, script_id, content=validated(title="第二版").content, content_hash=validated(title="第二版").content_hash,
        total_chars=validated(title="第二版").total_chars,
    )
    repo.submit_for_review(author.id, second.public_id, now=TODAY_UTC)
    repo.approve_version(second.public_id, now=TODAY_UTC + timedelta(minutes=1))
    member_session.commit()
    assert service_for(member_session).script_detail(reader, second.public_id)["report_state"] == "available"


def test_a_retained_anonymous_script_can_be_reported_and_is_not_shown_as_builtin(member_session):
    """匿名保留的玩家劇本沒有作者關聯、但沒有種子鍵：它是 `available`（不是 `unavailable`），筆名是「已註銷的作者」而不是「官方」。"""
    author = player(member_session, 2)
    version = publish(member_session, author, title="保留的")
    ScriptRepository(member_session).retain_published_anonymously(author.id)
    member_session.commit()
    detail = service_for(member_session).script_detail(player(member_session, 3), version.public_id)
    assert detail["report_state"] == "available" and detail["author_name"] == "已註銷的作者" and detail["author_name"] != OFFICIAL_AUTHOR_NAME


def test_a_players_script_detail_costs_one_extra_query_for_the_report_state_whatever_the_size(member_session):
    service, reader, author = service_for(member_session), player(member_session, 3), player(member_session, 2)
    small = publish(member_session, author, title="小", tags=["甲"], characters=[{"name": "甲", "personality": "x", "age": 1, "appearance": "y"}])
    big = publish(member_session, author, now=TODAY_UTC + timedelta(seconds=1), **hidden_script(title="大", tags=["甲", "乙", "丙", "丁", "戊"]))
    counts = []
    for public_id in (small.public_id, big.public_id):
        member_session.expire_all()
        with count_statements(member_session) as statements:
            service.script_detail(reader, public_id)
        counts.append(len(statements))
    assert counts[0] == counts[1] == 3                                           # 版本與劇本、標籤、自己是否檢舉過（內建劇本不用查）


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
    save = open_save(member_session, alice, title="青雲問道")
    summary = service_for(member_session).session_summary(alice, save.public_id)
    assert set(summary) == set(SUMMARY_FIELDS)
    assert summary == {"id": save.public_id, "title": "青雲問道", "content_tier": "safe", "player_name": "林初", "ended": False}


# ---- 存檔的內容模式是現算的（不是開存檔當下釘住的值）-----------------------------------------------------------

def open_save_of(session, user, binding, title="存檔", **overrides):
    """開一個存檔，劇本複本的綁定為 `binding`；`sessions.content_tier` 欄位故意寫成與現算不同的值，證明新版程式不讀它。"""
    save = open_save(session, user, title=title, **overrides)
    save.script_snapshot_json = {**save.script_snapshot_json, "content_tier": binding}
    session.commit()
    return save


def mode_of_listed_save(session, user, save, **service_kwargs):
    service = service_for(session, **service_kwargs)
    listed = {item["id"]: item["content_tier"] for item in service.my_sessions(user)["sessions"]}
    summary = service.session_summary(user, save.public_id)["content_tier"]
    assert listed[save.public_id] == summary                         # 清單與存檔摘要是同一個來源
    return summary


def test_the_listed_mode_follows_the_players_current_setting_not_the_mode_at_open_time(member_session):
    reader = player(member_session, content_tier_preference="safe")
    save = open_save_of(member_session, reader, "adult", content_tier="safe")                  # 開存檔當下是 safe、劇本未綁定
    assert mode_of_listed_save(member_session, reader, save) == "safe"
    user = member_session.get(User, reader.id)
    user.content_tier_preference, user.adult_confirmed_at, user.adult_rules_version = "adult", TODAY_UTC, SAFE_RULES_VERSION
    member_session.commit()
    relaxed = CurrentUser.from_model(user)
    assert mode_of_listed_save(member_session, relaxed, save) == "adult"                      # 玩家關閉安全模式之後，下一次讀取就是 adult


def test_a_save_of_a_bound_script_is_always_safe_even_after_the_player_turned_the_safe_mode_off(member_session):
    relaxed = player(member_session, content_tier_preference="adult", **AGREED)
    save = open_save_of(member_session, relaxed, "safe", content_tier="adult")
    assert mode_of_listed_save(member_session, relaxed, save) == "safe"


def test_the_legacy_standard_binding_in_a_snapshot_reads_as_unbound(member_session):
    relaxed = player(member_session, content_tier_preference="adult", **AGREED)
    save = open_save_of(member_session, relaxed, "standard", content_tier="safe")
    assert mode_of_listed_save(member_session, relaxed, save) == "adult"


def test_a_snapshot_without_a_readable_binding_is_treated_as_bound(member_session):
    relaxed = player(member_session, content_tier_preference="adult", **AGREED)
    for n, broken in enumerate(("", "mature"), start=1):
        save = open_save_of(member_session, relaxed, broken, title=f"壞的{n}")
        assert mode_of_listed_save(member_session, relaxed, save) == "safe"


def test_the_emergency_brake_makes_every_listed_save_safe(member_session):
    relaxed = player(member_session, content_tier_preference="adult", **AGREED)
    save = open_save_of(member_session, relaxed, "adult")
    assert mode_of_listed_save(member_session, relaxed, save, safe_mode_forced=True) == "safe"
    assert mode_of_listed_save(member_session, relaxed, save) == "adult"                      # 煞車解除後恢復


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
