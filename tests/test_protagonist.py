"""玩家主角的角色卡與錨定事實（rpg-protagonist 規格，逐一對應其中的情境）。純運算，不需要資料庫。"""
import re

import pytest

from app.config import PROJECT_ROOT
from app.services import protagonist as pro
from tests.support.script_data import validated

SENTINEL = "ZZ不該出現的使用者輸入ZZ"
ADULT = 18


@pytest.fixture(scope="module")
def script():
    """一份標準化的劇本內容：一位 16 歲的學徒、一位成年的長老、兩個地點、兩個里程碑。"""
    return validated(
        characters=[
            {"name": "林曉", "personality": "活潑好動", "age": 16, "appearance": "身形瘦小", "occupation": "學徒"},
            {"name": "蘇長風", "personality": "沉穩寡言", "age": 168, "appearance": "鬢髮微白", "occupation": "長老"},
        ],
        locations=["古劍山莊", "Qing Yun"],
        milestones=[
            {"id": "sect-trial", "title": "宗門大測", "trigger": {"kind": "age", "value": 20}},
            {"id": "ceremony", "title": "收徒大典", "trigger": {"kind": "year", "value": 1215}},
        ],
    ).content


def card(**changes):
    base = {"name": "林舟", "personality": "沉默愛思考", "age": 19, "appearance": "高瘦，常穿灰衣"}
    base.update(changes)
    return base


def anchor(**changes):
    base = {"kind": "character", "ref": "蘇長風", "text": "他的舊識之後"}
    base.update(changes)
    return base


def check(raw, script, *, age=19, **kwargs):
    return pro.validate_anchor(raw, script_content=script, protagonist_age=age, adult_age=ADULT, **kwargs)


# ---- 角色卡 ------------------------------------------------------------------------------


def test_a_valid_card_is_normalized_with_the_script_character_rules():
    character, errors = pro.validate_protagonist(card(name="  林　舟 ", occupation=" 遊方郎中 "))
    assert errors == {} and character["name"] == "林 舟" and character["occupation"] == "遊方郎中" and character["image"] is None


def test_a_minor_protagonist_is_valid_and_age_has_nothing_to_do_with_the_user():
    character, errors = pro.validate_protagonist(card(age=15))
    assert errors == {} and character["age"] == 15


@pytest.mark.parametrize(("changes", "field"), [
    ({"appearance": None}, "protagonist.appearance"), ({"name": ""}, "protagonist.name"), ({"age": "十八歲"}, "protagonist.age"),
    ({"image": "https://example.com/a.png"}, "protagonist.image"), ({"personality": "字" * 61}, "protagonist.personality"),
])
def test_an_invalid_card_names_the_field_with_the_protagonist_prefix(changes, field):
    character, errors = pro.validate_protagonist(card(**changes))
    assert character is None and field in errors and all(key.startswith("protagonist") for key in errors)


def test_card_errors_do_not_echo_the_input():
    _, errors = pro.validate_protagonist(card(name=SENTINEL, age="x"))
    assert SENTINEL not in str(errors)


# ---- 自我介紹 ------------------------------------------------------------------------------


def test_the_intro_is_optional_normalized_and_limited_to_300_characters():
    assert pro.clean_intro(None) == (None, {}) and pro.clean_intro("") == (None, {}) and pro.clean_intro("  \n ") == (None, {})
    text, errors = pro.clean_intro("我是個​遊方郎中\r\n想找師父")
    assert errors == {} and text == "我是個遊方郎中\n想找師父"
    assert pro.clean_intro("字" * 300)[1] == {} and "intro" in pro.clean_intro("字" * 301)[1]
    assert "intro" in pro.clean_intro(123)[1]


def test_intro_errors_do_not_echo_and_markers_are_refused():
    _, errors = pro.clean_intro(SENTINEL * 40)
    assert SENTINEL not in str(errors)
    _, errors = pro.clean_intro("含有 Secret-Mark 的文字", forbidden_markers=["secret-mark"])
    assert list(errors) == ["intro"] and "secret" not in str(errors).lower()


# ---- 錨定事實：指向劇本中存在的項目 ---------------------------------------------------------------


def test_an_anchor_to_an_existing_character_is_accepted_with_the_scripts_spelling(script):
    result, errors = check(anchor(ref="  蘇長風 "), script)
    assert errors == {} and result == {"kind": "character", "ref": "蘇長風", "text": "他的舊識之後"}


def test_locations_are_matched_without_case_and_milestones_by_id(script):
    result, errors = check({"kind": "location", "ref": "qing yun", "text": "出生的村子"}, script)
    assert errors == {} and result["ref"] == "Qing Yun"
    result, errors = check({"kind": "milestone", "ref": "sect-trial", "text": "為了這場大測而來"}, script)
    assert errors == {} and result["ref"] == "sect-trial"


@pytest.mark.parametrize("bad", [
    anchor(ref="不存在的人"), anchor(kind="location", ref="蘇長風"), anchor(kind="milestone", ref="宗門大測"), anchor(kind="milestone", ref="SECT-TRIAL"),
    anchor(ref=""), anchor(ref=None), anchor(ref=123),
])
def test_a_reference_that_is_not_in_the_script_fails_at_anchor_ref(script, bad):
    result, errors = check(bad, script)
    assert result is None and "anchor.ref" in errors


def test_unknown_kind_missing_text_and_unknown_fields_are_field_errors(script):
    assert "anchor.kind" in check(anchor(kind="item"), script)[1]
    assert "anchor.text" in check(anchor(text=""), script)[1] and "anchor.text" in check(anchor(text=None), script)[1]
    assert "anchor.text" in check(anchor(text=5), script)[1]
    assert "anchor.extra" in check({**anchor(), "extra": 1}, script)[1]
    assert "anchor" in check("蘇長風", script)[1] and "anchor" in check(["x"], script)[1]


def test_text_is_one_line_up_to_60_characters(script):
    result, errors = check(anchor(text="師徒\n之間"), script)
    assert errors == {} and result["text"] == "師徒 之間"
    assert check(anchor(text="字" * 60), script)[1] == {} and "anchor.text" in check(anchor(text="字" * 61), script)[1]


def test_anchor_errors_do_not_echo_the_input_and_markers_are_refused(script):
    _, errors = check(anchor(ref=SENTINEL, text=SENTINEL * 40, kind=SENTINEL), script)
    assert SENTINEL not in str(errors)
    _, errors = check(anchor(text="含有 Secret-Mark 的文字"), script, forbidden_markers=["secret-mark"])
    assert list(errors) == ["anchor.text"] and "secret" not in str(errors).lower()


# ---- 未成年的主角或對象不得有戀愛或婚姻的聯繫 ------------------------------------------------------


def test_a_minor_protagonist_may_not_have_a_romantic_or_marriage_link(script):
    for text in ("舊日的戀人", "他的道侶", "兩人已經成親", "被賣去當童養媳"):
        result, errors = check(anchor(text=text), script, age=16)
        assert result is None and list(errors) == ["anchor.text"], text
        assert text not in str(errors)


def test_a_betrothal_between_an_adult_and_a_minor_is_refused_in_both_directions(script):
    """成年與未成年之間不能是婚約：成年主角指向未成年人物，或未成年主角指向成年人物，都擋。"""
    for text in ("他的未婚妻", "指腹為婚的對象", "訂有婚約"):
        adult_to_minor, errors = check(anchor(ref="林曉", text=text), script, age=25)
        assert adult_to_minor is None and list(errors) == ["anchor.text"], text
        minor_to_adult, errors = check(anchor(ref="蘇長風", text=text), script, age=16)
        assert minor_to_adult is None and list(errors) == ["anchor.text"], text
        assert text not in str(errors)


def test_a_betrothal_between_two_minors_is_a_historical_backdrop_and_is_allowed(script):
    """兩位都未成年的娃娃親與指腹為婚是歷史題材常見的背景設定；戀愛與已成婚的詞仍然擋。"""
    for text in ("兩家自幼訂下娃娃親", "指腹為婚的對象", "他的未婚妻"):
        result, errors = check(anchor(ref="林曉", text=text), script, age=16)
        assert errors == {} and result["text"] == text, text
    for text in ("兩人已經成親", "被賣去當童養媳", "舊日的戀人"):
        assert list(check(anchor(ref="林曉", text=text), script, age=16)[1]) == ["anchor.text"], text


def test_a_betrothal_between_two_adults_is_allowed(script):
    assert check(anchor(text="他的未婚妻"), script, age=25)[1] == {}


def test_a_betrothal_with_a_place_or_milestone_has_no_target_age_so_it_is_not_blocked(script):
    for kind, ref in (("location", "古劍山莊"), ("milestone", "ceremony")):
        for age in (16, 25):
            assert check({"kind": kind, "ref": ref, "text": "兩家在此指腹為婚"}, script, age=age)[1] == {}, (kind, age)


def test_a_minor_target_blocks_a_romantic_link_even_for_an_adult_protagonist(script):
    result, errors = check(anchor(ref="林曉", text="舊日的戀人"), script, age=25)
    assert result is None and list(errors) == ["anchor.text"]


def test_ordinary_links_pass_for_minors_and_adults(script):
    for age in (16, 25):
        for text in ("他的同門師弟", "同鄉的鄰家孩子", "結拜的兄妹"):
            assert check(anchor(ref="林曉", text=text), script, age=age)[1] == {}, (age, text)


def test_romance_between_adults_is_allowed_and_locations_and_milestones_only_look_at_the_protagonist(script):
    assert check(anchor(text="舊日的戀人"), script, age=25)[1] == {}
    assert check({"kind": "location", "ref": "古劍山莊", "text": "與戀人初識之地"}, script, age=25)[1] == {}
    assert check({"kind": "milestone", "ref": "ceremony", "text": "他的戀人也會到場"}, script, age=16)[1] != {}
    assert check({"kind": "location", "ref": "古劍山莊", "text": "與戀人初識之地"}, script, age=16)[1] != {}


def test_the_adult_age_is_the_one_the_caller_provides_not_a_constant(script):
    link = anchor(text="舊日的戀人")
    assert pro.validate_anchor(link, script_content=script, protagonist_age=19, adult_age=20)[0] is None
    assert pro.validate_anchor(link, script_content=script, protagonist_age=19, adult_age=18)[0] is not None


def test_a_romantic_word_outside_the_anchor_text_does_not_trigger_the_guard(script):
    """守衛只看錨定事實的文字：角色卡裡的詞、其他欄位都不觸發（由審查層另行處理）。"""
    character, errors = pro.validate_protagonist(card(age=15, personality="暗戀鄰家姐姐的少年"))
    assert errors == {} and character is not None
    assert check(anchor(text="他的同門師弟"), script, age=15)[1] == {}


def test_when_the_card_is_invalid_the_age_guard_is_skipped_so_the_player_fixes_the_card_first(script):
    result, errors = check(anchor(text="舊日的戀人"), script, age=None)
    assert errors == {} and result is not None


# ---- 開存檔時：帶錨定事實或明確略過 --------------------------------------------------------------


def choice(raw, skip, script, age=19):
    return pro.validate_anchor_choice(raw, skip, script_content=script, protagonist_age=age, adult_age=ADULT)


def test_the_anchor_choice_is_an_anchor_or_an_explicit_skip(script):
    result, errors = choice(anchor(), None, script)
    assert errors == {} and result["ref"] == "蘇長風"
    assert choice(None, True, script) == (None, {})
    assert "anchor" in choice(None, None, script)[1] and "anchor" in choice(None, False, script)[1]
    assert "anchor" in choice(anchor(), True, script)[1]
    assert "anchor" in choice(None, 1, script)[1] and "anchor" in choice(None, "true", script)[1]


def test_a_false_skip_with_an_anchor_is_just_the_anchor(script):
    result, errors = choice(anchor(), False, script)
    assert errors == {} and result is not None


# ---- 創角畫面的選項與存放形狀 -----------------------------------------------------------------------


def test_anchor_options_expose_names_titles_and_ids_but_never_ages_or_hidden_traits():
    content = validated(characters=[
        {"name": "花小蝶", "personality": "活潑", "age": 19, "appearance": "嬌小", "occupation": "跑堂", "hidden_trait": "善於算計"},
    ]).content
    options = pro.anchor_options(content)
    assert options["characters"] == [{"name": "花小蝶", "occupation": "跑堂"}]
    assert "善於算計" not in str(options) and "19" not in str(options["characters"])
    assert options["locations"] == content["locations"] and options["milestones"][0]["id"] == content["milestones"][0]["id"]


def test_build_protagonist_has_the_stored_shape_and_does_not_alias_its_inputs():
    character = {"name": "林舟", "age": 19}
    stored = pro.build_protagonist(character, {"kind": "location", "ref": "古劍山莊", "text": "出生地"})
    assert set(stored) == {"character", "anchor"} and stored["character"] == character and stored["character"] is not character
    assert pro.build_protagonist(character, None)["anchor"] is None


def test_the_module_is_pure_without_database_network_or_model_imports():
    source = (PROJECT_ROOT / "app" / "services" / "protagonist.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.M)
    forbidden = ("sqlalchemy", "flask", "requests", "httpx", "urllib", "socket", "app.repositories", "app.llm", "app.db", "datetime", "time")
    assert not [name for name in imports if name.startswith(forbidden)], imports
