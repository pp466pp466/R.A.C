"""rpg-story-view 7.1–7.3：遊戲主畫面的樣板與樣式（劇情卡、行動區、歷史 Modal、流星氛圍層）的靜態斷言。

行為（翻頁、打字機、建議、推進）在 tests/js/*.test.js；這裡只檢查樣板結構、可及性屬性與樣式規則（動畫只動 transform 與 opacity、減少動態、手機減量、不攔截點擊）。
"""
from __future__ import annotations

import re

from app.config import PROJECT_ROOT

TEMPLATE = PROJECT_ROOT / "app" / "templates" / "game" / "session.html"
CSS = PROJECT_ROOT / "app" / "static" / "css" / "game.css"


def read(path):
    return path.read_text(encoding="utf-8-sig")


def body() -> str:
    template = read(TEMPLATE)
    return template[template.index("{% raw %}"):template.index("{% endraw %}")]


def css() -> str:
    return read(CSS)


def test_the_story_card_shows_one_page_as_plain_text_with_a_speaker_label_and_page_buttons():
    html = body()
    assert 'class="gm-card"' in html and "{{ card.speakerLabel }}" in html and "{{ card.text }}" in html
    assert '@click="nextPage"' in html and '@click="prevPage"' in html
    assert re.search(r'<button[^>]*:disabled="!card\.canPrev"[^>]*aria-label="回到上一段"[^>]*@click="prevPage">‹</button>', html)   # 第一段停用 ‹
    assert re.search(r'<button[^>]*:disabled="!card\.canNext"[^>]*aria-label="翻到下一段"[^>]*@click="nextPage">', html)
    assert not re.search(r"v-html|innerHTML", html)


def test_the_story_text_is_read_by_assistive_technology_once_and_not_while_typing():
    html = body()
    assert re.search(r'<div class="gm-card__text" aria-hidden="true">\{\{ card\.text \}\}</div>', html)       # 打字中的字不重複朗讀
    assert '<div v-if="!card.typing" class="gm-sr">{{ card.fullText }}</div>' in html                          # 打字結束才提供完整文字


def test_the_suggestion_icon_is_an_inline_svg_with_accessible_labels_and_the_chips_are_plain_text_buttons():
    html = body()
    start = html.index('class="gm-iconbtn gm-suggest"')
    icon = html[start:html.index("</button>", start)]
    assert "<svg" in icon and 'aria-hidden="true" focusable="false"' in icon
    for needle in ('aria-label="', ":aria-expanded=", 'aria-controls="gm-suggest-list"', '@click="toggleSuggestions"', ':disabled="suggestDisabled"'):
        assert needle in icon, needle
    assert 'v-for="item in suggestItems"' in html and '@click="pickSuggestion(item)"' in html
    assert re.search(r'<button[^>]*class="gm-chip"[^>]*>\{\{ item \}\}</button>', html)
    assert "<img" not in html and "<canvas" not in html


def test_advance_and_history_sit_in_the_quick_bar_and_the_history_modal_is_read_only_plain_text():
    html = body()
    start = html.index('<div class="gm-quick"')
    quick = html[start:html.index("</div>", start)]
    assert '@click="openHistory"' in quick and ':disabled="advanceDisabled"' in quick and '@click="advance"' in quick
    start = html.index('<ui-modal v-model="modal.history"')
    block = html[start:html.index("</ui-modal>", start)]
    assert 'status-code="HISTORY"' in block and 'role="log"' in block and 'ref="scroller"' in block
    assert "{{ item.story }}" in block and "{{ item.action }}" in block and "{{ item.text }}" in block
    for forbidden in ("openRegenerate", "openEdit", "@click=\"send\"", "v-html", "<textarea"):
        assert forbidden not in block, forbidden                                  # 歷史沒有編輯或重新生成按鈕
    assert '@click="showEarlier"' in block and '@click="closeHistory"' in block


def test_the_meteor_sky_is_hidden_from_assistive_technology_has_five_empty_meteors_and_no_images():
    html = body()
    start = html.index('<div class="gm-sky"')
    sky = html[start:html.index("</div>", start)]
    assert sky.split(">")[0].count('aria-hidden="true"') == 1
    assert sky.count('<span class="gm-meteor"></span>') == 5 and sky.count("<span") == 5
    assert html.index('class="gm-sky"') < html.index('<header class="gm-topbar">')            # 根容器最底層
    sheet = css()
    assert "url(" not in sheet and "canvas" not in sheet.lower()


def test_the_meteors_move_only_with_transform_and_opacity_and_never_catch_clicks():
    sheet = css()
    keyframes = re.search(r"@keyframes gm-meteor \{(.*?)\n\}", sheet, re.S).group(1)
    assert set(re.findall(r"([\w-]+)\s*:", keyframes)) == {"opacity", "transform"}            # 合成層動畫：不觸發排版與重繪
    sky = re.search(r"\.gm-sky \{([^}]*)\}", sheet).group(1)
    meteor = re.search(r"\.gm-meteor \{([^}]*)\}", sheet).group(1)
    assert "pointer-events: none" in sky and "pointer-events: none" in meteor
    assert "opacity: 0" in meteor and "animation: gm-meteor" in meteor
    assert "z-index: 0" in sky and re.search(r"\.gm-body[^{]*\{[^}]*z-index: 1", sheet)       # 在劇情卡之下
    for rule in re.findall(r"\.gm-(?:sky|meteor)[^{]*\{([^}]*)\}", sheet):
        assert not re.search(r"#f{3}(?:f{3})?\b", rule, re.I) and "white" not in rule, rule   # 沒有純白
        assert "url(" not in rule


def test_the_meteors_stop_for_reduced_motion_and_only_three_remain_below_992px():
    sheet = css()
    reduced = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", sheet, re.S).group(1)
    assert re.search(r"\.gm-meteor \{[^}]*display: none[^}]*animation: none", reduced)
    assert re.search(r"@media not all and \(min-width: 992px\) \{\s*\.gm-meteor:nth-child\(n \+ 4\) \{ display: none; \}", sheet)
    periods = [float(value) for value in re.findall(r"\.gm-meteor:nth-child\(\d\) \{[^}]*animation-duration: ([\d.]+)s", sheet)]
    assert len(periods) == 5 and all(6 <= value <= 14 for value in periods)                  # 週期約 6 至 14 秒
    delays = re.findall(r"\.gm-meteor:nth-child\(\d\) \{[^}]*animation-delay: (-[\d.]+)s", sheet)
    assert len(delays) == 5 and len(set(delays)) == 5                                         # 負延遲錯開，開頁就不同步


def test_the_desktop_quick_bar_sits_left_of_the_card_and_mobile_keeps_it_below():
    sheet = css()
    desktop = re.search(r"@media \(min-width: 992px\) \{\s*\.gm-stagerow \{([^}]*)\}\s*\.gm-quick \{([^}]*)\}", sheet)
    assert desktop and "flex-direction: row" in desktop.group(1) and "order: -1" in desktop.group(2)
    assert "flex-direction: column" in re.search(r"\.gm-stagerow \{([^}]*)\}", sheet).group(1)   # 預設（手機）單欄：捷徑在劇情卡下方


# ---- 劇情朗讀（rpg-story-tts 2.4、2.5）-------------------------------------------------------------------------------------------


def speech_buttons() -> str:
    html = body()
    start = html.index('<span v-if="speech.availability !== \'unsupported\'" class="gm-speak">')
    return html[start:html.index("</span>", start)]


def test_the_speech_buttons_sit_first_in_the_card_nav_and_hide_when_speech_is_unsupported():
    html = body()
    nav = html[html.index('<div class="gm-card__nav">'):]
    nav = nav[:nav.index('<div v-if="reviseKey"')]
    assert nav.index("gm-speakbtn") < nav.index('aria-label="回到上一段"') < nav.index('aria-label="翻到下一段"')       # 最左邊，在 ‹ 之前
    assert speech_buttons().startswith('<span v-if="speech.availability !== \'unsupported\'" class="gm-speak">')


def test_the_speak_button_is_a_toggle_with_labels_a_reason_title_and_two_inline_svg_icons():
    buttons = speech_buttons()
    speak = buttons[buttons.index("<button"):buttons.index("</button>") + len("</button>")]
    assert 'class="gm-pagebtn gm-speakbtn"' in speak and ':disabled="speakDisabled"' in speak and '@click="speakCurrent"' in speak
    assert ":aria-pressed=\"speech.speaking ? 'true' : 'false'\"" in speak
    assert ":aria-label=\"speech.speaking ? '停止朗讀' : '朗讀這一段'\"" in speak and ':title="speakHint"' in speak
    assert speak.count("<svg") == 2 and 'v-if="!speech.speaking"' in speak and 'v-else' in speak                   # 播放與停止兩個內嵌 SVG
    assert speak.count('aria-hidden="true"') == 2 and "<img" not in speak and "url(" not in speak


def test_the_voice_menu_button_is_labelled_opens_a_dialog_and_is_disabled_only_without_a_voice():
    buttons = speech_buttons()
    menu = buttons[buttons.index('<button ref="voiceMenuButton"'):]
    menu = menu[:menu.index("</button>") + len("</button>")]
    assert 'aria-label="選擇語音"' in menu and 'aria-haspopup="dialog"' in menu and '@click="openVoiceMenu"' in menu
    assert ':disabled="speech.availability === \'no-voice\'"' in menu                                                  # 不受說明行與空內容影響：沒有內容時仍可挑語音
    assert "class=\"gm-pagebtn gm-speakmenu\"" in menu


def test_the_voice_menu_is_a_self_made_modal_with_a_radiogroup_automatic_first_labels_and_an_empty_state():
    html = body()
    start = html.index('<ui-modal v-model="modal.voice"')
    modal = html[start:html.index("</ui-modal>", start)]
    assert 'status-code="VOICE_SELECT"' in modal and ':show-cancel="false"' in modal and 'title="選擇朗讀的語音"' in modal
    assert 'role="radiogroup"' in modal and 'aria-label="朗讀語音"' in modal and 'ref="voiceList"' in modal
    assert modal.index("@change=\"chooseAutoVoice()\"") < modal.index('v-for="option in speech.voices"')               # 「自動」在最前面
    assert '@change="chooseAutoVoice()"' in modal and '@change="chooseVoiceByKey(option.key)"' in modal                # 選取即寫入並關閉
    assert modal.count('type="radio"') == 2 and ' name=' not in modal                                                   # 不共用 name：方向鍵不會一路選下去而提前關閉
    assert "{{ option.name }}" in modal and "{{ option.lang }}" in modal and "{{ option.local ? '本機' : '線上' }}" in modal
    assert '@click="previewVoice(option.key)"' in modal and ":aria-label=\"'試聽 ' + option.name\"" in modal
    assert 'v-if="speech.voices.length === 0"' in modal and "目前找不到中文語音" in modal and 'role="status"' in modal   # 空狀態說明
    assert '@click="closeVoiceMenu"' in modal
    assert "不會經過本平台" in modal and "只在這個瀏覽器記住" in modal                                                   # 線上語音與記住什麼的說明


def test_the_speech_markup_is_plain_text_without_html_injection_framework_attributes_or_new_resources():
    html = body()
    for fragment in (speech_buttons(), html[html.index('<ui-modal v-model="modal.voice"'):html.index("</ui-modal>", html.index('<ui-modal v-model="modal.voice"'))]):
        assert not re.search(r"v-html|innerHTML", fragment)
        assert "data-bs-" not in fragment
        assert not re.search(r"(?:src|href)=", fragment)                                                               # 沒有任何外部資源
        assert "speechSynthesis" not in fragment and "localStorage" not in fragment


def test_the_speech_styles_reuse_the_page_button_look_and_the_theme_colours_without_new_colours():
    sheet = css()
    for selector in (".gm-speak", ".gm-speakbtn", ".gm-speakbtn--on", ".gm-speakmenu", ".gm-speak__icon", ".gm-voice", ".gm-voice__row", ".gm-voice__option",
                     ".gm-voice__option--on", ".gm-voice__name", ".gm-voice__tag", ".gm-voice__preview"):
        assert re.search(re.escape(selector) + r"[ ,{:]", sheet), selector
    rules = "".join(re.findall(r"\.gm-(?:speak|voice)[\w_-]* \{[^}]*\}", sheet))
    assert rules and not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|white", rules)                                          # 只用主題變數，沒有新顏色
    assert "animation" not in rules and "transition" not in rules                                                       # 沒有動畫，減少動態不受影響
    assert re.search(r"\.gm-speak \{[^}]*margin-right: auto", sheet)                                                    # 語音按鈕靠左，翻頁按鈕仍靠右
