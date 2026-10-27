"""shared-ui-components 規格：全站色彩規範（只檢查專案自己撰寫的樣式，不掃第三方的 Bootstrap）。"""
import re

import pytest

from app import create_app
from app.config import PROJECT_ROOT, load_config

CSS_DIR = PROJECT_ROOT / "app" / "static" / "css"
THEME = CSS_DIR / "theme.css"

EXPECTED_TOKENS = {
    "--color-bg": "#0F1012",
    "--color-surface": "rgba(28, 30, 35, 0.75)",
    "--color-text": "#E2E4E9",
    "--color-text-muted": "#8A8F99",
    "--color-accent": "#FF6B35",
    "--color-border": "#2A2D34",
    "--color-modal-overlay": "rgba(15, 16, 18, 0.85)",
    "--color-modal-surface": "rgba(22, 24, 28, 0.95)",
}
# 「white」只在它是完整的顏色關鍵字時才算（不誤抓 white-space 等屬性名稱）
PURE_WHITE = re.compile(r"#fff(?:fff)?\b|(?<![\w-])white(?![\w-])|rgba?\(\s*255\s*,\s*255\s*,\s*255", re.IGNORECASE)


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def root_tokens() -> dict[str, str]:
    root_block = re.search(r":root\s*\{(.*?)\}", strip_comments(THEME.read_text(encoding="utf-8")), re.S).group(1)
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", root_block)}


def own_css_files():
    return sorted(CSS_DIR.glob("*.css"))


def test_color_tokens_match_the_specification_exactly():
    tokens = root_tokens()
    for name, value in EXPECTED_TOKENS.items():
        assert tokens.get(name) == value, f"{name} 應為 {value}，實際為 {tokens.get(name)}"


def test_border_is_a_one_pixel_hairline_in_the_border_colour():
    assert root_tokens()["--border-hairline"] == "1px solid var(--color-border)"


def test_font_stack_starts_with_the_local_font_and_falls_back_as_specified():
    stack = root_tokens()["--font-sans"]
    order = [stack.index(name) for name in ('"Noto Sans TC Variable"', "Microsoft JhengHei", "PingFang TC")]
    assert order == sorted(order)
    assert stack.rstrip().endswith("sans-serif")


def test_no_own_stylesheet_uses_pure_white():
    for path in own_css_files():
        found = PURE_WHITE.findall(strip_comments(path.read_text(encoding="utf-8")))
        assert found == [], f"{path.name} 使用了純白：{found}"


def test_white_detector_flags_real_whites_but_not_white_space():
    assert PURE_WHITE.search("color: white;")
    assert PURE_WHITE.search("color:#FFF;") and PURE_WHITE.search("color: #ffffff")
    assert PURE_WHITE.search("color: rgb(255, 255, 255)")
    assert not PURE_WHITE.search("white-space: nowrap;")
    assert not PURE_WHITE.search("color: var(--color-text);")


def test_bootstrap_dark_mode_variables_are_overridden_so_its_white_never_surfaces():
    css = strip_comments(THEME.read_text(encoding="utf-8"))
    for variable in (
        "--bs-body-bg", "--bs-body-color", "--bs-emphasis-color", "--bs-heading-color", "--bs-border-color",
        "--bs-code-color", "--bs-highlight-bg", "--bs-link-color",
    ):
        assert re.search(rf"{re.escape(variable)}\s*:", css), f"{variable} 沒有被覆蓋"
    assert 'html[data-bs-theme="dark"]' in css


def test_every_colour_in_theme_outside_the_token_block_goes_through_variables():
    css = strip_comments(THEME.read_text(encoding="utf-8"))
    outside_root = re.sub(r":root\s*\{.*?\}", "", css, flags=re.S)
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", outside_root)
    # --bs-*-rgb 變數需要「逗號分隔的 RGB 數字」，不是顏色字面值，上面的規則不會誤抓
    assert literals == [], f"theme.css 在 :root 之外直接寫了顏色字面值：{literals}"


@pytest.fixture()
def client():
    return create_app(load_config(environ={"APP_ENV": "testing"})).test_client()


def test_page_links_the_theme_and_the_stylesheet_is_served(client):
    html = client.get("/").get_data(as_text=True)
    assert "css/theme.css" in html
    response = client.get("/static/css/theme.css")
    assert response.status_code == 200 and response.content_type.startswith("text/css")
    assert b"#0F1012" in response.data
