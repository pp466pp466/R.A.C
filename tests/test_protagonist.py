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


# ---- 戀愛、婚姻與婚約不論年齡一律開放，只有性的部分與性奴等禁斷關係不行（使用者 2026-11-09）--------------------------


def test_romance_is_open_at_every_age_combination(script):
    """使用者 2026-11-09：「戀愛關係也放行，只有性方面不行」——成年與未成年之間（不論誰是主角）、兩位都未成年、兩位都成年，都通過。"""
    for text in ("舊日的戀人", "他的道侶", "暗戀已久的對象", "青梅竹馬，從國中起就在交往"):
        for ref, age in (("林曉", 25), ("蘇長風", 16), ("林曉", 16), ("蘇長風", 30), ("蘇長風", 10)):
            result, errors = check(anchor(ref=ref, text=text), script, age=age)
            assert errors == {} and result["text"] == text, (text, ref, age)
    assert check(anchor(ref="蘇長風", text="他的同門師兄"), script, age=16)[1] == {}


def test_marriage_and_betrothal_are_open_at_every_age_combination(script):
    """使用者 2026-11-09：「婚姻類一律開放不論年齡，性部分則不行」——成年與未成年之間、兩位都未成年、兩位都成年，結婚、成親、夫妻、童養媳、
    娃娃親、指腹為婚、未婚夫妻與各種同義詞（結姻、聯姻、許配、成婚…）都通過。"""
    texts = (
        "他的未婚妻", "指腹為婚的對象", "訂有婚約", "兩人已經成親", "被賣去當童養媳", "他們是夫妻", "新婚的新郎", "出生就和他結姻", "兩家聯姻", "被許配給他",
        "已經下聘", "與他成婚", "已經完婚", "兩家自幼訂下娃娃親",
    )
    for text in texts:
        for ref, age in (("林曉", 25), ("蘇長風", 16), ("林曉", 16), ("蘇長風", 30), ("蘇長風", 10)):         # 成年→未成年、未成年→成年、兩位未成年、兩位成年、幼童→成年
            result, errors = check(anchor(ref=ref, text=text), script, age=age)
            assert errors == {} and result["text"] == text, (text, ref, age)


def test_marriage_and_betrothal_stay_open_for_places_and_milestones_at_every_age(script):
    for kind, ref in (("location", "古劍山莊"), ("milestone", "ceremony")):
        for age in (10, 16, 25):
            assert check({"kind": kind, "ref": ref, "text": "兩家在此指腹為婚，後來結為夫妻"}, script, age=age)[1] == {}, (kind, age)


def test_a_marriage_is_still_refused_when_it_is_a_sexual_link_or_sexual_slavery(script):
    for text in ("兩人已經成親，發生過性關係", "新婚之夜情慾糾纏", "被賣去當童養媳的性奴"):
        for ref, age in (("林曉", 16), ("蘇長風", 16), ("林曉", 25)):
            result, errors = check(anchor(ref=ref, text=text), script, age=age)
            assert result is None and list(errors) == ["anchor.text"] and text not in str(errors), (text, ref, age)


def test_two_minors_may_have_a_romantic_link_such_as_childhood_sweethearts(script):
    """例如青梅竹馬、國中時就開始交往；用符合世界觀的說法（修仙界的同門情侶、道侶之約）也一樣。"""
    for text in ("青梅竹馬，從國中起就在交往", "舊日的戀人", "同門的情侶", "兩人互許終身的道侶", "暗戀多年的同窗"):
        result, errors = check(anchor(ref="林曉", text=text), script, age=16)
        assert errors == {} and result["text"] == text, text


def test_a_sexual_link_is_refused_when_either_side_is_a_minor_and_the_message_does_not_echo_it(script):
    for text in ("兩人發生過性關係", "彼此情慾糾纏"):
        for ref, age in (("林曉", 16), ("林曉", 25), ("蘇長風", 16)):
            result, errors = check(anchor(ref=ref, text=text), script, age=age)
            assert result is None and list(errors) == ["anchor.text"] and text not in str(errors), (text, ref, age)


def test_sexual_slavery_and_other_forbidden_relations_are_refused_at_every_age(script):
    for text in ("他是我的性奴", "性奴隸般的主僕", "被他凌辱多年"):
        for ref, age in (("林曉", 16), ("林曉", 25), ("蘇長風", 25), ("蘇長風", 16)):
            result, errors = check(anchor(ref=ref, text=text), script, age=age)
            assert result is None and list(errors) == ["anchor.text"] and text not in str(errors), (text, ref, age)
    assert check({"kind": "location", "ref": "古劍山莊", "text": "他曾在此被當作性奴"}, script, age=30)[1] != {}


def test_a_betrothal_between_two_adults_is_allowed(script):
    assert check(anchor(text="他的未婚妻"), script, age=25)[1] == {}


def test_a_betrothal_with_a_place_or_milestone_has_no_target_age_so_it_is_not_blocked(script):
    for kind, ref in (("location", "古劍山莊"), ("milestone", "ceremony")):
        for age in (16, 25):
            assert check({"kind": kind, "ref": ref, "text": "兩家在此指腹為婚"}, script, age=age)[1] == {}, (kind, age)


def test_a_romantic_link_with_a_minor_target_is_open_for_an_adult_protagonist_but_a_sexual_one_is_not(script):
    assert check(anchor(ref="林曉", text="舊日的戀人"), script, age=25)[1] == {}
    result, errors = check(anchor(ref="林曉", text="舊日的戀人，兩人發生過性關係"), script, age=25)
    assert result is None and list(errors) == ["anchor.text"]


def test_ordinary_links_pass_for_minors_and_adults(script):
    for age in (16, 25):
        for text in ("他的同門師弟", "同鄉的鄰家孩子", "結拜的兄妹"):
            assert check(anchor(ref="林曉", text=text), script, age=age)[1] == {}, (age, text)


def test_romance_with_locations_and_milestones_is_open_at_every_age(script):
    for age in (16, 25):
        assert check(anchor(text="舊日的戀人"), script, age=age)[1] == {}
        assert check({"kind": "location", "ref": "古劍山莊", "text": "與戀人初識之地"}, script, age=age)[1] == {}
        assert check({"kind": "milestone", "ref": "ceremony", "text": "他的戀人也會到場"}, script, age=age)[1] == {}


def test_the_adult_age_is_the_one_the_caller_provides_not_a_constant(script):
    link = anchor(text="兩人發生過性關係")
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


# ---- 來歷（rpg-turn-engine：主角有來歷）-----------------------------------------------------------


def test_a_card_without_an_origin_is_a_native_and_the_origin_is_always_present():
    character, errors = pro.validate_protagonist(card())
    assert errors == {} and character["origin"] == "native"
    again, _ = pro.validate_protagonist(card(origin=None))
    assert again["origin"] == "native"


def test_a_transmigrator_is_kept():
    character, errors = pro.validate_protagonist(card(origin="transmigrator"))
    assert errors == {} and character["origin"] == "transmigrator"


@pytest.mark.parametrize("bad", ["ghost", "", "Native", "TRANSMIGRATOR", 1, True, ["native"], {"v": "native"}])
def test_an_origin_outside_the_two_values_is_a_field_error_on_protagonist_origin(bad):
    character, errors = pro.validate_protagonist(card(origin=bad))
    assert character is None and list(errors) == ["protagonist.origin"]
    assert "ghost" not in errors["protagonist.origin"]                        # 錯誤訊息不回顯輸入


def test_the_origin_error_is_reported_together_with_the_other_field_errors():
    character, errors = pro.validate_protagonist(card(origin="ghost", name=""))
    assert character is None and {"protagonist.origin", "protagonist.name"} <= set(errors)


def test_the_origin_does_not_depend_on_the_age_of_the_character():
    for age in (3, 17, 18, 500):
        character, errors = pro.validate_protagonist(card(age=age, origin="transmigrator"))
        assert errors == {} and character["origin"] == "transmigrator"


def test_the_origin_is_not_run_through_the_text_checks_or_the_forbidden_markers():
    """來歷是列舉不是文字：不合法時只回報欄位錯誤；合法值不受標記字串檢查影響。"""
    character, errors = pro.validate_protagonist(card(origin="transmigrator"), forbidden_markers=["transmigrator"])
    assert errors == {} and character["origin"] == "transmigrator"


def test_the_stored_shape_carries_the_origin_and_an_old_save_reads_as_native():
    character, _ = pro.validate_protagonist(card(origin="transmigrator"))
    stored = pro.build_protagonist(character, None)
    assert stored["character"]["origin"] == "transmigrator" and pro.origin_of(stored["character"]) == "transmigrator"
    assert pro.origin_of({"name": "舊存檔", "age": 30}) == "native"           # 遷移之前建立的存檔沒有這個欄位
    assert pro.origin_of({"origin": "unknown"}) == "native"
    assert pro.origin_of(None) == "native"


def test_the_creation_screen_gets_the_two_options_with_native_first():
    assert pro.origin_options() == ["native", "transmigrator"]
