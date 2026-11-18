"""架構圖解共用的版面元件：標題列、「功能對應」表、色帶、頁面外殼與 4K PNG 輸出。

登入、MCP（build_diagrams.py）與入口、RPG 回合引擎、內容安全、部署（build_diagrams_system.py）共用。
版面寬度固定 1800，輸出 PNG 時以 3840 / 1800 的倍率放大，成品寬度剛好是 4K（UHD）的 3840 像素。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from render_tools import FONTS_CSS, FONT_STACK, PALETTE, file_url, html_to_png, write_text
from svg_kit import Svg, wrap

P = PALETTE
W = 1800
UHD_WIDTH = 3840          # 4K（UHD）寬度：所有圖解的 PNG 輸出寬度


# ---- 共用版面 ---------------------------------------------------------------------------------


def header(svg: Svg, *, tag: str, title: str, subtitle: str, title_size: int = 40) -> None:
    svg.text(40, 36, tag, size=14, fill=P["accent"], mono=True)
    svg.text(40, 82, title, size=title_size, weight=700)
    svg.text(40, 116, subtitle, size=19, fill=P["muted"])
    svg.text(W - 40, 40, "圖示：Azure Architecture Icons（僅作元件符號；展示環境為 AWS，production 規劃為 Google Cloud）", size=14, fill=P["muted"], anchor="end")
    svg.text(W - 40, 62, "卡片內「Azure 圖示：…」是圖示名稱，並不代表使用該 Azure 服務", size=13, fill=P["muted"], anchor="end")
    svg.text(W - 40, 84, "橘色圓圈的數字＝流程步驟，對應下方的「功能對應」表", size=13, fill=P["muted"], anchor="end")


def mapping_table(svg: Svg, y: float, *, title: str, headers: tuple[str, str, str, str], rows: list[tuple[str, str, str, str]],
                  widths: tuple[int, int, int] = (400, 760, 480)) -> float:
    """底部的「功能對應」表：步驟徽章 ｜ 動作 ｜ 程式 ｜ 資料表。回傳表格下方的 y。"""
    svg.text(40, y + 8, title, size=26, weight=700)
    y += 30
    x_badge, x_action = 56, 100
    x_code = x_action + widths[0] + 20
    x_db = x_code + widths[1] + 20
    svg.rect(30, y, W - 60, 40, fill=P["panel"], stroke=P["line"], rx=8)
    svg.text(x_action, y + 26, headers[0], size=16, fill=P["accent"], weight=700)
    svg.text(x_code, y + 26, headers[1], size=16, fill=P["accent"], weight=700)
    svg.text(x_db, y + 26, headers[2], size=16, fill=P["accent"], weight=700)
    svg.text(x_badge, y + 26, headers[3], size=16, fill=P["accent"], weight=700, anchor="middle")
    y += 48
    size, lh = 15, 22
    for index, (number, action, code, db) in enumerate(rows):
        a = wrap(action, size, widths[0], mono=False)
        c = wrap(code, size, widths[1], mono=False)
        d = wrap(db, size, widths[2], mono=False)
        height = max(len(a), len(c), len(d)) * lh + 20
        if index % 2 == 0:
            svg.rect(30, y, W - 60, height, fill="#14161A", rx=8)
        svg.badge(x_badge, y + 20, number, r=13, size=15)
        svg.lines(x_action, y + 25, a, size=size, lh=lh, weight=600)
        svg.lines(x_code, y + 25, c, size=size, lh=lh, fill=P["text"])
        svg.lines(x_db, y + 25, d, size=size, lh=lh, fill=P["muted"])
        y += height + 4
    return y


def page_html(svg_markup: str, width: int, height: int) -> str:
    return (
        f'<!doctype html><html lang="zh-Hant-TW"><head><meta charset="UTF-8">'
        f'<link rel="stylesheet" href="{file_url(FONTS_CSS)}">'
        f'<style>html,body{{margin:0;padding:0;background:{P["bg"]};font-family:{FONT_STACK}}}svg{{display:block}}</style>'
        f"</head><body>{svg_markup}</body></html>"
    )


def render_png(svg: Svg, png: Path) -> None:
    """輸出 4K（UHD）寬度的 PNG：版面寬 1800，縮放倍率取 3840 / 1800，所以成品寬度剛好是 3840 像素。"""
    markup = svg.render()
    scale = UHD_WIDTH / svg.width
    with tempfile.TemporaryDirectory(prefix="diagram-") as folder:
        page = Path(folder) / "diagram.html"
        write_text(page, page_html(markup, svg.width, svg.height))
        html_to_png(page, png, width=svg.width, height=svg.height, scale=scale)
    print(f"已輸出 {png}（版面 {svg.width} x {svg.height}，輸出寬 {UHD_WIDTH} 像素）")


def band(svg: Svg, x: float, y: float, w: float, *, label: str, chips: list, size: float = 14, mono: bool = False) -> float:
    """分層的色帶：標籤 + 膠囊標籤。先量高度再畫底，回傳色帶高度。"""
    tmp = Svg(W, 10)
    bottom = tmp.chips(x + 14, y + 36, w - 28, chips, size=size, mono=mono)
    height = bottom - y + 12
    svg.rect(x, y, w, height, fill=P["panel_alt"], stroke=P["line"], rx=10)
    svg.text(x + 14, y + 22, label, size=12, fill=P["accent"], mono=True)
    svg.parts.extend(tmp.parts)
    return height
