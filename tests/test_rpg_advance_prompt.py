"""rpg-advance-action 規格「系統指令的推進規則」：回合敘事的系統指令含一條固定的平台規則；第 0 幕、狀態呼叫與記事簿抓取都不含；
安全模式的界線與四類絕對禁止項不因此放寬；行動文字本身只出現在使用者內容區。純函式，不需要資料庫。

（建議呼叫的系統指令在批 3 的 `rpg_suggest_prompt.py`，那一批的測試再斷言它也不含這條規則。）
"""
import inspect
import itertools

import pytest

from app.services import rpg_notebook_prompt as notebook_prompt
from app.services import rpg_prompt as rp

MARKER = "0123456789abcdef01234567"
TIERS = ("safe", "adult")
ORIGINS = ("native", "transmigrator")
LANGUAGE = "zh-Hant-TW"


def turn_system(tier="safe", origin="native", ending_due=False):
    return rp.build_system(tier, LANGUAGE, origin, ending_due, MARKER)


def test_the_advance_action_is_the_fixed_four_character_platform_constant():
    assert rp.ADVANCE_ACTION == "推進劇情" and len(rp.ADVANCE_ACTION) == 4


@pytest.mark.parametrize(("tier", "origin", "ending"), list(itertools.product(TIERS, ORIGINS, (False, True))))
def test_every_turn_narration_instruction_carries_the_advance_rule(tier, origin, ending):
    text = turn_system(tier, origin, ending)
    assert rp._ADVANCE_RULE in text
    assert f"本次行動恰好是「{rp.ADVANCE_ACTION}」" in text
    assert "最近 10 輪" in text and "記事簿" in text and "當前世界狀態" in text and "自然地往下推進" in text
    assert "不要替主角做出違背其角色卡的重大決定" in text and "不會放寬任何內容界線" in text


def test_the_advance_rule_sits_in_the_writing_principles_before_the_content_boundary():
    writing, _boundary = turn_system().split("## 內容界線")
    assert rp._ADVANCE_RULE in writing and rp._ACTION_RULE in writing
    assert "優先於其他一切" in turn_system().split("## 內容界線")[1].split("\n")[0]


def test_the_advance_rule_is_not_in_the_opening_state_or_notebook_instructions():
    opening = [rp.build_opening_system(tier, LANGUAGE, origin, MARKER) for tier, origin in itertools.product(TIERS, ORIGINS)]
    state = [rp.build_state_system(LANGUAGE, MARKER)]
    capture = [notebook_prompt.build_capture_system(tier, LANGUAGE, MARKER) for tier in TIERS]
    for text in opening + state + capture:
        assert rp._ADVANCE_RULE not in text
        assert rp.ADVANCE_ACTION not in text and "自然地往下推進" not in text


@pytest.mark.parametrize("origin", ORIGINS)
def test_the_advance_rule_keeps_the_safe_boundary_and_the_absolute_bans(origin):
    safe, adult = turn_system("safe", origin), turn_system("adult", origin)
    assert rp.TIER_BOUNDARIES["safe"] in safe and rp.TIER_BOUNDARIES["adult"] not in safe
    assert rp.TIER_BOUNDARIES["adult"] in adult and rp.TIER_BOUNDARIES["safe"] not in adult
    for text in (safe, adult):
        assert "無論哪一種模式，一律禁止" in text and "涉及未成年" in text and "非自願的性情節" in text and "以真人名義的性內容" in text


def test_the_advance_rule_is_a_constant_and_never_built_from_player_text():
    """規則文字來自平台常數；系統指令的建構函式只收列舉參數，推進規則不依任何參數而變，所以玩家的文字不可能進入它。"""
    assert list(inspect.signature(rp.build_system).parameters) == ["tier", "language", "origin", "ending_due", "marker"]
    assert rp._ADVANCE_RULE == (
        f"- 若本次行動恰好是「{rp.ADVANCE_ACTION}」，表示玩家沒有特別的指示：請依最近 10 輪的劇情、記事簿與當前世界狀態，讓故事自然地往下推進，"
        "可以讓世界或其他人物主動行動；不要替主角做出違背其角色卡的重大決定。這不會放寬任何內容界線。\n"
    )
    first, second = turn_system("safe", "native", False), turn_system("safe", "native", False)
    assert first == second and first.count(rp._ADVANCE_RULE) == 1


def test_the_instruction_still_has_no_unfilled_placeholders_and_one_marker():
    for tier, origin, ending in itertools.product(TIERS, ORIGINS, (False, True)):
        text = turn_system(tier, origin, ending)
        assert "<<" not in text and text.count(MARKER) == 1
