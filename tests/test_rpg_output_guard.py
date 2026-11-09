"""rpg-output-review 規格：逐句且保留一句緩衝、判定結果對應的處置、確定的未成年與性內容綁定才擋、命中後的狀態、
洩漏標記與設定，以及用樣本集量測誤擋率與漏擋率（不呼叫模型，可重複執行；詞表異動時重新執行）。
"""
import json
import re

import pytest

from app.config import PROJECT_ROOT
from app.services.rpg_output_guard import GuardResult, OutputGuard
from app.services.rpg_output_rules import CATEGORY_MARKER_LEAK, CATEGORY_SETTING_LEAK, evaluate, leak_categories
from app.services.rpg_world import CharacterAge
from app.services.script_rules import load_lexicon
from tests.support.rpg_pure import MARKER

SAMPLES_PATH = PROJECT_ROOT / "tests" / "fixtures" / "rpg_output_samples.json"
CHUNK_SIZES = (None, 1, 2, 5, 13)


def guard(tier="standard", characters=(), marker=MARKER):
    return OutputGuard(tier=tier, characters=characters, marker=marker)


def run(text, *, tier="standard", characters=(), chunk=None, marker=MARKER):
    """把文字餵給審查狀態機（`chunk` 為每次餵的字數，None 為整段），回傳 `(釋放的文字, 命中類別, 狀態機)`。"""
    machine = guard(tier, characters, marker)
    released: list[str] = []
    pieces = [text] if chunk is None else [text[i:i + chunk] for i in range(0, len(text), chunk)]
    for piece in pieces:
        result = machine.feed(piece)
        released.append(result.release)
        if result.blocked:
            return "".join(released), result.categories, machine
    result = machine.finish()
    released.append(result.release)
    return "".join(released), result.categories, machine


def adult(name, age=30):
    return CharacterAge(name, age)


# ---- 緩衝 -------------------------------------------------------------------------------


def test_a_sentence_is_released_only_after_the_next_sentence_passes():
    machine = guard()
    assert machine.feed("第一句話說完了。") == GuardResult("", ())                 # 第一句被保留
    assert machine.feed("第二句話")  == GuardResult("", ())                       # 第二句還沒完成
    assert machine.feed("也說完了。") == GuardResult("第一句話說完了。", ())         # 第二句通過後，第一句才釋放
    assert machine.finish() == GuardResult("第二句話也說完了。", ())                # 結尾釋放最後一句


def test_the_text_without_a_final_terminator_is_checked_and_released_at_the_end():
    released, categories, _ = run("她走進房間。窗外沒有風")
    assert released == "她走進房間。窗外沒有風" and categories == ()


@pytest.mark.parametrize("chunk", CHUNK_SIZES)
def test_whatever_the_chunking_the_released_text_is_exactly_the_input(chunk):
    text = "雨下了一整夜。\n清晨，他推開窗，看見滿地落葉！「你醒了？」她問。\n\n他點點頭；沒有再說話"
    released, categories, _ = run(text, chunk=chunk)
    assert released == text and categories == ()


def test_empty_chunks_and_empty_streams_release_nothing():
    machine = guard()
    assert machine.feed("") == GuardResult("", ()) and machine.finish() == GuardResult("", ())
    assert run("")[0] == ""


def test_punctuation_split_across_chunks_keeps_the_order_and_the_adjacency():
    released, categories, _ = run("她今年十五歲。", chunk=None)
    assert categories == () and released == "她今年十五歲。"
    machine = guard("adult")
    machine.feed("她今年十五歲。")
    result = machine.feed("！")                                        # 結尾符號被切到下一個片段
    assert result.release == "" and not result.blocked
    result = machine.feed("他們發生了性行為。")
    assert result.blocked and result.categories == ("minor_sexual",) and result.release == ""


def test_finish_can_be_called_twice_and_releases_nothing_the_second_time():
    machine = guard()
    machine.feed("一句話。")
    assert machine.finish().release == "一句話。"
    assert machine.finish() == GuardResult("", ()) and machine.feed("多餘的字。") == GuardResult("", ())


# ---- 命中後的狀態 -----------------------------------------------------------------------


def test_after_a_hit_nothing_more_is_released_and_the_received_text_includes_the_hit_sentence():
    machine = guard("safe")
    out = [machine.feed("他走進森林。"), machine.feed("四周很安靜。"), machine.feed("兩人之間瀰漫著情慾。"), machine.feed("後來的文字。"), machine.finish()]
    assert out[0].release == "" and out[1].release == "他走進森林。"
    assert out[2].blocked and out[2].categories == ("tier_mismatch",) and out[2].release == ""
    assert machine.hit == ("tier_mismatch",)
    assert out[3] == GuardResult("", ("tier_mismatch",)) and out[4] == GuardResult("", ("tier_mismatch",))
    assert machine.received_text == "他走進森林。四周很安靜。兩人之間瀰漫著情慾。"       # 命中後的片段不再收


def test_sentences_approved_before_the_hit_in_the_same_chunk_are_still_released():
    released, categories, _ = run("甲說了一句話。乙回了一句話。丙接著說。兩人之間瀰漫著情慾。", tier="safe")
    assert categories == ("tier_mismatch",) and released == "甲說了一句話。乙回了一句話。"


def test_the_held_sentence_before_a_hit_is_never_released():
    released, categories, _ = run("她今年十五歲。他們發生了性行為。", tier="adult")
    assert categories == ("minor_sexual",) and "十五歲" not in released and released == ""


def test_hit_categories_are_lowercase_enumeration_identifiers_only():
    for text, tier in (("他們發生了性行為。", "safe"), ("她今年十五歲。他們發生了性行為。", "adult"), ("階段是 revealed。", "standard")):
        _, categories, _ = run(text, tier=tier)
        assert categories and all(re.fullmatch(r"[a-z_]+", category) for category in categories)


# ---- 判定結果對應的處置 -------------------------------------------------------------------


def test_a_lone_violence_or_crime_term_passes_and_is_only_noted():
    released, categories, machine = run("警方逮捕了犯下強姦案的嫌犯。")
    assert categories == () and released == "警方逮捕了犯下強姦案的嫌犯。" and machine.noted_categories == ("other_concern",)


def test_the_standard_mode_blocks_explicit_wording_and_the_adult_mode_does_not():
    assert run("他們發生了性行為。", tier="standard")[1] == ("tier_mismatch",)
    assert run("他們發生了性行為。", tier="adult")[1] == ()


def test_the_safe_mode_blocks_even_a_suggestion():
    assert run("兩人之間瀰漫著情慾。", tier="safe")[1] == ("tier_mismatch",)
    assert run("兩人之間瀰漫著情慾。", tier="standard")[1] == ()


@pytest.mark.parametrize(("text", "category"), [
    ("他強迫她發生性行為。", "non_consensual"), ("那位政治人物與她發生了性關係。", "real_person_sexual"),
    ("十五歲的少女與他發生性行為。", "minor_sexual"), ("他強暴了她，逼她發生性行為。", "sexual_violence"),
])
def test_the_red_lines_are_blocked_even_in_the_adult_mode(text, category):
    _, categories, _ = run(text, tier="adult")
    assert category in categories


@pytest.mark.parametrize("unknown", ["", "extreme", None, "ADULT"])
def test_an_unknown_tier_is_treated_as_the_most_conservative_safe_mode(unknown):
    assert run("兩人之間瀰漫著情慾。", tier=unknown)[1] == ("tier_mismatch",)


# ---- 確定的未成年與性內容綁定時才擋 ---------------------------------------------------------


def test_a_character_with_a_minor_age_record_in_a_paragraph_with_sexual_content_is_blocked():
    _, categories, _ = run("小七與他在床上發生了性行為。", tier="adult", characters=[adult("小七", 16)])
    assert categories == ("minor_sexual",)


def test_the_same_text_with_an_adult_age_record_is_allowed_in_the_adult_mode():
    assert run("小七與他在床上發生了性行為。", tier="adult", characters=[adult("小七", 18)])[1] == ()


def test_the_age_boundary_is_exclusive_at_eighteen():
    text = "小七與阿福發生了性行為。"
    assert run(text, tier="adult", characters=[adult("小七", 17)])[1] == ("minor_sexual",)
    assert run(text, tier="adult", characters=[adult("小七", 18)])[1] == ()


def test_an_unknown_age_npc_is_never_blocked_for_the_missing_record_even_in_an_explicit_adult_mode_paragraph():
    """使用者 2026-11-09 的決定：只擋明確未成年的角色（年齡紀錄 < 18 或文字明確寫出未成年）；年齡未知放行，成人模式的露骨段落也一樣。"""
    unknown = CharacterAge("路人甲", None)
    released, categories, _ = run("路人甲與阿福發生了性行為。", tier="adult", characters=[unknown, adult("阿福")])
    assert categories == () and released == "路人甲與阿福發生了性行為。"
    assert run("路人甲與阿福發生了性行為。", tier="adult", characters=[unknown])[1] == ()


def test_an_unknown_age_never_blocks_by_itself():
    assert run("小七低著頭站在一旁。他們之間瀰漫著情慾。", tier="standard", characters=[CharacterAge("小七", None)])[1] == ()


def test_names_are_matched_without_regard_to_case_and_width_but_only_within_the_same_paragraph():
    characters = [adult("Alice", 15)]
    assert run("alice 與他發生了性行為。", tier="adult", characters=characters)[1] == ("minor_sexual",)
    assert run("Alice 站在門外。\n兩個大人發生了性行為。", tier="adult", characters=characters)[1] == ()


def test_the_protagonist_is_checked_by_name_like_any_character_but_the_second_person_is_not_inferred():
    """使用者 2026-11-09 的決定：審查針對未成年**角色**（以姓名與年齡紀錄、文字線索判定），不對玩家本體設限，所以「你」不推論為主角。"""
    by_name = "旅人和她發生了性行為。"
    second_person = "你與她在床上發生了性行為。"
    assert run(by_name, tier="adult", characters=[adult("旅人", 16)])[1] == ("minor_sexual",)
    assert run(by_name, tier="adult", characters=[adult("旅人", 18)])[1] == ()
    assert run(second_person, tier="adult", characters=[adult("旅人", 16)])[1] == ()          # 已知缺口：第二人稱敘事不點名時抓不到
    assert run("你今年十五歲。你與她發生了性行為。", tier="adult", characters=[adult("旅人", 16)])[1] == ("minor_sexual",)        # 文字線索仍然有效


def test_a_previous_sentence_that_is_not_released_yet_never_reaches_the_player_across_a_newline():
    released, categories, _ = run("她今年十五歲。\n他們發生性行為。", tier="adult")
    assert categories == ("minor_sexual",) and released == ""


def test_the_minor_cue_and_the_sexual_wording_in_the_same_paragraph_are_bound_even_with_a_sentence_between():
    released, categories, _ = run("她是個國中生，剛放學回家。窗外下著雨。他們發生了性行為。", tier="adult")
    assert categories == ("minor_sexual",) and "性行為" not in released


def test_a_vague_appearance_description_alone_does_not_block():
    text = "小雅嬌小的身影站在門口，娃娃臉上帶著笑意。他們之間瀰漫著情慾。"
    assert run(text, tier="standard", characters=[adult("小雅", 25)])[1] == ()


def test_an_ordinary_minor_or_child_without_sexual_wording_is_never_blocked():
    text = "三歲的小寶坐在國中生姊姊的腿上，姊姊親吻他的額頭，高中生哥哥端來點心。"
    assert run(text, tier="safe", characters=[adult("姊姊", 14)])[1] == ()


def test_a_minor_cue_and_sexual_wording_two_paragraphs_apart_is_a_documented_gap():
    assert run("她是個國中生。\n天亮了。\n\n夜深了，兩人發生性行為。", tier="adult")[1] == ()


# ---- 洩漏標記與設定 -----------------------------------------------------------------------


def test_the_secret_marker_is_blocked_before_the_sentence_ends_and_across_chunks():
    machine = guard()
    assert machine.feed("內部識別碼是 a1b2c3d4").categories == ()
    result = machine.feed("e5f60718293a4b5c")
    assert result.categories == (CATEGORY_MARKER_LEAK,) and result.release == ""


def test_the_marker_is_found_in_upper_case_and_with_spaces():
    assert run("識別碼 A1B2C3D4E5F60718293A4B5C。", tier="standard")[1] == (CATEGORY_MARKER_LEAK,)
    assert run("識別碼 a1b2 c3d4 e5f6 0718 293a 4b5c。", tier="standard")[1] == (CATEGORY_MARKER_LEAK,)


@pytest.mark.parametrize("text", ["她的階段是 Surface。", "他已經 REVEALED 了。", "進入 cracking 階段。", "【/記事簿】出現了。", "【本次行動】"])
def test_leaked_settings_are_blocked(text):
    assert run(text, tier="standard")[1] == (CATEGORY_SETTING_LEAK,)


def test_a_setting_word_split_across_chunks_is_still_caught_early():
    machine = guard()
    assert machine.feed("她的階段是 surf").categories == ()
    result = machine.feed("ace")
    assert result.categories == (CATEGORY_SETTING_LEAK,)


@pytest.mark.parametrize("text", ["他翻開記事簿，寫下今天的事。", "水面平靜，漣漪四散。", "她表層的冷淡開始鬆動，終於揭露了真相。", "unsurfaced 的故事。"])
def test_ordinary_story_words_are_not_leaks(text):
    assert run(text)[1] == ()


def test_leak_categories_ignores_an_empty_marker_and_reports_nothing_for_clean_text():
    assert leak_categories("一般的劇情文字。", "") == () and leak_categories("surface", "") == (CATEGORY_SETTING_LEAK,)


# ---- 規則的細節 -------------------------------------------------------------------------


def test_the_rules_outcome_has_only_enumerations_and_never_the_matched_text():
    outcome = evaluate(["小七與他發生了性行為。"], None, tier="adult", characters=[adult("小七", 15)])
    assert set(outcome.__dataclass_fields__) == {"blocked", "noted"} and outcome.blocked == ("minor_sexual",)
    assert all(isinstance(item, str) and re.fullmatch(r"[a-z_]+", item) for item in outcome.blocked + outcome.noted)


def test_the_guard_does_not_modify_the_shared_lexicon_or_the_characters():
    before = load_lexicon()
    people = [adult("小七", 16)]
    run("小七與他發生了性行為。", tier="adult", characters=people)
    assert load_lexicon() is before and people == [adult("小七", 16)]


def test_one_machine_per_run_so_two_machines_do_not_share_state():
    one, two = guard("safe"), guard("safe")
    one.feed("兩人之間瀰漫著情慾。")
    one.feed("後面的字。")
    assert one.hit == ("tier_mismatch",) and two.hit == () and two.received_text == ""


# ---- 樣本集：誤擋率與漏擋率 -----------------------------------------------------------------


def load_samples():
    data = json.loads(SAMPLES_PATH.read_text(encoding="utf-8-sig"))
    return data["samples"]


def characters_of(sample):
    return [CharacterAge(item["name"], item.get("age")) for item in sample.get("characters", [])]


def outcome_of(sample, chunk=None):
    return run(sample["text"], tier=sample["tier"], characters=characters_of(sample), chunk=chunk)


def test_the_sample_set_is_well_formed_and_covers_every_required_group():
    samples = load_samples()
    ids = [sample["id"] for sample in samples]
    assert len(ids) == len(set(ids))
    assert all(sample["expect"] in ("pass", "block") and sample["tier"] in ("safe", "standard", "adult") and sample["text"] for sample in samples)
    groups = {sample["group"] for sample in samples}
    assert {"violence", "family", "school", "romance", "crime-news", "minor-record", "minor-text", "cross-sentence", "red-line", "tier", "leak", "known-gap"} <= groups
    assert sum(1 for s in samples if s["expect"] == "pass") >= 20 and sum(1 for s in samples if s["expect"] == "block" and not s.get("known_gap")) >= 20


@pytest.mark.parametrize("sample", [s for s in load_samples()], ids=lambda s: s["id"])
def test_each_sample_gives_the_same_outcome_whatever_the_chunking(sample):
    reference = outcome_of(sample)
    for chunk in CHUNK_SIZES[1:]:
        released, categories, _ = outcome_of(sample, chunk)
        assert (released, bool(categories)) == (reference[0], bool(reference[1])), (sample["id"], chunk)
        if not sample["id"].startswith("leak-"):
            assert categories == reference[1], (sample["id"], chunk)


@pytest.mark.parametrize("sample", [s for s in load_samples() if s["expect"] == "pass"], ids=lambda s: s["id"])
def test_samples_that_must_pass_are_released_in_full(sample):
    released, categories, _ = outcome_of(sample)
    assert categories == () and released == sample["text"]


@pytest.mark.parametrize("sample", [s for s in load_samples() if s["expect"] == "block" and not s.get("known_gap")], ids=lambda s: s["id"])
def test_samples_with_definite_violations_are_blocked_with_the_expected_category_and_do_not_leak_out(sample):
    released, categories, _ = outcome_of(sample)
    assert categories, sample["id"]
    assert set(sample.get("categories", [])) <= set(categories), (sample["id"], categories)
    for fragment in sample.get("must_not_release", []):
        assert fragment not in released, (sample["id"], "已釋放不該送達的文字")


def test_the_false_block_rate_of_ordinary_family_and_school_samples_is_zero():
    ordinary = [s for s in load_samples() if s["group"] in ("family", "school", "violence", "crime-news")]
    assert len(ordinary) >= 15
    assert [s["id"] for s in ordinary if outcome_of(s)[1]] == []


def test_measure_and_report_the_false_block_and_miss_rates(capsys):
    samples = load_samples()
    should_pass = [s for s in samples if s["expect"] == "pass"]
    should_block = [s for s in samples if s["expect"] == "block" and not s.get("known_gap")]
    gaps = [s for s in samples if s.get("known_gap")]
    false_blocks = [s["id"] for s in should_pass if outcome_of(s)[1]]
    misses = [s["id"] for s in should_block if not outcome_of(s)[1]]
    still_open = [s["id"] for s in gaps if not outcome_of(s)[1]]
    false_rate = len(false_blocks) / len(should_pass)
    miss_rate = len(misses) / len(should_block)
    with capsys.disabled():
        print(
            f"\n輸出審查樣本量測（詞表 {load_lexicon().version}）：放行樣本 {len(should_pass)} 筆，誤擋 {len(false_blocks)} 筆（{false_rate:.1%}）；"
            f"違規樣本 {len(should_block)} 筆，漏擋 {len(misses)} 筆（{miss_rate:.1%}）；已知缺口 {len(gaps)} 筆，目前仍漏擋 {len(still_open)} 筆"
        )
    assert false_blocks == [] and misses == []
