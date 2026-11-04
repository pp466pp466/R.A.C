"""script-review 規格的規則層：確定性、拆字閃避、四類紅線的明確命中、誤擋防線、有疑慮的標示、結果不回顯內容。

規則層是純函式，這裡不需要資料庫。
"""
import json
import re
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.models.script import (
    FLAG_CATEGORIES, REJECT_CATEGORIES, REVIEW_CATEGORIES, REVIEW_FLAG, REVIEW_PASS, REVIEW_REJECT,
)
from app.services.script_rules import LEXICON_PATH, RulesResult, check_rules, load_lexicon
from tests.support.script_data import valid, validated


def check(tier="safe", adult_age=18, **changes):
    """對 `valid(**changes)` 標準化後的內容跑規則層（宣告的內容模式由 tier 決定）。"""
    content = validated(content_tier=tier, **changes).content
    return check_rules(content, tier=tier, adult_age=adult_age)


def character(name="林曉", age=30, personality="沉穩", appearance="黑髮黑眼", **extra):
    return {"name": name, "personality": personality, "age": age, "appearance": appearance, **extra}


# ---- 確定性與純函式 ----------------------------------------------------------------------


def test_the_same_input_gives_the_same_result():
    one = check(background="他們做愛。")
    two = check(background="他們做愛。")
    assert one == two and isinstance(one, RulesResult)


def test_the_result_carries_the_lexicon_version_and_nothing_else_about_the_input():
    result = check()
    assert result.lexicon_version == load_lexicon().version
    assert set(RulesResult.__dataclass_fields__) == {"verdict", "categories", "lexicon_version"}


def test_the_module_is_a_pure_function_without_database_network_or_model_imports():
    source = (PROJECT_ROOT / "app" / "services" / "script_rules.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.M)
    forbidden = ("sqlalchemy", "flask", "requests", "httpx", "urllib", "socket", "app.repositories", "app.llm", "app.db")
    assert not [name for name in imports if name.startswith(forbidden)], imports


def test_the_lexicon_is_a_utf8_data_file_with_a_version_and_all_groups():
    data = json.loads(Path(LEXICON_PATH).read_text(encoding="utf-8-sig"))
    assert data["version"] and set(data["groups"]) == {
        "sexual_explicit", "sexual_suggestive", "minor", "rape", "sexual_violence", "non_consent", "real_person",
    }
    assert all(data["groups"][name] for name in data["groups"])
    lexicon = load_lexicon()
    assert lexicon.version == data["version"] and lexicon.minor and lexicon.real_person


def test_the_shipped_builtin_script_passes_under_its_declared_tier():
    shipped = json.loads((PROJECT_ROOT / "app" / "data" / "scripts" / "xianxia-qingyun.json").read_text(encoding="utf-8-sig"))
    content = validated(**{k: v for k, v in shipped["content"].items() if k != "spec_version"}).content
    assert check_rules(content, tier=content["content_tier"]) == RulesResult(REVIEW_PASS, (), load_lexicon().version)


# ---- 四類紅線：同句共現才直接拒絕 ----------------------------------------------------------


@pytest.mark.parametrize("sentence", [
    "十歲的少女與他做愛。", "那個小學生被迫發生性交。", "未成年的孩子們自慰。", "她只有 15 歲，他們有了性行為。", "十七歲的她和他口交。",
    "蘿莉角色的床戲場景。", "高中生之間發生了性交。", "二十歲的青年與三歲的幼童做愛。",
])
def test_an_explicit_sentence_with_a_minor_is_rejected_as_minor_sexual(sentence):
    result = check(background=sentence)
    assert result.verdict == REVIEW_REJECT and "minor_sexual" in result.categories


def test_the_age_threshold_comes_from_the_caller_not_from_the_module():
    sentence = "十九歲的他們做愛。"
    assert check(background=sentence).verdict != REVIEW_REJECT                                    # 預設門檻 18：十九歲是成年
    assert check(background=sentence, adult_age=20).categories == ("minor_sexual",)               # 門檻 20：十九歲算未成年


# 直接拒絕的主軸：露骨的性用語與未成年用語出現在同一句，**不分先後**
@pytest.mark.parametrize("sentence", [
    "高中生與老師發生性關係。", "發生性關係的是兩位高中生。", "老師與國中生發生性關係。", "他們發生性行為，對象是初中生。",
    "中學生之間發生了性交。", "發生性交的雙方都是未成年人。", "幼女被他侵犯而發生性行為。",
])
def test_an_explicit_term_before_or_after_a_minor_term_in_the_same_sentence_is_rejected(sentence):
    result = check(tier="adult", background=sentence)
    assert result.verdict == REVIEW_REJECT and result.categories == ("minor_sexual",)


@pytest.mark.parametrize("sentence", [
    "他在異國中生存了三年，歸來後與妻子發生性關係。", "他與妻子在美國中生活多年，常常發生性關係。",
    "他們看完中國中生代化石展，回家發生性關係。", "他在祖國中生活的那幾年，與妻子發生性行為。",
])
def test_words_that_merely_contain_a_school_term_are_not_mistaken_for_a_minor(sentence):
    """每句都帶有露骨的性用語：沒有排除字的話，這些句子會被當成「國中生」而誤擋。"""
    assert check(tier="adult", background=sentence).verdict == REVIEW_PASS


@pytest.mark.parametrize("sentence", ["他們做愛，她被迫屈服。", "他迷暈她後發生性交。", "違背意願的性行為。"])
def test_an_explicit_sentence_with_non_consent_is_rejected(sentence):
    result = check(background=sentence)
    assert result.verdict == REVIEW_REJECT and "non_consensual" in result.categories


@pytest.mark.parametrize("sentence", [
    "他強姦了她。", "反派輪姦了俘虜。", "他因強姦罪被判處五年徒刑，出獄後在鎮上做木工。", "學堂開了一堂性侵害防治課，教導孩子保護自己。",
])
def test_a_rape_term_without_an_explicit_sexual_term_is_flagged_not_rejected(sentence):
    """犯罪劇情、新聞與防治課會提到這些詞，單獨出現不直接拒絕；標示後交給模型層（它會判斷是否為非自願的性情節）與人工。"""
    result = check(tier="adult", background=sentence)
    assert result == RulesResult(REVIEW_FLAG, ("other_concern",), load_lexicon().version)


@pytest.mark.parametrize("sentence", ["他強姦了她，兩人發生性行為。", "反派輪姦了俘虜，過程中發生性交。", "性侵害之後是肛交。"])
def test_a_rape_term_in_the_same_sentence_as_an_explicit_sexual_term_is_still_rejected(sentence):
    result = check(background=sentence)
    assert result.verdict == REVIEW_REJECT and "non_consensual" in result.categories


@pytest.mark.parametrize("sentence", ["他強姦了她。他們發生性行為。", "反派強姦了俘虜。\n接著是肛交。"])
def test_the_two_halves_in_different_sentences_are_not_joined_into_a_rejection(sentence):
    assert check(background=sentence).verdict != REVIEW_REJECT


@pytest.mark.parametrize("sentence", ["他對她施以性虐並做愛。", "強暴之後是性交。"])
def test_an_explicit_sentence_with_sexual_violence_is_rejected(sentence):
    result = check(background=sentence)
    assert result.verdict == REVIEW_REJECT and "sexual_violence" in result.categories


@pytest.mark.parametrize("sentence", ["現實人物與她做愛。", "那位政治人物和他自慰。", "網紅與粉絲發生性行為。"])
def test_an_explicit_sentence_about_a_real_person_is_rejected(sentence):
    result = check(background=sentence)
    assert result.verdict == REVIEW_REJECT and "real_person_sexual" in result.categories


def test_a_rejection_lists_only_rejectable_categories_and_wins_over_flags():
    result = check(tier="safe", background="十歲的少女與他做愛。", summary="現實人物的故事。")
    assert result.verdict == REVIEW_REJECT
    assert set(result.categories) <= set(REJECT_CATEGORIES)


def test_every_field_of_the_script_is_scanned():
    for field, change in {
        "title": {"title": "做愛小學生"}, "summary": {"summary": "小學生做愛的故事"}, "world_name": {"world_name": "未成年做愛界"},
        "rules": {"rules": "未成年可以做愛。"}, "opening": {"opening": "幼女做愛。"}, "ending": {"ending_mode": "custom", "ending_text": "未成年做愛。", "ending_age": 80},
        "location": {"locations": ["幼童做愛的房間"]}, "tag": {"tags": ["兒童做愛"]},
        "character": {"characters": [character(personality="幼童做愛。")]},
        "hidden": {"characters": [character(hidden_trait="嬰兒性交")]},
        "milestone": {"milestones": [{"title": "小學生做愛", "trigger": {"kind": "age", "value": 20}}]},
    }.items():
        assert check(**change).verdict == REVIEW_REJECT, field


# ---- 閃避：拆字、全形、零寬字元 ------------------------------------------------------------


@pytest.mark.parametrize("sentence", [
    "未 成 年 的 孩 子 做 愛。", "未成年​的孩子做​愛。", "未成年的孩子做*愛。", "未成年的孩子做·愛。", "ＵＮ未成年的孩子做愛",
    "未成年的孩子做_愛。", "未成年的孩子做 － 愛。",
])
def test_splitting_the_words_with_spaces_zero_width_characters_or_symbols_still_matches(sentence):
    assert check(background=sentence).categories == ("minor_sexual",)


def test_fullwidth_digits_in_an_age_still_count():
    assert check(background="１２歲的她做愛。").categories == ("minor_sexual",)


# ---- 誤擋防線：一般的未成年角色必須通過 ----------------------------------------------------


def test_a_grown_up_protagonist_with_a_three_year_old_baby_passes():
    result = check(
        tier="standard", background="龍傲天帶著三歲的寶寶闖蕩江湖，一路上餵奶、換尿布、哄睡。",
        characters=[character("龍傲天", 28, "霸氣"), character("小寶", 3, "愛哭又黏人", appearance="圓圓的臉蛋")],
    )
    assert result == RulesResult(REVIEW_PASS, (), load_lexicon().version)


def test_everyday_school_life_of_sixteen_year_olds_passes():
    result = check(
        background="十六歲的高中生每天上學、參加社團、準備考試，放學後和同學一起吃宵夜。",
        characters=[character("小雨", 16, "認真", appearance="短髮"), character("阿傑", 17, "調皮", appearance="高個子")],
    )
    assert result.verdict == REVIEW_PASS


def test_parent_and_child_and_childcare_stories_pass():
    for text in (
        "母親照顧八歲的兒子，每晚講睡前故事。", "新手爸爸學習幫嬰兒洗澡、拍嗝、哄睡。", "小朋友們在公園玩耍，大人在一旁聊天。",
        "他看著孩童們的笑臉，想起自己的童年。", "未成年人需要監護人的陪同，這是宗門的規矩。",
    ):
        assert check(tier="standard", background=text).verdict == REVIEW_PASS, text


def test_a_minor_character_without_sexual_words_passes_in_every_tier():
    for tier in ("safe", "standard", "adult"):
        assert check(tier=tier, characters=[character("小明", 12, "好奇", appearance="瘦小")]).verdict == REVIEW_PASS


def test_the_minor_sentence_and_the_explicit_sentence_in_different_sentences_is_not_a_rejection():
    result = check(tier="adult", background="十歲的小明在院子裡玩耍。成年的兩人在房裡做愛。")
    assert result.verdict != REVIEW_REJECT


@pytest.mark.parametrize("text", [
    "他生性愛玩，常常闖禍。", "這孩子天性愛哭，需要人哄。", "我們喜歡好吃的東西，小朋友也是。", "他在異國中生存了三年，像個兒童一樣學習語言。",
    "他箭術精準，一箭射精準地命中，旁邊的小學生拍手叫好。", "聊以自慰的是，孩子平安長大了。", "清虛真人帶著幼童入山修行，並與他現實中的父親道別。",
    "角色情緒激動的小學生跑了過來。", "小朋友在路口交通指揮下過馬路。", "媽媽為小學生做愛心便當。", "孩童們在路口交談甚歡。",
    "兒童節那天，門口交易所裡擠滿了人。",
])
def test_ordinary_sentences_that_contain_a_lexicon_word_as_part_of_another_word_do_not_match(text):
    result = check(tier="standard", background=text)
    assert result.verdict != REVIEW_REJECT, (text, result)
    assert "minor_sexual" not in result.categories


def test_an_adult_age_that_looks_like_a_minor_age_in_chinese_numerals_is_not_a_minor():
    for text in ("二十歲的她做愛。", "十八歲的她做愛。", "三十歲的他做愛。", "九十九歲的長老做愛。"):
        assert check(tier="adult", background=text).verdict != REVIEW_REJECT, text


# ---- 有疑慮：標示類別、進人工 ----------------------------------------------------------------


def test_a_minor_character_with_sexual_words_in_their_own_text_is_flagged_in_standard_and_adult():
    for tier in ("standard", "adult"):
        result = check(tier=tier, characters=[character("小明", 17, "情慾旺盛", appearance="瘦小")])
        assert result == RulesResult(REVIEW_FLAG, ("minor_suspected",), load_lexicon().version)


def test_a_minor_character_is_not_flagged_by_an_adult_characters_text():
    result = check(tier="adult", characters=[character("小明", 17, "活潑"), character("大雄", 30, "情慾旺盛")])
    assert "minor_suspected" not in result.categories


def test_sexual_words_and_real_person_words_in_different_sentences_are_flagged():
    result = check(tier="adult", background="現實人物的故事。另一段是他們做愛。")
    assert result.verdict == REVIEW_FLAG and "real_person_suspected" in result.categories


def test_a_safe_script_with_sexual_words_is_flagged_as_a_tier_mismatch():
    assert check(tier="safe", background="他們做愛。").categories == ("tier_mismatch",)
    assert check(tier="safe", background="兩人情慾難耐。").categories == ("tier_mismatch",)


def test_a_standard_script_with_explicit_words_is_flagged_but_suggestive_words_are_fine():
    assert check(tier="standard", background="他們做愛。").categories == ("tier_mismatch",)
    assert check(tier="standard", background="兩人情慾難耐，春宵苦短。").verdict == REVIEW_PASS


def test_an_adult_script_with_explicit_words_between_adults_passes():
    assert check(tier="adult", background="兩位成年人在房裡做愛，彼此情願。").verdict == REVIEW_PASS


def test_a_lone_sexual_violence_term_is_flagged_for_a_human_not_rejected():
    result = check(tier="adult", background="新聞報導了一起性侵案件，警方已介入。")
    assert result == RulesResult(REVIEW_FLAG, ("other_concern",), load_lexicon().version)


# ---- 類別是固定列舉、結果不回顯內容 -------------------------------------------------------------


def test_every_category_ever_returned_is_a_fixed_enumeration_value():
    samples = [
        dict(tier="safe", background="十歲的少女與他做愛。"), dict(tier="adult", background="他強姦了她。"),
        dict(tier="adult", background="現實人物與她做愛。"), dict(tier="adult", background="新聞報導了一起性侵案件。"),
        dict(tier="safe", background="他們做愛。"), dict(tier="adult", characters=[character("小明", 17, "情慾旺盛")]),
    ]
    seen = set()
    for sample in samples:
        result = check(**sample)
        assert set(result.categories) <= set(REVIEW_CATEGORIES)
        seen |= set(result.categories)
    assert seen & set(REJECT_CATEGORIES) and seen & set(FLAG_CATEGORIES)


def test_a_reject_uses_only_rejectable_categories_and_a_flag_only_flag_categories():
    assert set(check(tier="safe", background="十歲的少女與他做愛。").categories) <= set(REJECT_CATEGORIES)
    assert set(check(tier="safe", background="他們做愛。").categories) <= set(FLAG_CATEGORIES)


def test_the_result_never_contains_the_matched_words_or_the_input_text():
    text = "獨特的句子：十歲的少女與他做愛。"
    result = check(background=text)
    dumped = json.dumps([result.verdict, list(result.categories), result.lexicon_version], ensure_ascii=False)
    assert "做愛" not in dumped and "獨特" not in dumped and "少女" not in dumped


def test_checking_does_not_change_the_content_it_was_given():
    content = validated().content
    before = json.dumps(content, sort_keys=True, ensure_ascii=False)
    check_rules(content, tier="safe")
    assert json.dumps(content, sort_keys=True, ensure_ascii=False) == before
