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
