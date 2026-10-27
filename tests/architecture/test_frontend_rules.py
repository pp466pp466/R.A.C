"""架構檢查：前端禁用技術與離線規則（見 AGENTS.md 第 1、2 節與 offline-frontend-assets 規格）。

禁止：.vue 單檔元件、打包工具、CDN、外部字型服務、外部彈窗套件、Bootstrap 的 JavaScript、
v-html、樣板中寫死的 /static/ 路徑或外部網址，以及 data-bs-theme="dark" 以外的 data-bs-* 屬性。
第三方資產（vendor、fonts）本身不掃描內容，其完整性由資產清單負責。
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from app import create_app
from app.config import PROJECT_ROOT, load_config

SKIP_DIRS = {".venv", "venv", "node_modules", ".git", "__pycache__", ".pytest_cache", "openspec", "docs", ".claude", ".agents"}
APP_DIR = PROJECT_ROOT / "app"
TEMPLATES_DIR = APP_DIR / "templates"
OWN_ASSET_DIRS = [APP_DIR / "static" / "css", APP_DIR / "static" / "js"]  # 專案自己撰寫的前端檔案

CDN_AND_EXTERNAL_SERVICES = (
    "cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "ajax.googleapis.com", "fonts.googleapis.com",
    "fonts.gstatic.com", "code.jquery.com", "stackpath.bootstrapcdn.com", "maxcdn.bootstrapcdn.com",
    "cdn.tailwindcss.com", "use.fontawesome.com", "kit.fontawesome.com", "typekit.net",
)
BUNDLER_CONFIGS = ("vite.config", "webpack.config", "rollup.config", "parcel", ".babelrc", "esbuild.config", "snowpack.config")
BUNDLER_PACKAGES = ("vite", "webpack", "rollup", "parcel", "esbuild", "snowpack", "@vue/cli-service", "sweetalert", "sweetalert2")

EXTERNAL_ATTR = re.compile(r"""\b(?:src|href|action|poster|srcset)\s*=\s*["']\s*(?:https?:)?//""", re.I)
EXTERNAL_CSS_URL = re.compile(r"""url\(\s*["']?\s*(?:https?:)?//""", re.I)
ANY_ABSOLUTE_URL = re.compile(r"https?://", re.I)
HARDCODED_STATIC = re.compile(r"""["'(]\s*/static/""")
BOOTSTRAP_JS = re.compile(r"bootstrap(?:\.bundle)?(?:\.min)?\.js", re.I)
DATA_BS = re.compile(r"""\bdata-bs-[\w-]+(?:\s*=\s*["'][^"']*["'])?""", re.I)
ASSET_REF = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)
ASSET_EXTENSIONS = (".js", ".css", ".woff2", ".woff", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp")


def repo_files(suffixes: tuple[str, ...]) -> list[Path]:
    found = []
    for path in PROJECT_ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes and not (set(path.relative_to(PROJECT_ROOT).parts) & SKIP_DIRS):
            found.append(path)
    return found


# ---- 掃描規則（可單元測試）-----------------------------------------------------

def template_violations(source: str) -> list[str]:
    problems = []
    if ANY_ABSOLUTE_URL.search(source):
        problems.append("樣板含有絕對網址（http:// 或 https://）")
    if EXTERNAL_ATTR.search(source) or EXTERNAL_CSS_URL.search(source):
        problems.append("樣板引用外部資源")
    if HARDCODED_STATIC.search(source):
        problems.append("樣板寫死 /static/ 路徑，應使用 url_for")
    for match in ASSET_REF.finditer(source):
        ref = match.group(1)
        if ref.lower().split("?")[0].endswith(ASSET_EXTENSIONS) and "url_for(" not in ref:
            problems.append(f"資源 {ref} 未使用 url_for")
    if BOOTSTRAP_JS.search(source):
        problems.append("樣板載入 Bootstrap 的 JavaScript")
    for attr in DATA_BS.findall(source):
        if re.sub(r"\s+", "", attr).replace("'", '"').lower() != 'data-bs-theme="dark"':
            problems.append(f"不允許的 data-bs-* 屬性：{attr}")
    if "v-html" in source:
        problems.append("使用了 v-html（回覆文字一律以純文字渲染）")
    lowered = source.lower()
    for domain in CDN_AND_EXTERNAL_SERVICES:
        if domain in lowered:
            problems.append(f"引用外部服務：{domain}")
    if "sweetalert" in lowered:
        problems.append("使用了外部彈窗套件 SweetAlert")
    return problems


UNSAFE_DOM = re.compile(r"\.(?:innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write\s*\(|\beval\s*\(|new\s+Function\s*\(")


def strip_comments(source: str) -> str:
    """移除 /* */ 與 // 註解（不誤傷 http:// 這種網址中的雙斜線）。說明文字可以提到被禁用的寫法。"""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<![:\w])//[^\n]*", "", source)


def own_asset_violations(source: str) -> list[str]:
    source = strip_comments(source)
    problems = []
    if ANY_ABSOLUTE_URL.search(source):
        problems.append("含有絕對網址")
    if "v-html" in source:
        problems.append("使用了 v-html")
    if UNSAFE_DOM.search(source):
        problems.append("使用了會造成 XSS 的 DOM 寫法（innerHTML／eval 等）；請用文字插值或 textContent")
    lowered = source.lower()
    for domain in CDN_AND_EXTERNAL_SERVICES:
        if domain in lowered:
            problems.append(f"引用外部服務：{domain}")
    if re.search(r"\bdata-bs-(?!theme\b)", source, re.I):
        problems.append("使用了 data-bs-* 互動屬性")
    if "sweetalert" in lowered:
        problems.append("使用了外部彈窗套件 SweetAlert")
    return problems


# ---- 專案掃描 -----------------------------------------------------------------

def test_no_vue_single_file_components_anywhere():
    assert repo_files((".vue",)) == []


def test_no_bundler_configuration_files():
    offenders = [
        p.name for p in PROJECT_ROOT.rglob("*")
        if p.is_file() and not (set(p.relative_to(PROJECT_ROOT).parts) & SKIP_DIRS)
        and any(p.name.lower().startswith(prefix) for prefix in BUNDLER_CONFIGS)
    ]
    assert offenders == []


def test_package_json_if_present_declares_no_bundler_or_popup_library():
    package_json = PROJECT_ROOT / "package.json"
    if not package_json.is_file():
        return
    data = json.loads(package_json.read_text(encoding="utf-8-sig"))
    declared = {*data.get("dependencies", {}), *data.get("devDependencies", {})}
    assert not (declared & set(BUNDLER_PACKAGES)), f"不允許的前端相依：{declared & set(BUNDLER_PACKAGES)}"


def test_no_bootstrap_javascript_is_shipped():
    offenders = [p.name for p in (APP_DIR / "static" / "vendor").rglob("*.js") if "bootstrap" in p.name.lower()]
    assert offenders == []


def test_templates_follow_the_offline_rules():
    problems = {}
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        found = template_violations(path.read_text(encoding="utf-8-sig"))
        if found:
            problems[str(path.relative_to(PROJECT_ROOT))] = found
    assert problems == {}


def test_our_own_css_and_js_follow_the_offline_rules():
    problems = {}
    for folder in OWN_ASSET_DIRS:
        if not folder.is_dir():
            continue
        for path in sorted(p for p in folder.rglob("*") if p.suffix in (".css", ".js")):
            found = own_asset_violations(path.read_text(encoding="utf-8-sig"))
            if found:
                problems[str(path.relative_to(PROJECT_ROOT))] = found
    assert problems == {}


def test_python_code_references_no_cdn_or_external_ui_service():
    problems = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8-sig").lower()
        hits = [d for d in CDN_AND_EXTERNAL_SERVICES if d in text] + (["sweetalert"] if "sweetalert" in text else [])
        if hits:
            problems[str(path.relative_to(PROJECT_ROOT))] = hits
    assert problems == {}


def test_requirements_contain_no_popup_or_bundler_packages():
    for name in ("requirements.txt", "requirements-dev.txt"):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8").lower()
        assert "sweetalert" not in text


# ---- 實際渲染出來的頁面：所有資源都指向本站 -------------------------------------

class _ResourceCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[tuple[str, str]] = []
        self.data_bs: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("src", "href") and value:
                self.references.append((tag, value))
            if name.startswith("data-bs-"):
                self.data_bs.append((name, value))


def test_rendered_pages_load_only_local_resources_and_use_only_the_theme_attribute():
    client = create_app(load_config(environ={"APP_ENV": "testing"})).test_client()
    for path in ("/", "/this-page-does-not-exist"):
        html = client.get(path).get_data(as_text=True)
        collector = _ResourceCollector()
        collector.feed(html)
        external = [(tag, ref) for tag, ref in collector.references if re.match(r"(https?:)?//", ref)]
        assert external == [], f"{path} 載入了外部資源：{external}"
        assert collector.data_bs == [("data-bs-theme", "dark")], f"{path} 的 data-bs-* 屬性不符規定"
        local = [ref for _, ref in collector.references if ref.startswith("/static/")]
        assert any("bootstrap.min.css" in r for r in local)
        assert any("fonts.css" in r for r in local)
        assert any("vue.global.prod.js" in r for r in local)
        assert any("axios.min.js" in r for r in local)


# ---- 掃描規則本身的單元測試 ---------------------------------------------------

def test_scanner_flags_cdn_links_and_external_fonts():
    assert template_violations('<script src="https://cdn.jsdelivr.net/npm/vue@3"></script>')
    assert template_violations('<link href="//fonts.googleapis.com/css2?family=Noto+Sans+TC" rel="stylesheet">')
    assert template_violations("<style>@import url('https://fonts.googleapis.com/x');</style>")
    assert template_violations('<a href="http://example.com">x</a>')


def test_scanner_flags_hardcoded_static_paths_and_asset_refs_without_url_for():
    assert template_violations('<script src="/static/vendor/vue/vue.global.prod.js"></script>')
    assert template_violations('<link rel="stylesheet" href="css/site.css">')


def test_scanner_flags_bootstrap_javascript_and_interactive_data_attributes():
    assert template_violations('<script src="{{ url_for(\'static\', filename=\'vendor/bootstrap/bootstrap.bundle.min.js\') }}"></script>')
    assert template_violations('<button data-bs-toggle="modal" data-bs-target="#m">x</button>')
    assert template_violations('<div data-bs-dismiss="alert"></div>')


def test_scanner_flags_v_html_and_popup_libraries():
    assert template_violations('<div v-html="reply"></div>')
    assert template_violations("<script>Swal.fire()</script> sweetalert2")
    assert own_asset_violations("el.innerHTML = x")
    assert own_asset_violations("el.insertAdjacentHTML('beforeend', x)")
    assert own_asset_violations("eval(code)") and own_asset_violations("new Function('return 1')")
    assert own_asset_violations("const t = `<div v-html='x'>`")


def test_scanner_ignores_forbidden_words_inside_comments_but_not_urls_in_strings():
    commented = "/* 不要用 innerHTML 或 v-html */\n// 也不要用 eval(x)\nconst ok = textContent;\n"
    assert own_asset_violations(commented) == []
    assert own_asset_violations("const u = 'https://cdn.jsdelivr.net/x';")


def test_scanner_allows_the_theme_attribute_and_url_for_references():
    clean = (
        '<html lang="zh-Hant-TW" data-bs-theme="dark">'
        '<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'fonts/fonts.css\') }}">'
        '<script src="{{ url_for(\'static\', filename=\'vendor/vue/vue.global.prod.js\') }}"></script>'
        '<a href="/">首頁</a>'
    )
    assert template_violations(clean) == []
    assert template_violations("<html data-bs-theme = 'dark'>") == []
