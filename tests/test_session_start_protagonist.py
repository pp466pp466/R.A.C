"""rpg-protagonist 規格的開存檔：主角角色卡與錨定事實的驗證、規則層、模型層審查、失敗時不留下半個存檔，以及存放與同步。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；審查用替身（`tests/support/review.py`），真正的模型層另見
`test_protagonist_review.py`。
"""
import logging

import pytest
from sqlalchemy import select

from app.models import RpgSession
from app.services.errors import ProtagonistRejected, ReviewUnavailable, ScriptRejected, ValidationError
from tests.support.protagonist import character, start_kwargs
from tests.support.review import PassingProtagonistReviews, PassingReviews
from tests.test_session_service import count, draft_script, player, publish_script, service_for

NPC_ADULT = {"name": "蘇長風", "personality": "沉穩寡言", "age": 168, "appearance": "鬢髮微白", "occupation": "長老"}
NPC_MINOR = {"name": "林曉", "personality": "活潑好動", "age": 16, "appearance": "瘦小", "occupation": "學徒"}
ANCHOR = {"kind": "character", "ref": "蘇長風", "text": "他的舊識之後"}


def script(session, **changes):
    changes.setdefault("characters", [NPC_MINOR, NPC_ADULT])
    return publish_script(session, **changes)


def open_save(service, user, version, **kwargs):
    return service.start_session(user, version.public_id, **kwargs)


def kw(**changes):
    """帶錨定事實的預設請求。"""
    return start_kwargs(name="林舟", age=19, anchor=ANCHOR, **changes)


# ---- 成功與存放 ----------------------------------------------------------------------------------


def test_a_valid_card_with_an_anchor_opens_a_save_that_stores_both_and_syncs_name_and_age(member_session):
    service, clock = service_for(member_session)
    user, version = player(member_session), script(member_session)
    save = open_save(service, user, version, **kw(occupation="遊方郎中", speech_style="說話慢"))
    member_session.commit()
    member_session.expire_all()
    stored = member_session.scalar(select(RpgSession))
    assert (stored.player_name, stored.character_age) == ("林舟", 19)
    assert stored.protagonist_json["character"]["name"] == "林舟" and stored.protagonist_json["character"]["occupation"] == "遊方郎中"
    assert stored.protagonist_json["character"]["image"] is None and stored.protagonist_json["anchor"] == ANCHOR
    assert stored.public_id == save.public_id and stored.last_active_at == clock.now


def test_skipping_the_anchor_opens_a_save_with_the_card_and_no_anchor(member_session):
    service, _ = service_for(member_session)
    save = open_save(service, player(member_session), script(member_session), **start_kwargs(name="林舟", age=19))
    assert save.protagonist_json["anchor"] is None and save.protagonist_json["character"]["name"] == "林舟"


def test_the_stored_card_is_normalised_and_the_anchor_uses_the_scripts_spelling(member_session):
    service, _ = service_for(member_session)
    save = open_save(service, player(member_session), script(member_session), **start_kwargs(
        name="  林\n  舟 ", age=19, anchor={"kind": "character", "ref": " 蘇長風", "text": "  他的\n舊識之後 "},
    ))
    assert save.protagonist_json["character"]["name"] == "林 舟" and save.player_name == "林 舟"
    assert save.protagonist_json["anchor"] == {"kind": "character", "ref": "蘇長風", "text": "他的 舊識之後"}


def test_a_minor_protagonist_is_fine_and_the_players_own_age_has_nothing_to_do_with_it(member_session):
    service, _ = service_for(member_session)
    adult_player = player(member_session)
    save = open_save(service, adult_player, script(member_session), **start_kwargs(name="小舟", age=10, anchor={"kind": "location", "ref": "古劍山莊", "text": "出生的地方"}))
    assert save.character_age == 10
    teen = player(member_session, 2, age=15)
    assert open_save(service, teen, script(member_session, title="另一個"), **start_kwargs(name="老舟", age=300)).character_age == 300


def test_each_save_has_its_own_protagonist(member_session):
    service, _ = service_for(member_session)
    user, version = player(member_session), script(member_session)
    first = open_save(service, user, version, **start_kwargs(name="甲", age=20))
    second = open_save(service, user, version, **start_kwargs(name="乙", age=21))
    assert (first.player_name, second.player_name) == ("甲", "乙") and count(member_session, RpgSession) == 2


# ---- 錨定事實：二選一與指向 --------------------------------------------------------------------------


@pytest.mark.parametrize(("anchor", "skip", "field"), [
    (None, None, "anchor"), (None, False, "anchor"), (ANCHOR, True, "anchor"), (None, "yes", "anchor"), (None, 1, "anchor"),
    ({"kind": "character", "ref": "沒有這個人", "text": "舊識"}, None, "anchor.ref"),
    ({"kind": "location", "ref": "蘇長風", "text": "舊識"}, None, "anchor.ref"),
    ({"kind": "milestone", "ref": "宗門大測", "text": "舊識"}, None, "anchor.ref"),
    ({"kind": "item", "ref": "x", "text": "舊識"}, None, "anchor.kind"),
    ({"kind": "character", "ref": "蘇長風", "text": ""}, None, "anchor.text"),
    ({"kind": "character", "ref": "蘇長風", "text": "字" * 61}, None, "anchor.text"),
    ("蘇長風", None, "anchor"),
])
def test_a_missing_conflicting_or_invalid_anchor_is_a_400_naming_the_field_and_leaves_no_save(member_session, anchor, skip, field):
    reviews, protagonist_reviews = PassingReviews(), PassingProtagonistReviews()
    service, _ = service_for(member_session, reviews=reviews, protagonist_reviews=protagonist_reviews)
    with pytest.raises(ValidationError) as caught:
        open_save(service, player(member_session), script(member_session), protagonist=character("林舟", 19), anchor=anchor, skip_anchor=skip)
    assert field in caught.value.fields
    assert count(member_session, RpgSession) == 0 and reviews.calls == [] and protagonist_reviews.calls == []      # 便宜的檢查在前，沒有呼叫任何審查


def test_all_field_errors_are_reported_at_once(member_session):
    service, _ = service_for(member_session)
    with pytest.raises(ValidationError) as caught:
        open_save(service, player(member_session), script(member_session), protagonist=character("", 19, appearance=...), anchor=None, skip_anchor=None)
    assert {"protagonist.name", "protagonist.appearance", "anchor"} <= set(caught.value.fields)


@pytest.mark.parametrize("bad", [None, "林舟", [], 7])
def test_a_protagonist_that_is_not_a_mapping_is_a_field_error(member_session, bad):
    service, _ = service_for(member_session)
    with pytest.raises(ValidationError) as caught:
        open_save(service, player(member_session), script(member_session), protagonist=bad, anchor=None, skip_anchor=True)
    assert "protagonist" in caught.value.fields and count(member_session, RpgSession) == 0


# ---- 未成年的戀愛或婚姻聯繫 --------------------------------------------------------------------------


@pytest.mark.parametrize(("age", "ref", "text"), [
    (16, "蘇長風", "他的未婚妻"),              # 未成年主角與成年對象的婚約
    (30, "林曉", "指腹為婚的對象"),            # 成年主角與未成年對象的婚約
    (30, "林曉", "舊日的戀人"), (17, "林曉", "兩人已經成親"), (16, "林曉", "被賣去當童養媳"),
])
def test_a_romantic_marriage_or_adult_minor_betrothal_link_is_refused_for_a_minor_in_the_pair(member_session, age, ref, text):
    service, _ = service_for(member_session)
    with pytest.raises(ValidationError) as caught:
        open_save(service, player(member_session), script(member_session), **start_kwargs(name="林舟", age=age, anchor={"kind": "character", "ref": ref, "text": text}))
    assert list(caught.value.fields) == ["anchor.text"] and text not in str(caught.value.fields)
    assert count(member_session, RpgSession) == 0


def test_two_minors_with_a_romantic_childhood_link_open_a_save_but_a_marriage_or_sexual_slavery_does_not(member_session):
    """使用者 2026-11-09：青梅竹馬、國中時就開始交往是可以的；婚姻關係與性奴等禁斷關係仍不行。"""
    service, _ = service_for(member_session)
    user, version = player(member_session), script(member_session)
    save = open_save(service, user, version, **start_kwargs(name="小舟", age=16, anchor={"kind": "character", "ref": "林曉", "text": "青梅竹馬的戀人，國中時就開始交往"}))
    assert save.protagonist_json["anchor"]["text"] == "青梅竹馬的戀人，國中時就開始交往"
    for text in ("兩人已經成親", "他是我的性奴"):
        with pytest.raises(ValidationError) as caught:
            open_save(service, user, version, **start_kwargs(name="小舟", age=16, anchor={"kind": "character", "ref": "林曉", "text": text}))
        assert list(caught.value.fields) == ["anchor.text"] and text not in str(caught.value.fields)


def test_a_betrothal_between_two_minors_opens_a_save_as_a_historical_backdrop(member_session):
    service, _ = service_for(member_session)
    save = open_save(service, player(member_session), script(member_session), **start_kwargs(name="小舟", age=17, anchor={"kind": "character", "ref": "林曉", "text": "兩家自幼訂下娃娃親"}))
    assert save.protagonist_json["anchor"]["text"] == "兩家自幼訂下娃娃親"


def test_ordinary_links_pass_for_minors_and_romance_passes_between_adults(member_session):
    service, _ = service_for(member_session)
    user, version = player(member_session), script(member_session)
    assert open_save(service, user, version, **start_kwargs(name="小舟", age=16, anchor={"kind": "character", "ref": "林曉", "text": "同門的師弟"}))
    assert open_save(service, user, version, **start_kwargs(name="大舟", age=30, anchor={"kind": "character", "ref": "蘇長風", "text": "舊日的戀人"}))


# ---- 規則層：直接拒絕，且早於任何模型呼叫 ------------------------------------------------------------------


@pytest.mark.parametrize("changes", [
    {"personality": "十歲的少女與他做愛。"},
    {"appearance": "高中生之間發生了性交。"},
    {"hidden_trait": "他們做愛，她被迫屈服。"},
])
def test_an_explicit_red_line_in_the_card_is_a_422_with_categories_only_and_no_model_calls(member_session, changes):
    reviews, protagonist_reviews = PassingReviews(), PassingProtagonistReviews()
    service, _ = service_for(member_session, adult_mode=True, reviews=reviews, protagonist_reviews=protagonist_reviews)
    adult = player(member_session, content_tier_preference="adult", adult_confirmed_at=None)
    with pytest.raises(ProtagonistRejected) as caught:
        open_save(service, adult, script(member_session), **start_kwargs(name="林舟", age=30, **changes))
    assert caught.value.categories and set(caught.value.categories) <= {"minor_sexual", "non_consensual"}
    assert "做愛" not in caught.value.message and count(member_session, RpgSession) == 0
    assert reviews.calls == [] and protagonist_reviews.calls == []


def test_a_minor_protagonist_whose_own_text_is_explicit_is_rejected_even_when_a_script_would_only_be_flagged(member_session):
    service, _ = service_for(member_session)
    with pytest.raises(ProtagonistRejected) as caught:
        open_save(service, player(member_session), script(member_session), **start_kwargs(name="小舟", age=15, personality="喜歡性愛的少年"))
    assert caught.value.categories == ("minor_sexual",)


def test_the_rules_layer_looks_at_the_anchor_text_too(member_session):
    service, _ = service_for(member_session)
    with pytest.raises(ProtagonistRejected):
        open_save(service, player(member_session), script(member_session), **start_kwargs(
            name="林舟", age=30, anchor={"kind": "character", "ref": "蘇長風", "text": "十歲的少女與他做愛。"},
        ))


def test_an_ordinary_young_protagonist_is_not_misjudged(member_session):
    service, _ = service_for(member_session)
    assert open_save(service, player(member_session), script(member_session), **start_kwargs(name="小舟", age=10, personality="天真愛哭的學童"))


# ---- 模型層：拒絕、有疑慮、不可用 -----------------------------------------------------------------------


def test_a_model_layer_reject_is_a_422_and_no_save_is_created(member_session):
    protagonist_reviews = PassingProtagonistReviews(verdict="reject", categories=("minor_sexual",))
    service, _ = service_for(member_session, protagonist_reviews=protagonist_reviews)
    with pytest.raises(ProtagonistRejected) as caught:
        open_save(service, player(member_session), script(member_session), **kw())
    assert caught.value.categories == ("minor_sexual",) and count(member_session, RpgSession) == 0 and len(protagonist_reviews.calls) == 1


def test_a_model_layer_flag_is_allowed_because_a_protagonist_has_no_human_review_queue(member_session):
    protagonist_reviews = PassingProtagonistReviews(verdict="flag", categories=("tier_mismatch",))
    service, _ = service_for(member_session, protagonist_reviews=protagonist_reviews)
    assert open_save(service, player(member_session), script(member_session), **kw()).status == "active"


@pytest.mark.parametrize("reason", ["not_configured", "budget", "degraded", "blocked"])
def test_when_the_model_layer_is_unavailable_nothing_is_opened(member_session, reason):
    service, _ = service_for(member_session, protagonist_reviews=PassingProtagonistReviews(verdict=None, unavailable=reason))
    with pytest.raises(ReviewUnavailable):
        open_save(service, player(member_session), script(member_session), **kw())
    assert count(member_session, RpgSession) == 0


def test_the_model_layer_gets_the_final_card_the_script_content_and_the_saves_tier(member_session):
    protagonist_reviews = PassingProtagonistReviews()
    service, _ = service_for(member_session, protagonist_reviews=protagonist_reviews)
    user = player(member_session)
    open_save(service, user, script(member_session, content_tier="safe"), **kw())
    [(user_id, stored, tier)] = protagonist_reviews.calls
    assert user_id == user.id and tier == "safe" and stored["character"]["name"] == "林舟" and stored["anchor"] == ANCHOR


def test_a_rejected_script_stops_before_the_protagonist_model_review(member_session):
    reviews, protagonist_reviews = PassingReviews(verdict="reject", categories=("minor_sexual",)), PassingProtagonistReviews()
    service, _ = service_for(member_session, reviews=reviews, protagonist_reviews=protagonist_reviews)
    owner = player(member_session)
    draft = draft_script(member_session, owner, characters=[NPC_MINOR, NPC_ADULT])
    with pytest.raises(ScriptRejected):
        open_save(service, owner, draft, **kw())
    assert protagonist_reviews.calls == [] and count(member_session, RpgSession) == 0


def test_the_builtin_script_skips_the_script_review_but_the_protagonist_is_still_reviewed(member_session):
    reviews, protagonist_reviews = PassingReviews(), PassingProtagonistReviews()
    service, _ = service_for(member_session, reviews=reviews, protagonist_reviews=protagonist_reviews)
    open_save(service, player(member_session), publish_script(member_session), **start_kwargs(name="林舟", age=19))        # owner 為空：內建劇本
    assert reviews.calls == [] and len(protagonist_reviews.calls) == 1


# ---- 事件與個資 --------------------------------------------------------------------------------------


def test_the_session_started_event_carries_only_enumerations(member_session, caplog):
    service, _ = service_for(member_session)
    user = player(member_session)
    with caplog.at_level(logging.INFO, logger="app.protagonist"):
        open_save(service, user, script(member_session), **kw())
        open_save(service, user, script(member_session, title="另一個"), **start_kwargs(name="林舟", age=19))
    events = [record for record in caplog.records if getattr(record, "event", "") == "session.started"]
    assert [(r.kind, r.outcome, r.user_id) for r in events] == [("safe", "anchored", user.id), ("safe", "skipped", user.id)]
    for record in caplog.records:
        assert "林舟" not in f"{record.getMessage()} {vars(record)}" and "舊識" not in f"{record.getMessage()} {vars(record)}"


def test_a_rejection_logs_the_stage_the_layer_and_the_categories_only(member_session, caplog):
    service, _ = service_for(member_session)
    with caplog.at_level(logging.INFO, logger="app.protagonist"):
        with pytest.raises(ProtagonistRejected):
            open_save(service, player(member_session), script(member_session), **start_kwargs(name="小舟", age=15, personality="喜歡性愛的少年"))
    [record] = [r for r in caplog.records if getattr(r, "event", "") == "protagonist.rejected"]
    assert (record.kind, record.outcome, record.reason) == ("start", "rules", "minor_sexual")
    assert "喜歡性愛" not in f"{record.getMessage()} {vars(record)}"
