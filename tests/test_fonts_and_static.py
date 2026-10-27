"""offline-frontend-assets 規格：字型與前端庫由本機提供。"""
import re

import pytest

from app import create_app
from app.config import PROJECT_ROOT, load_config

FONTS_DIR = PROJECT_ROOT / "app" / "static" / "fonts"
FONTS_CSS = FONTS_DIR / "fonts.css"
FONT_BUDGET_BYTES = 6 * 1024 * 1024  # 全部字型切片的總量上限，避免不小心變肥

# 繁體中文介面最常用的字與標點；每一個都必須由本機字型切片涵蓋
SAMPLE_TEXT = (
    "繁體中文智能客服會員登入註冊密碼信箱驗證設定訂單退款意見回饋系統運作中發生錯誤請稍後再試"
    "，。、！？：；「」『』（）—…"
    "ABCxyz0123456789"
)


def read_css() -> str:
    return FONTS_CSS.read_text(encoding="utf-8")


def parse_unicode_ranges(css: str) -> set[int]:
    covered: set[int] = set()
    for declaration in re.findall(r"unicode-range:\s*([^;]+);", css):
        for item in declaration.split(","):
            item = item.strip().removeprefix("U+")
            if "-" in item:
                low, high = item.split("-")
                covered.update(range(int(low, 16), int(high, 16) + 1))
            else:
                covered.add(int(item.replace("?", "0"), 16))
    return covered


@pytest.fixture()
def client():
    return create_app(load_config(environ={"APP_ENV": "testing"})).test_client()


# ---- fonts.css 的內容 ---------------------------------------------------------

def test_fonts_css_starts_with_charset_and_uses_only_local_files():
    css = read_css()
    assert css.startswith('@charset "UTF-8";')
    assert "http://" not in css and "https://" not in css
    assert "//" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S).replace("url(./", "")  # 沒有 protocol-relative 網址


def test_every_font_face_is_swappable_ranged_and_uses_the_expected_family():
    css = read_css()
    blocks = re.findall(r"@font-face\s*\{(.*?)\}", css, flags=re.S)
    assert len(blocks) == 108
    for block in blocks:
        assert "font-family: 'Noto Sans TC Variable'" in block
        assert "font-display: swap" in block
        assert "unicode-range:" in block
        assert "font-weight: 100 900" in block


def test_every_referenced_file_exists_and_no_font_file_is_orphaned():
    referenced = set(re.findall(r"url\(\./([^)]+\.woff2)\)", read_css()))
    on_disk = {p.name for p in FONTS_DIR.glob("*.woff2")}
    assert referenced, "fonts.css 沒有引用任何字型檔"
    assert referenced == on_disk


def test_common_traditional_chinese_text_is_covered_by_the_local_font():
    covered = parse_unicode_ranges(read_css())
    missing = [ch for ch in SAMPLE_TEXT if ord(ch) not in covered]
    assert missing == [], f"這些字元沒有被本機字型涵蓋，會退回後備字型：{missing}"


def test_total_font_size_stays_within_budget():
    total = sum(p.stat().st_size for p in FONTS_DIR.glob("*.woff2"))
    assert 0 < total < FONT_BUDGET_BYTES, f"字型總量 {total / 1024 / 1024:.1f} MB 超過預算"


def test_font_license_file_is_shipped():
    assert "SIL OPEN FONT LICENSE" in (FONTS_DIR / "LICENSE").read_text(encoding="utf-8").upper()


# ---- 由應用程式自己提供 -------------------------------------------------------

@pytest.mark.parametrize(
    ("path", "needle"),
    [
        ("/static/vendor/vue/vue.global.prod.js", b"Vue"),
        ("/static/vendor/bootstrap/bootstrap.min.css", b"Bootstrap"),
        ("/static/vendor/axios/axios.min.js", b"axios"),
    ],
)
def test_vendor_libraries_are_served_locally(client, path, needle):
    response = client.get(path)
    assert response.status_code == 200
    assert needle.lower() in response.data.lower()


def test_font_css_and_a_slice_are_served_with_proper_types(client):
    css = client.get("/static/fonts/fonts.css")
    assert css.status_code == 200 and css.content_type.startswith("text/css")
    slice_response = client.get("/static/fonts/noto-sans-tc-0-wght-normal.woff2")
    assert slice_response.status_code == 200
    assert "woff2" in slice_response.content_type
    assert slice_response.data[:4] == b"wOF2"  # woff2 檔案魔術位元組


def test_missing_static_file_is_404(client):
    assert client.get("/static/vendor/vue/not-there.js").status_code == 404
