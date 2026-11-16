"""報告素材的共用工具：找 Chrome、把 HTML 轉成 PNG 或 PDF、把 SVG 圖示轉成可內嵌的資料網址。

只用標準函式庫。圖與簡報是**文件素材**，不屬於應用程式：這裡不匯入 app 底下的任何東西，也不連線到資料庫。
所有檔案讀寫都明確指定 UTF-8（見 AGENTS.md 第 8 節）。
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DIAGRAMS_DIR = Path(__file__).resolve().parent
DOCS_DIR = DIAGRAMS_DIR.parent
PROJECT_ROOT = DOCS_DIR.parent
ICON_DIR = DIAGRAMS_DIR / "icons"
FONTS_CSS = PROJECT_ROOT / "app" / "static" / "fonts" / "fonts.css"

# 與 AGENTS.md 第 2 節的色彩規範一致（Cinematic Dark Minimalist）
PALETTE = {
    "bg": "#0F1012",          # 主背景：劇院深黑
    "panel": "#1C1E23",       # 容器（不透明版本，圖檔不需要半透明）
    "panel_alt": "#16181C",
    "text": "#E2E4E9",        # 主要文字：薄霧灰白（不使用純白）
    "muted": "#8A8F99",       # 次要文字：鋼鐵灰
    "accent": "#FF6B35",      # 焦點：導演琥珀橙
    "line": "#2A2D34",        # 極細分隔線
    "ok": "#4C9A6A",          # 已實作（路線圖的綠）
    "ok_bg": "#1F3D2B",
}

FONT_STACK = "'Noto Sans TC Variable','Noto Sans TC','Microsoft JhengHei','PingFang TC',sans-serif"
MONO_STACK = "Consolas,'Cascadia Mono','Noto Sans TC Variable','Microsoft JhengHei',monospace"


class RenderError(RuntimeError):
    pass


def find_chrome() -> str:
    """Chrome 或 Edge 的執行檔。環境變數 CHROME_PATH 優先。"""
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
        shutil.which("chrome") or "",
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    raise RenderError("找不到 Chrome 或 Edge；請安裝，或以環境變數 CHROME_PATH 指定執行檔。")


def file_url(path: Path) -> str:
    return path.resolve().as_uri()


def icon_data_uri(name: str) -> str:
    """把 icons/ 底下的 SVG 轉成 data URI（以 <image> 內嵌，避免多個圖示的漸層 id 互相衝突）。"""
    raw = (ICON_DIR / f"{name}.svg").read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")


def _run_chrome(args: list[str], *, timeout: int = 180) -> None:
    chrome = find_chrome()
    with tempfile.TemporaryDirectory(prefix="chrome-profile-") as profile:
        command = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--allow-file-access-from-files",
            "--hide-scrollbars",
            f"--user-data-dir={profile}",
            *args,
        ]
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        if result.returncode != 0:
            tail = result.stderr.decode("utf-8", errors="replace")[-800:]
            raise RenderError(f"Chrome 轉檔失敗（結束碼 {result.returncode}）：{tail}")


def html_to_png(html: Path, png: Path, *, width: int, height: int, scale: float = 2.0, wait_ms: int = 4000) -> None:
    """把 HTML 頁面截成 PNG。視窗大小就是輸出的版面大小（輸出像素 = 版面 x scale）。"""
    png.parent.mkdir(parents=True, exist_ok=True)
    if png.exists():
        png.unlink()
    _run_chrome(
        [
            f"--window-size={width},{height}",
            f"--force-device-scale-factor={scale}",
            f"--virtual-time-budget={wait_ms}",
            "--default-background-color=FF0F1012",
            f"--screenshot={png}",
            file_url(html),
        ]
    )
    if not png.is_file() or png.stat().st_size == 0:
        raise RenderError(f"沒有產生 {png}")


def html_to_pdf(html: Path, pdf: Path, *, wait_ms: int = 20000) -> None:
    """把 HTML 頁面列印成 PDF（頁面大小由 CSS 的 @page 決定）。"""
    pdf.parent.mkdir(parents=True, exist_ok=True)
    if pdf.exists():
        pdf.unlink()
    _run_chrome(
        [
            f"--virtual-time-budget={wait_ms}",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf}",
            file_url(html),
        ],
        timeout=300,
    )
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise RenderError(f"沒有產生 {pdf}")


def html_dump_dom(html: Path, *, wait_ms: int = 20000) -> str:
    """載入頁面、等 JavaScript 跑完，回傳渲染後的 DOM（用來檢查渲染結果）。"""
    chrome = find_chrome()
    with tempfile.TemporaryDirectory(prefix="chrome-profile-") as profile:
        command = [
            chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--allow-file-access-from-files", f"--user-data-dir={profile}", f"--virtual-time-budget={wait_ms}",
            "--dump-dom", file_url(html),
        ]
        result = subprocess.run(command, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        raise RenderError(f"Chrome 轉檔失敗（結束碼 {result.returncode}）：{result.stderr.decode('utf-8', errors='replace')[-800:]}")
    return result.stdout.decode("utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    """UTF-8（無 BOM）、LF。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8-sig") as handle:
        return handle.read()
