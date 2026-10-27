"""shared-ui-components 規格：Modal 與 Toast 的樣式數值、結構與「不依賴 Bootstrap 互動腳本」的靜態檢查。

視覺與互動行為（Esc、焦點、淡入方向）另由開發示範頁人工驗收，見 README「共用元件」。
"""
import re

import pytest

from app import create_app
from app.config import PROJECT_ROOT, load_config

CSS_DIR = PROJECT_ROOT / "app" / "static" / "css"
JS_DIR = PROJECT_ROOT / "app" / "static" / "js" / "ui"
THEME_CSS = (CSS_DIR / "theme.css").read_text(encoding="utf-8")
COMPONENTS_CSS = (CSS_DIR / "components.css").read_text(encoding="utf-8")
COMPONENTS_JS = (JS_DIR / "ui-components.js").read_text(encoding="utf-8")


def strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def strip_js_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?<![:\w])//[^\n]*", "", js)


def tokens() -> dict[str, str]:
    block = re.search(r":root\s*\{(.*?)\}", strip_css_comments(THEME_CSS), re.S).group(1)
    return {n: v.strip() for n, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


def resolve(value: str) -> str:
    """把 var(--x) 換成 theme.css 定義的實際值（可巢狀）。"""
    token_map = tokens()
    for _ in range(5):
        value = re.sub(r"var\((--[\w-]+)\)", lambda m: token_map[m.group(1)], value)
    return value.strip()


def rule(selector: str) -> dict[str, str]:
    """取出完全符合 selector 的規則（合併重複的規則）的屬性，值已解析 var()。"""
    css = strip_css_comments(COMPONENTS_CSS)
    props: dict[str, str] = {}
    for sel, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
        if sel.strip() == selector:
            for name, value in re.findall(r"([\w-]+)\s*:\s*([^;]+);", body):
                props[name] = resolve(value)
    assert props, f"components.css 找不到規則 {selector}"
    return props


# ---- Modal 樣式 ---------------------------------------------------------------

def test_modal_overlay_is_a_fullscreen_dark_blurred_layer():
    overlay = rule(".ui-modal-overlay")
    assert overlay["position"] == "fixed" and overlay["inset"] == "0"
    assert overlay["background"] == "rgba(15, 16, 18, 0.85)"
    assert overlay["backdrop-filter"] == "blur(8px)"


def test_modal_container_has_the_specified_background_and_hairline_border():
    modal = rule(".ui-modal")
    assert modal["background"] == "rgba(22, 24, 28, 0.95)"
    assert modal["border"] == "1px solid #2A2D34"


def test_modal_status_code_sits_small_in_the_top_left_corner():
    code = rule(".ui-modal__code")
    assert code["position"] == "absolute"
    assert code["top"] and code["left"] and "right" not in code and "bottom" not in code
    assert float(code["font-size"].removesuffix("rem")) <= 0.75   # 極小系統狀態碼


def test_scrolling_is_locked_while_a_modal_is_open():
    assert rule("body.ui-modal-open")["overflow"] == "hidden"


# ---- Toast 樣式 ---------------------------------------------------------------

def test_toast_region_is_anchored_to_the_bottom_right():
    region = rule(".ui-toast-region")
    assert region["position"] == "fixed" and region["right"] and region["bottom"]
    assert "left" not in region and "top" not in region
    assert region["flex-direction"] == "column"                    # 多則時由下往上堆疊


def test_toast_has_a_two_pixel_amber_line_on_the_left_only():
    toast = rule(".ui-toast")
    assert toast["border-left"] == "2px solid #FF6B35"
    assert toast["border"] == "1px solid #2A2D34"


def test_toast_fades_in_from_below():
    enter = rule(".ui-toast-enter-from")
    assert enter["opacity"] == "0"
    offset = re.fullmatch(r"translateY\((\d+)px\)", enter["transform"])
    assert offset and int(offset.group(1)) > 0                      # 起點在下方，向上移動


def test_motion_is_disabled_for_users_who_prefer_reduced_motion():
    css = strip_css_comments(COMPONENTS_CSS)
    media = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*)\}\s*$", css, re.S)
    assert media and "transition: none" in media.group(1)
    for selector in (".ui-toast-enter-active", ".ui-toast-move", ".ui-fade-enter-active"):
        assert selector in media.group(1)


# ---- Modal／Toast 的結構與規則（靜態檢查原始碼）----------------------------------

CODE_ONLY_JS = strip_js_comments(COMPONENTS_JS)


@pytest.mark.parametrize(
    "needle",
    [
        'role="dialog"',
        'aria-modal="true"',
        'v-if="modelValue"',                 # 顯示／隱藏由 Vue 響應式狀態接管
        "[ {{ statusCode }} ]",              # 左上角系統狀態碼
        "'Escape'",                          # Esc 關閉
        "'Tab'",                             # 焦點限制
        "previouslyFocused",                 # 關閉後還原焦點
        "aria-labelledby",
    ],
)
def test_modal_source_contains_the_required_behaviour(needle):
    assert needle in CODE_ONLY_JS


def test_modal_default_status_code_and_chinese_texts():
    for text in ("SYSTEM_PROMPT", "確認", "取消", "關閉提示"):
        assert text in CODE_ONLY_JS


def test_toast_region_renders_from_reactive_state_with_a_transition_group():
    assert 'v-for="item in store.state.items"' in CODE_ONLY_JS
    assert 'name="ui-toast"' in CODE_ONLY_JS
    assert "{{ item.message }}" in CODE_ONLY_JS                     # 純文字插值
    assert "Vue.reactive" in CODE_ONLY_JS                           # 狀態是響應式的


def test_components_never_touch_bootstrap_javascript_or_data_bs_attributes():
    for path in sorted(JS_DIR.glob("*.js")):
        code = strip_js_comments(path.read_text(encoding="utf-8"))
        assert not re.search(r"\bbootstrap\b", code, re.I), f"{path.name} 引用了 Bootstrap 物件"
        assert "data-bs-" not in code, f"{path.name} 使用了 data-bs-* 屬性"
    assert "data-bs-" not in COMPONENTS_CSS


def test_components_expose_the_documented_global_api():
    for name in ("UI.install", "UI.useToast", "UI.toast", "UI.Modal", "UI.ToastRegion", "UI.toasts"):
        assert name.replace("UI.", "root.UI.") in CODE_ONLY_JS or name in CODE_ONLY_JS


# ---- 頁面實際載入順序 ---------------------------------------------------------

def test_base_page_loads_scripts_in_dependency_order():
    client = create_app(load_config(environ={"APP_ENV": "testing"})).test_client()
    html = client.get("/").get_data(as_text=True)
    order = [html.index(name) for name in ("vue.global.prod.js", "typewriter.js", "toast-store.js", "ui-components.js")]
    assert order == sorted(order), "Vue 必須先載入，且 toast-store 要在 ui-components 之前"
    assert html.index("theme.css") < html.index("components.css")
    for path in ("/static/css/components.css", "/static/js/ui/ui-components.js", "/static/js/ui/toast-store.js", "/static/js/ui/typewriter.js"):
        assert client.get(path).status_code == 200
