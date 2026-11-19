"""模型輸出的繁體中文保證：`zh_text.to_traditional` 與它套用的三個地方（劇情串流的審查狀態機、RPG 用途的結構化輸出、提示詞）。

背景：展示時 RPG 的免費模型（nemotron）不遵守提示詞的繁體要求，輸出簡體字。伺服器端再轉一次，不依賴模型守規矩；客服（Gemini）不經過這裡。
"""
import json

import pytest

from app.models.llm_call_log import (
    KIND_CHAT, KIND_PROTAGONIST_GEN, KIND_PROTAGONIST_REVIEW, KIND_RPG_NOTEBOOK, KIND_RPG_STATE, KIND_SCRIPT_REVIEW,
)
from app.services import rpg_prompt
from app.services.rpg_output_guard import OutputGuard
from app.services.zh_text import to_traditional
from tests.support.game import RunnerKit, story_script
from tests.support.llm import FakeLLMProvider, Step
from tests.support.rpg_pure import MARKER
from tests.test_llm_service import build, run

SIMPLIFIED_SENTENCES = ("清晨的海风吹过山门。", "守门的弟子向你点了点头。", "你踏进了雾气之中。")
SIMPLIFIED = "".join(SIMPLIFIED_SENTENCES)
TRADITIONAL = "清晨的海風吹過山門。守門的弟子向你點了點頭。你踏進了霧氣之中。"
CHUNKS = (None, 1, 2, 5, 13)
ENGLISH_STORY = "我感覺你需要 help。他說了聲 okay 就走了。"                   # 繁體句夾雜英文單字：伺服器端不翻譯、不改寫、不擋下


# ---- 轉換本身 ------------------------------------------------------------------------------------------------------

def test_simplified_text_becomes_traditional_with_taiwan_wording():
    assert to_traditional(SIMPLIFIED) == TRADITIONAL
    assert to_traditional("他拿起软件和鼠标，发现这个网络里的信息很奇怪。") == "他拿起軟體和滑鼠，發現這個網路裡的資訊很奇怪。"       # 慣用詞：軟件→軟體、鼠標→滑鼠


def test_text_that_is_already_traditional_is_kept_as_it_is():
    assert to_traditional(TRADITIONAL) == TRADITIONAL
    assert to_traditional("晨霧之中，山門緩緩開啟。遠處傳來悠長的鐘聲。") == "晨霧之中，山門緩緩開啟。遠處傳來悠長的鐘聲。"
    assert to_traditional(to_traditional(SIMPLIFIED)) == to_traditional(SIMPLIFIED)                       # 重複轉換不會再變


def test_names_and_ambiguous_characters_are_not_damaged():
    assert to_traditional("皇后與太后在後山遇見了于謙。") == "皇后與太后在後山遇見了于謙。"
    assert to_traditional("皇后与太后在后山遇见了于谦。") == "皇后與太后在後山遇見了于謙。"


@pytest.mark.parametrize("sentence", [
    "千里之外的山門。", "鄉里的里長來了。", "台灣的天台上有一座台。", "咸陽城外有一片鹹水湖。", "余下的人都散了，我余某不怕。", "曲折的小徑通向一個大曲坊。",
    "神祕的少女站在月下。",                                                             # 繁體異體字「祕」不能被改成「秘」
])
def test_correct_traditional_sentences_with_characters_that_both_scripts_use_are_left_alone(sentence):
    assert to_traditional(sentence) == sentence


def test_a_sentence_is_converted_when_it_has_a_clear_simplified_character_even_next_to_a_both_script_one():
    assert to_traditional("咸阳城外有一片咸水湖。") == "咸陽城外有一片鹹水湖。"                       # 「阳」是簡體字，整句交給轉換器依上下文處理
    assert to_traditional("他说：「走。」") == "他說：「走。」"                                      # 句子裡只有一個簡體字也要轉


@pytest.mark.parametrize("sentence", [
    "他叫李杰，是山門的弟子。", "杰哥看著她，沒有說話。", "俊杰與家杰一起走進山門。", "杰", "「杰，」她說，「過來。」",
])
def test_the_variant_character_jie_used_in_taiwanese_names_is_kept(sentence):
    """「杰」是「傑」的異體字，台灣人名常用；OpenCC 會把它當簡體字改掉（玩家取名「李杰」，AI 的回覆變成「李傑」）。"""
    assert to_traditional(sentence) == sentence


def test_jie_is_kept_even_when_the_sentence_is_converted_for_another_simplified_character():
    assert to_traditional("他叫李杰，我们一起走吧。") == "他叫李杰，我們一起走吧。"                        # 其餘的簡體字照轉，「杰」不動
    assert to_traditional("杰说：「杰哥来了。」杰笑了。") == "杰說：「杰哥來了。」杰笑了。"                 # 連續出現、在句首與句尾都一樣
    assert to_traditional(to_traditional("李杰踏进了雾气之中。")) == "李杰踏進了霧氣之中。"                  # 重複轉換不會再變


def test_jie_alone_does_not_make_a_traditional_sentence_count_as_simplified():
    """句子裡只有「杰」時不算簡體：整句不進轉換器，所以同一句裡正確的繁體異體字（「祕」）與「咸陽」也不會被改。"""
    assert to_traditional("神祕的少女與李杰站在月下。") == "神祕的少女與李杰站在月下。"
    assert to_traditional("李杰來自咸陽。") == "李杰來自咸陽。"


def test_the_standard_form_jie_is_untouched_too():
    assert to_traditional("他叫李傑，是山門的弟子。") == "他叫李傑，是山門的弟子。"
    assert to_traditional("李傑与杰一起走进山门。") == "李傑與杰一起走進山門。"


@pytest.mark.parametrize("text", ["", "abc 123 [DONE]", "a1b2c3d4e5f60718293a4b5c", "！？。，", "\n\n"])
def test_text_without_chinese_characters_is_returned_unchanged(text):
    assert to_traditional(text) == text


def test_json_structure_and_ascii_survive_so_converted_model_json_still_parses():
    raw = json.dumps({"reply": "他踏进了雾气之中", "stage": "surface", "n": 3, "ok": True}, ensure_ascii=False)
    converted = to_traditional(raw)
    assert json.loads(converted) == {"reply": "他踏進了霧氣之中", "stage": "surface", "n": 3, "ok": True}


@pytest.mark.parametrize("sentence", ["我感覺你需要 help。", "他說了聲 okay 就走了。", "「Hello,」她說，「你好。」", ENGLISH_STORY])
def test_english_words_in_a_traditional_sentence_are_neither_translated_nor_changed(sentence):
    """英文夾雜只靠提示詞約束；伺服器端不翻譯、不改寫、不刪除（規格 rpg-output-traditional-chinese）。"""
    assert to_traditional(sentence) == sentence


def test_english_words_next_to_simplified_characters_survive_while_the_chinese_is_converted():
    assert to_traditional("我感觉你需要 help，他说了声 okay 就走了。") == "我感覺你需要 help，他說了聲 okay 就走了。"


# ---- 劇情串流的審查狀態機 --------------------------------------------------------------------------------------------

def release_all(text, chunk, tier="standard"):
    machine = OutputGuard(tier=tier, characters=(), marker=MARKER)
    out, hit = [], ()
    pieces = [text] if chunk is None else [text[i:i + chunk] for i in range(0, len(text), chunk)]
    for piece in pieces:
        result = machine.feed(piece)
        out.append(result.release)
        if result.blocked:
            return "".join(out), result.categories, machine
    result = machine.finish()
    out.append(result.release)
    return "".join(out), result.categories, machine


@pytest.mark.parametrize("chunk", CHUNKS)
def test_the_guard_releases_traditional_text_whatever_way_the_chunks_are_cut(chunk):
    released, hit, machine = release_all(SIMPLIFIED, chunk)
    assert released == TRADITIONAL and hit == ()
    assert machine.received_text == SIMPLIFIED                           # 迄今收到的原文不動（被擋回合暫存用）


def test_a_blocked_sentence_is_blocked_the_same_way_in_simplified_form():
    """規則層看到的是繁體：簡體寫法的未成年性內容不能因為字形不同而漏擋。"""
    traditional = release_all("十歲的少女與他做愛。", None)
    simplified = release_all("十岁的少女与他做爱。", None)
    assert traditional[1] and simplified[1] == traditional[1]
    assert simplified[0] == traditional[0] == ""                         # 被擋的句子沒有釋放


def test_a_simplified_sentence_that_follows_an_earlier_one_is_still_buffered_and_converted_in_order():
    released, hit, _ = release_all("第一句话说完了。第二句也说完了。", 3)
    assert released == "第一句話說完了。第二句也說完了。" and hit == ()


@pytest.mark.parametrize("chunk", CHUNKS)
def test_the_guard_keeps_the_name_jie_whatever_way_the_chunks_are_cut(chunk):
    """玩家替角色取名「李杰」：劇情串流釋放的文字保留「杰」，同一段裡的簡體字照轉。"""
    released, hit, _ = release_all("李杰笑了笑。他说：「我们走吧，杰。」", chunk)
    assert released == "李杰笑了笑。他說：「我們走吧，杰。」" and hit == ()


@pytest.mark.parametrize("chunk", CHUNKS)
def test_the_guard_releases_english_words_as_they_are_whatever_way_the_chunks_are_cut(chunk):
    released, hit, _ = release_all(ENGLISH_STORY, chunk)
    assert released == ENGLISH_STORY and hit == ()                       # 沒有被翻譯、改寫或擋下


def test_a_simplified_story_is_streamed_and_stored_as_traditional(member_session):
    """規格：釋放給玩家與寫入資料庫的都是繁體。回合、第 0 幕、重新生成與編輯共用同一個 `RunnerBase._attempt`，這裡驗最常用的回合，
    並確認玩家重新整理後重播看到的也是繁體。"""
    kit = RunnerKit(member_session)
    user = kit.player()
    save = kit.open_save(user)
    plan = kit.start(user, save)
    state = json.dumps({"elapsed_months": 1, "status_summary": "你抵達了山莊。"}, ensure_ascii=False)
    kit.provider.script_stream(*story_script(*SIMPLIFIED_SENTENCES)).script(Step(state))
    events = kit.run(plan, user)
    assert events[-1].name == "done" and events[-1].data["status"] == "complete"
    assert "".join(event.data["text"] for event in events if event.name == "token") == TRADITIONAL        # 送達玩家的
    (turn,) = [turn for turn in kit.turns(save) if turn.client_msg_id == "m-1"]
    assert turn.llm_raw_response == TRADITIONAL                                                          # 寫入資料庫的
    replay = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action="我走進山門。").replay
    assert replay.story == TRADITIONAL


# ---- RPG 用途的結構化輸出 ---------------------------------------------------------------------------------------------

SIMPLIFIED_JSON = json.dumps({"reply": "他踏进了雾气之中。"}, ensure_ascii=False)


@pytest.mark.parametrize("kind", [KIND_RPG_STATE, KIND_RPG_NOTEBOOK, KIND_PROTAGONIST_GEN])
def test_structured_output_that_players_read_is_converted_before_it_is_parsed(member_session, kind):
    provider = FakeLLMProvider()
    provider.script(SIMPLIFIED_JSON)
    outcome = run(build(member_session, provider), kind=kind)
    assert outcome.ok and outcome.value == {"reply": "他踏進了霧氣之中。"}


@pytest.mark.parametrize("kind", [KIND_RPG_STATE, KIND_RPG_NOTEBOOK, KIND_PROTAGONIST_GEN])
def test_structured_output_keeps_the_name_jie_while_converting_the_rest(member_session, kind):
    provider = FakeLLMProvider()
    provider.script(json.dumps({"reply": "李杰踏进了雾气之中，杰说：「我们走。」"}, ensure_ascii=False))
    outcome = run(build(member_session, provider), kind=kind)
    assert outcome.ok and outcome.value == {"reply": "李杰踏進了霧氣之中，杰說：「我們走。」"}


@pytest.mark.parametrize("kind", [KIND_RPG_STATE, KIND_RPG_NOTEBOOK, KIND_PROTAGONIST_GEN])
def test_structured_output_keeps_english_words_as_they_are(member_session, kind):
    provider = FakeLLMProvider()
    provider.script(json.dumps({"reply": ENGLISH_STORY}, ensure_ascii=False))
    outcome = run(build(member_session, provider), kind=kind)
    assert outcome.ok and outcome.value == {"reply": ENGLISH_STORY}


@pytest.mark.parametrize("kind", [KIND_CHAT, KIND_SCRIPT_REVIEW, KIND_PROTAGONIST_REVIEW])
def test_customer_service_and_review_outputs_are_not_touched(member_session, kind):
    provider = FakeLLMProvider()
    provider.script(SIMPLIFIED_JSON)
    outcome = run(build(member_session, provider), kind=kind)
    assert outcome.ok and outcome.value == {"reply": "他踏进了雾气之中。"}


# ---- 提示詞 ------------------------------------------------------------------------------------------------------------

def test_the_prompt_forbids_simplified_characters_explicitly():
    text = rpg_prompt.LANGUAGE_INSTRUCTIONS["zh-Hant-TW"]
    assert "繁體" in text and "絕對不要輸出簡體字" in text and "台灣" in text


def test_the_prompt_tells_the_model_to_write_chinese_for_english_words_and_keeps_names_as_they_are():
    """展示驗收發現：劇情偶爾夾雜英文單字（「我感覺你需要help」）。提示詞要明講改用中文說法，並給具體的例子；人名與專有名詞仍可保留原文。"""
    text = rpg_prompt.LANGUAGE_INSTRUCTIONS["zh-Hant-TW"]
    assert "不要夾雜英文" in text and "改用自然的繁體中文說法" in text
    assert "幫忙" in text and "help" in text                                       # 具體例子：寫「幫忙」不寫「help」
    assert "人名與專有名詞" in text
    # 回合敘事、第 0 幕、記事簿與狀態呼叫共用同一段語言規則
    assert text in rpg_prompt.build_system("safe", rpg_prompt.DEFAULT_LANGUAGE, "native", False, "m" * 24)
    assert text in rpg_prompt.build_opening_system("adult", rpg_prompt.DEFAULT_LANGUAGE, "native", "m" * 24)
