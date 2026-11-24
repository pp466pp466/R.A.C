"""產生 docs/5.0簡報.pptx：把 2026-11-24 的 2.0 簡報彙整成 16 張只放重點的 PowerPoint（含一張「開發工具」），給主管與評審，字夠大、投影幕上也看得清楚。

用法（專案根目錄）：
    python docs/diagrams/build_pptx_50.py --pptx-libs <資料夾>
    python docs/diagrams/build_pptx_50.py --pptx-libs <資料夾> --check      只檢查（版面估算與數字是否出現在 2.0 簡報），不寫檔
    python docs/diagrams/build_pptx_50.py --pptx-libs <資料夾> --output docs/5.0簡報.pptx

與 build_pptx.py（2.1，把 2.0 的 Markdown 逐頁轉成 PowerPoint）不同：5.0（版型沿用 build_pptx_40.py 的 4.0）是**手工編排**的精簡版，內容寫在本檔的各個 slide_* 函式裡，
重點取自 docs/2.0簡報.md；數字要和 2.0 簡報一致，`--check` 會逐一確認 FACTS 裡的字串都出現在 2.0簡報.md（簡報改版後先跑它）。

設計：
- 16:9（13.333 x 7.5 吋）；Cinematic Dark Minimalist（與產品、2.0 簡報同一套色彩，沒有純白）。內文一律用主要文字色，不使用偏淡的次要灰
  （投影時偏淡的字會看不清楚）；次要灰只用在頁碼。
- 字級：標題 44 pt；卡片標題 28 至 32 pt；內文 26 至 28 pt；最小的說明文字 24 pt（頁碼與頁首標籤 16 至 18 pt 不算內文）。每頁最多 5、6 個重點。
- 圖是 PowerPoint 的原生形狀（可以編輯），不嵌入 2.0 的 4K 圖解（字太小，投影看不清楚）。
- 字型用 Microsoft JhengHei（專案的後備字型）；Noto Sans TC 是網頁用的自託管切片，無法內嵌在 pptx 裡。
- 每張都有講者備忘（放被刪掉的細節），投影片上只留重點。

python-pptx 只在產生文件時用到，不屬於應用程式，不放進版本庫與 requirements：
    pip install python-pptx --target <資料夾>        （--pptx-libs，或環境變數 PPTX_LIBS）

版面是以字數估算高度；估算放不下的地方會列在輸出裡，結束代碼為 3（檔案仍會寫出）。**有版面問題時不要自己縮字級，先回報。**
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


def _prepare_path() -> None:
    libs = os.environ.get("PPTX_LIBS")
    if "--pptx-libs" in sys.argv:
        index = sys.argv.index("--pptx-libs")
        if index + 1 < len(sys.argv):
            libs = sys.argv[index + 1]
    if libs:
        sys.path.insert(0, libs)


_prepare_path()
try:
    from lxml import etree
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.oxml.ns import qn
    from pptx.util import Emu, Inches, Pt
except ImportError as error:  # pragma: no cover - 缺工具時的說明
    raise SystemExit(f"找不到 python-pptx（{error}）。請先 pip install python-pptx --target <資料夾>，再用 --pptx-libs 指定。")

from render_tools import DOCS_DIR, PALETTE

SOURCE_MD = DOCS_DIR / "2.0簡報.md"
OUTPUT = DOCS_DIR / "5.0簡報.pptx"

# ---- 色彩與字型 ------------------------------------------------------------------------------------------------------

BG = PALETTE["bg"].lstrip("#")             # 0F1012 主背景
PANEL = PALETTE["panel"].lstrip("#")       # 1C1E23 容器
PANEL_ALT = PALETTE["panel_alt"].lstrip("#")
TEXT = PALETTE["text"].lstrip("#")         # E2E4E9 主要文字（不使用純白）
MUTED = PALETTE["muted"].lstrip("#")       # 8A8F99 只用在頁碼
ACCENT = PALETTE["accent"].lstrip("#")     # FF6B35 焦點
LINE = PALETTE["line"].lstrip("#")         # 2A2D34 分隔線
OK = PALETTE["ok"].lstrip("#")             # 4C9A6A 已完成
OK_BG = PALETTE["ok_bg"].lstrip("#")       # 1F3D2B
FONT = "Microsoft JhengHei"

SLIDE_W, SLIDE_H = 13.333, 7.5
MARGIN_X = 0.7
CONTENT_W = SLIDE_W - 2 * MARGIN_X        # 11.933
TOTAL = 16

# 版面估算：字寬以字級為單位（全形字約 1 em）、行高約 1.32 倍字級
LINE_FACTOR = 1.32
LINT: list[str] = []

# 投影片上出現的數字，必須全部出現在 2.0簡報.md（--check 逐一確認）
FACTS = [
    "15,170", "1,963", "103 份主規格", "35 個 change 已封存", "0023", "b63f4e7", "2026-11-24", "rpg-story-tts", "劇情朗讀",
    "每日簽到 200", "上限 1000", "一輪 30 幣", "2000／5000／10000", "安全模式開（預設）／關",
]


def rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color)


# ---- 版面估算 ---------------------------------------------------------------------------------------------------------

def char_width(ch: str, size: float) -> float:
    code = ord(ch)
    if code >= 0x2000:
        return size                               # 中文、全形標點、箭頭、× 等
    if ch == " ":
        return size * 0.3
    if ch.isdigit():
        return size * 0.56
    if ch.isupper():
        return size * 0.64
    if ch.isalpha():
        return size * 0.54
    return size * 0.42


def count_lines(text: str, size: float, width_in: float) -> int:
    width = width_in * 72
    lines = 0
    for part in text.split("\n"):
        lines += 1
        used = 0.0
        for ch in part:
            w = char_width(ch, size)
            if used + w > width:
                lines += 1
                used = w
            else:
                used += w
    return lines


def paragraph_text(paragraph) -> str:
    if isinstance(paragraph, str):
        return paragraph
    return "".join(run if isinstance(run, str) else run[0] for run in paragraph)


# ---- 形狀與文字的小工具 ----------------------------------------------------------------------------------------------

def _strip_style(shape) -> None:
    """拿掉形狀預設的 p:style，避免主題的陰影、漸層與框線混進來。"""
    style = shape._element.find(qn("p:style"))
    if style is not None:
        shape._element.remove(style)


def _set_fonts(run, size: float, bold: bool, color: str) -> None:
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = rgb(color)
    run.font.name = FONT
    properties = run.font._element
    for tag in ("a:ea", "a:cs"):
        element = etree.SubElement(properties, qn(tag))
        element.set("typeface", FONT)


class Deck:
    """包住 python-pptx 的投影片集合，記錄目前頁碼以便回報版面問題。"""

    def __init__(self) -> None:
        self.prs = Presentation()
        self.prs.slide_width = Emu(12192000)
        self.prs.slide_height = Emu(6858000)
        size = self.prs._element.find(qn("p:sldSz"))
        if size is not None and "type" in size.attrib:
            del size.attrib["type"]                # 預設範本標成 4:3，與實際的 16:9 尺寸矛盾
        self._theme()
        self.number = 0
        self.slide = None

    # 主題：新增的文字與背景在 PowerPoint 裡也是深色、同一組字型
    def _theme(self) -> None:
        master = self.prs.slide_master
        part = master.part.part_related_by(RT.THEME)
        xml = part.blob.decode("utf-8")
        colors = {"dk1": BG, "lt1": TEXT, "dk2": PANEL, "lt2": MUTED, "accent1": ACCENT, "accent2": OK, "accent3": LINE,
                  "accent4": MUTED, "accent5": OK_BG, "accent6": PANEL_ALT, "hlink": ACCENT, "folHlink": MUTED}
        for name, value in colors.items():
            pattern = re.compile(rf"<a:{name}>.*?</a:{name}>", re.S)
            if not pattern.search(xml):
                raise SystemExit(f"主題缺少色彩 {name}，無法套用")
            xml = pattern.sub(f'<a:{name}><a:srgbClr val="{value}"/></a:{name}>', xml, count=1)
        for group in ("majorFont", "minorFont"):
            pattern = re.compile(rf"(<a:{group}>)(.*?)(</a:{group}>)", re.S)
            match = pattern.search(xml)
            if not match:
                raise SystemExit(f"主題缺少 {group}")
            body = re.sub(r'<a:latin typeface="[^"]*"[^>]*/>', f'<a:latin typeface="{FONT}"/>', match.group(2), count=1)
            body = re.sub(r'<a:ea typeface="[^"]*"/>', f'<a:ea typeface="{FONT}"/>', body, count=1)
            xml = xml[:match.start()] + match.group(1) + body + match.group(3) + xml[match.end():]
        part._blob = xml.encode("utf-8")
        mapping = master._element.find(qn("p:clrMap"))
        mapping.set("bg1", "dk1")
        mapping.set("tx1", "lt1")
        mapping.set("bg2", "dk2")
        mapping.set("tx2", "lt2")

    def add_slide(self, tag: str, title: str | None, notes: str) -> "Deck":
        layout = self.prs.slide_layouts[6]          # Blank
        self.slide = self.prs.slides.add_slide(layout)
        self.number += 1
        if title is not None:
            self.text(MARGIN_X, 0.32, 8.0, 0.4, [[(tag, {"bold": True})]], size=18, color=ACCENT, name="頁首標籤", lint=False)
            self.text(MARGIN_X, 0.72, CONTENT_W, 0.85, [[(title, {"bold": True})]], size=44, name="標題", anchor="middle", margin=(0.0, 0.0))
            self.text(SLIDE_W - MARGIN_X - 2.0, 7.0, 2.0, 0.35, [f"{self.number} / {TOTAL}"], size=16, color=MUTED,
                      align="r", name="頁碼", lint=False)
        self.slide.notes_slide.notes_text_frame.text = notes
        return self

    # -- 文字
    def text(self, x, y, w, h, paragraphs, size=28, color=TEXT, bold=False, align="l", anchor="t", name=None,
             margin=(0.05, 0.03), space_after=0.0, lint=True):
        box = self.slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        if name:
            box.name = name
        frame = box.text_frame
        frame.word_wrap = True
        frame.auto_size = MSO_AUTO_SIZE.NONE
        frame.margin_left = frame.margin_right = Inches(margin[0])
        frame.margin_top = frame.margin_bottom = Inches(margin[1])
        frame.vertical_anchor = {"t": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}[anchor]
        need = 0.0
        for index, paragraph in enumerate(paragraphs):
            target = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            target.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}[align]
            target.space_after = Pt(space_after)
            runs = [paragraph] if isinstance(paragraph, str) else paragraph
            biggest = size
            for run in runs:
                text, options = (run, {}) if isinstance(run, str) else run
                piece = target.add_run()
                piece.text = text
                run_size = options.get("size", size)
                biggest = max(biggest, run_size)
                _set_fonts(piece, run_size, options.get("bold", bold), options.get("color", color))
            lines = count_lines(paragraph_text(paragraph), biggest, w - 2 * margin[0])
            need += lines * biggest * LINE_FACTOR / 72 + space_after / 72
        need += 2 * margin[1]
        if lint and need > h + 0.02:
            LINT.append(f"第 {self.number} 頁「{name or paragraph_text(paragraphs[0])[:12]}」：估算需要 {need:.2f} 吋，只有 {h:.2f} 吋")
        return box

    # -- 形狀
    def shape(self, kind, x, y, w, h, fill=None, line=None, line_w=1.0, radius=None, name=None, alpha=None):
        item = self.slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
        _strip_style(item)
        if name:
            item.name = name
        if fill is None:
            item.fill.background()
        else:
            item.fill.solid()
            item.fill.fore_color.rgb = rgb(fill)
            if alpha is not None:
                color = item.fill._xPr.find(qn("a:solidFill")).find(qn("a:srgbClr"))
                etree.SubElement(color, qn("a:alpha")).set("val", str(int(alpha * 1000)))
        if line is None:
            item.line.fill.background()
        else:
            item.line.color.rgb = rgb(line)
            item.line.width = Pt(line_w)
        if radius is not None and kind == MSO_SHAPE.ROUNDED_RECTANGLE:
            item.adjustments[0] = radius
        return item

    def card(self, x, y, w, h, fill=PANEL, line=LINE, name="卡片"):
        return self.shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h, fill=fill, line=line, line_w=1.0, radius=0.06, name=name)

    def badge(self, x, y, d, label, size=24, fill=ACCENT, color=BG, name="編號"):
        item = self.shape(MSO_SHAPE.OVAL, x, y, d, d, fill=fill, name=name)
        frame = item.text_frame
        frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        frame.word_wrap = False
        paragraph = frame.paragraphs[0]
        paragraph.alignment = PP_ALIGN.CENTER
        run = paragraph.add_run()
        run.text = label
        _set_fonts(run, size, True, color)
        return item

    def dot(self, x, y, d, done=True):
        if done:
            return self.shape(MSO_SHAPE.OVAL, x, y, d, d, fill=OK, name="已完成")
        return self.shape(MSO_SHAPE.OVAL, x, y, d, d, fill=None, line=ACCENT, line_w=3, name="待處理")

    def arrow(self, x1, y1, x2, y2, color=ACCENT, width=2.5):
        connector = self.slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
        _strip_style(connector)
        connector.line.color.rgb = rgb(color)
        connector.line.width = Pt(width)
        line = connector.line._get_or_add_ln()
        tail = etree.SubElement(line, qn("a:tailEnd"))
        tail.set("type", "triangle")
        connector.name = "箭頭"
        return connector

    def table(self, x, y, col_widths, row_h, rows, header_color=ACCENT, size=26):
        graphic = self.slide.shapes.add_table(len(rows), len(col_widths), Inches(x), Inches(y), Inches(sum(col_widths)), Inches(row_h * len(rows)))
        graphic.name = "表格"
        table = graphic.table
        properties = table._tbl.tblPr
        for attribute in ("firstRow", "bandRow", "firstCol", "lastRow", "lastCol", "bandCol"):
            properties.set(attribute, "0")
        style = properties.find(qn("a:tableStyleId"))
        if style is not None:
            properties.remove(style)
        for index, width in enumerate(col_widths):
            table.columns[index].width = Inches(width)
        for r, row in enumerate(rows):
            table.rows[r].height = Inches(row_h)
            for c, value in enumerate(row):
                cell = table.cell(r, c)
                cell.margin_left = cell.margin_right = Inches(0.1)
                cell.margin_top = cell.margin_bottom = Inches(0.04)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.fill.solid()
                cell.fill.fore_color.rgb = rgb(PANEL if r == 0 else PANEL_ALT)
                paragraph = cell.text_frame.paragraphs[0]
                paragraph.alignment = PP_ALIGN.LEFT if c == 0 else PP_ALIGN.CENTER
                run = paragraph.add_run()
                run.text = value
                head = r == 0
                _set_fonts(run, size, head or c > 0, header_color if head else TEXT)
                lines = count_lines(value, size, col_widths[c] - 0.2)
                if lines * size * LINE_FACTOR / 72 + 0.08 > row_h + 0.02:
                    LINT.append(f"第 {self.number} 頁表格（第 {r + 1} 列第 {c + 1} 欄「{value}」）放不下")
                self._border(cell)
        return table

    @staticmethod
    def _border(cell) -> None:
        properties = cell._tc.get_or_add_tcPr()
        for side in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
            existing = properties.find(qn(side))
            if existing is not None:
                properties.remove(existing)
        for position, side in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):
            line = etree.Element(qn(side))
            line.set("w", "12700")
            fill = etree.SubElement(line, qn("a:solidFill"))
            etree.SubElement(fill, qn("a:srgbClr")).set("val", LINE)
            properties.insert(position, line)        # 框線依 L、R、T、B 的順序排在填色之前

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.prs.save(str(path))


# ---- 共用的版面片段 ---------------------------------------------------------------------------------------------------

def titled_card(deck: Deck, x, y, w, h, heading, lines, heading_size=30, size=28, heading_color=ACCENT, gap=0.1):
    """卡片：粗體標題加數行內文（每行一個重點，前面不放項目符號）。"""
    deck.card(x, y, w, h)
    deck.text(x + 0.3, y + 0.2, w - 0.6, 0.62, [[(heading, {"bold": True})]], size=heading_size, color=heading_color, name=f"{heading}標題")
    deck.text(x + 0.3, y + 0.95, w - 0.6, h - 1.1, lines, size=size, space_after=gap * 72, name=f"{heading}內容")


def bullet_pairs(deck: Deck, x, y, w, items, head_size=28, sub_size=24, row_h=1.2, dot_d=0.2):
    """一個重點兩行：粗體標題加一行說明，前面是琥珀色小圓點。"""
    for index, (head, sub) in enumerate(items):
        top = y + index * row_h
        deck.shape(MSO_SHAPE.OVAL, x, top + 0.17, dot_d, dot_d, fill=ACCENT, name="重點")
        deck.text(x + 0.45, top, w - 0.45, 0.56, [[(head, {"bold": True})]], size=head_size, name=f"重點{index + 1}標題", margin=(0.02, 0.0))
        deck.text(x + 0.45, top + 0.56, w - 0.45, 0.5, [sub], size=sub_size, name=f"重點{index + 1}說明", margin=(0.02, 0.0))


def bullet_rows(deck: Deck, x, y, w, items, size=28, row_h=0.62, dot_d=0.2):
    """一列一個重點，前面是琥珀色小圓點（不用文字的項目符號）。"""
    for index, item in enumerate(items):
        top = y + index * row_h
        deck.shape(MSO_SHAPE.OVAL, x, top + (row_h - dot_d) / 2 - 0.02, dot_d, dot_d, fill=ACCENT, name="重點")
        deck.text(x + 0.45, top, w - 0.45, row_h, [item], size=size, anchor="middle", name=f"重點{index + 1}", margin=(0.02, 0.0))


# ---- 16 張投影片 ------------------------------------------------------------------------------------------------------

def slide_cover(deck: Deck) -> None:
    deck.add_slide("", None, "這份簡報是 2.0 簡報（2026-11-24 版）的 16 張重點版，給主管與評審；每張只放重點，細節在講者備忘與 2.0 簡報。數字取自 2026-11-24 的程式碼與 git 紀錄。第 12 張說明開發時用到的工具與用途。")
    deck.text(MARGIN_X, 1.55, 6.4, 0.45, [[("PROJECT REPORT 5.0", {"bold": True})]], size=20, color=ACCENT, name="標籤", lint=False)
    deck.text(MARGIN_X, 2.05, 6.4, 1.4, [[("雙核心平台", {"bold": True})]], size=72, name="標題", anchor="middle", margin=(0.0, 0.0))
    deck.text(MARGIN_X, 3.55, 6.4, 1.3, ["開放世界文字 RPG", "× 會員制智能客服"], size=34, name="副標題", margin=(0.0, 0.0))
    deck.text(MARGIN_X, 5.2, 6.4, 0.5, [[("專案狀態報告｜2026-11-24", {"bold": True})]], size=28, name="日期", margin=(0.0, 0.0))
    deck.text(MARGIN_X, 5.8, 6.4, 0.5, ["給主管與評審的 16 張重點版"], size=24, name="說明", margin=(0.0, 0.0))
    # 兩個重疊的圓：雙核心共用同一套基礎
    deck.shape(MSO_SHAPE.OVAL, 7.0, 1.7, 3.9, 3.9, fill=ACCENT, line=ACCENT, line_w=3, alpha=22, name="核心一")
    deck.shape(MSO_SHAPE.OVAL, 8.8, 1.7, 3.9, 3.9, fill=TEXT, line=TEXT, line_w=3, alpha=14, name="核心二")
    deck.text(7.25, 3.25, 1.5, 0.8, [[("RPG", {"bold": True})]], size=36, align="c", anchor="middle", name="RPG")
    deck.text(10.95, 3.25, 1.5, 0.8, [[("客服", {"bold": True})]], size=36, align="c", anchor="middle", name="客服")
    deck.text(8.85, 3.0, 2.0, 1.3, [[("共用", {"bold": True})], [("基礎", {"bold": True})]], size=26, align="c", anchor="middle", name="共用基礎", color=ACCENT)
    deck.text(6.7, 5.85, 6.3, 0.5, ["Flask · Vue 3 · SQL Server · LLM · AWS"], size=24, align="c", name="技術", lint=False)


def slide_overview(deck: Deck) -> None:
    deck.add_slide("02 ｜ OVERVIEW", "一頁看懂：做了什麼、做到哪",
                   "八天內封存 11 個 change（AWS 展示環境、只限滿 18 歲、安全模式開關、錢包頁、劇本審核與目錄、檢舉與匿名保留、視覺小說式遊戲畫面、登入後設定精靈、後台刪除帳號等）。"
                   "15,170 個後端測試案例通過 15,165、略過 5；另有 1,963 個前端測試。103 份主規格是 OpenSpec 規格，每個設計決策都有規格、設計文件與守門測試。"
                   "展示環境最新部署是 2026-11-24 的映像 b63f4e7，資料庫在遷移 0023；已封存的功能都已部署。另有劇情朗讀（rpg-story-tts，瀏覽器內建語音、純前端）已部署、待驗收，尚未封存。")
    tiles = [("2", "個核心產品", "RPG 與客服"), ("35", "項功能已封存", "每項有規格測試"), ("15,170", "個自動化測試", "全數通過或略過"), ("103", "份規格文件", "決策有據可查")]
    width, gap = 2.8, 0.24
    for index, (number, label, sub) in enumerate(tiles):
        x = MARGIN_X + index * (width + gap)
        deck.card(x, 1.8, width, 2.75)
        deck.text(x + 0.15, 1.95, width - 0.3, 0.95, [[(number, {"bold": True})]], size=52, color=ACCENT, name=f"{label}數字", margin=(0.05, 0.0), anchor="middle")
        deck.text(x + 0.15, 2.95, width - 0.3, 0.5, [[(label, {"bold": True})]], size=26, name=f"{label}標籤", margin=(0.05, 0.0))
        deck.text(x + 0.15, 3.5, width - 0.3, 0.95, [sub], size=24, name=f"{label}說明", margin=(0.05, 0.0))
    rows = [(True, "已完成：客服、RPG、遊戲畫面、劇本上架與檢舉"), (True, "已部署：AWS 雪梨展示環境（2026-11-24 版本）"), (False, "待驗收：劇情朗讀｜下一步：串流壓測、法務覆核")]
    for index, (done, text) in enumerate(rows):
        y = 4.9 + index * 0.68
        deck.dot(MARGIN_X + 0.05, y + 0.15, 0.32, done=done)
        deck.text(MARGIN_X + 0.6, y, CONTENT_W - 0.6, 0.62, [text], size=28, anchor="middle", name=f"狀態{index + 1}", margin=(0.02, 0.0))


def slide_dual_core(deck: Deck) -> None:
    deck.add_slide("03 ｜ DUAL CORE", "兩個產品核心",
                   "兩個核心共用同一套帳號、年齡閘門、模型呼叫層與用量控制。客服用 Gemini、非串流結構化輸出；RPG 用 OpenAI 相容端點、SSE 串流與打字機效果。"
                   "客服僅由 AI 提供，不設真人客服；使用者要求真人時，AI 引導留下意見回饋，供管理員檢視。客服的知識範圍 v1 只含平台自身資訊（登入與帳號、會員等級與額度、意見回饋流程）。")
    titled_card(deck, MARGIN_X, 1.8, 5.85, 4.05, "開放世界文字 RPG", ["無窮盡的開放世界劇情", "玩家可自創並分享劇本", "串流輸出，逐句審查", "遊戲幣控管成本"], size=30, gap=0.16)
    titled_card(deck, MARGIN_X + 6.08, 1.8, 5.85, 4.05, "會員制智能客服", ["AI 解析意圖與實體", "回覆經伺服器驗證", "只回答平台自身資訊", "要真人？留下意見回饋"], size=30, gap=0.16)
    deck.shape(MSO_SHAPE.ROUNDED_RECTANGLE, MARGIN_X, 6.0, CONTENT_W, 0.85, fill=OK_BG, line=OK, radius=0.15, name="共用條")
    deck.text(MARGIN_X + 0.3, 6.0, CONTENT_W - 0.6, 0.85, [[("共用同一套：", {"bold": True, "color": ACCENT}), "帳號、年齡閘門、模型呼叫層、用量控制"]], size=28, anchor="middle", name="共用說明")


def slide_journey(deck: Deck) -> None:
    deck.add_slide("04 ｜ PLAYER JOURNEY", "玩家旅程：從登入到創作",
                   "首次 Google 登入即註冊，須勾選「同意服務條款與隱私權政策」與「我確認已滿 18 歲」；回訪者一鍵登入。登入後填生日與年齡區間，伺服器由生日計算年齡，未滿 18 歲立即抹除帳號與個資。"
                   "登入後的 /age 是三步設定精靈（歡迎與使用守則、年齡確認、故事偏好），仍一次送出；故事偏好在年齡確認成功之後才存。"
                   "建立主角時 AI 生成只是預覽，可修改後接受，按「開始冒險」才寫入存檔；開場的第 0 幕不算一輪、不扣幣。遊戲畫面是劇情卡：AI 回應依句分段，點擊或按鍵翻段，並可用瀏覽器內建語音朗讀當前段。")
    steps = [("Google 登入", "首次登入即註冊"), ("設定精靈", "三步：守則、年齡確認、偏好"), ("建立主角", "可請 AI 生成角色卡與錨定事實"),
             ("開始冒險", "劇情卡翻段、可朗讀，每輪 30 幣"), ("創作與分享", "固定規格創作，審核後公開")]
    for index, (title, desc) in enumerate(steps):
        y = 1.8 + index * 1.02
        deck.card(MARGIN_X, y, CONTENT_W, 0.9)
        deck.badge(MARGIN_X + 0.2, y + 0.1, 0.7, str(index + 1), size=28)
        deck.text(MARGIN_X + 1.15, y, 3.6, 0.9, [[(title, {"bold": True})]], size=32, anchor="middle", name=f"步驟{index + 1}", margin=(0.02, 0.0))
        deck.text(MARGIN_X + 4.85, y, CONTENT_W - 5.0, 0.9, [desc], size=26, anchor="middle", name=f"步驟{index + 1}說明", margin=(0.02, 0.0))


def slide_rpg(deck: Deck) -> None:
    deck.add_slide("05 ｜ RPG", "RPG：無窮盡的開放世界",
                   "平台預設與首發劇本沒有硬性結局；玩家創作的劇本可選擇自訂結局，到結局時存檔進入「已完結」。里程碑由 Service 依玩家年齡或遊戲內年份判斷，不由模型宣告。"
                   "一輪 = 一則玩家訊息 + 一則 AI 回應；送出行動先過規則層，預扣 30 幣，串流期間沒有任何資料庫交易，輸出經 OutputGuard 逐句審查（保留一句緩衝）才釋放，完成後在同一個短交易結算。"
                   "記事簿每完成 10 輪由 RPG 模型從最近 10 輪抓重點追加，容量 2000／5000／10000 字依會員等級；玩家可檢視與編輯。"
                   "遊戲畫面（rpg-story-view）：AI 回應依句分段成劇情卡，逐段打字機、點擊或按鍵翻段，「‹」只能在當前輪回上一段；另有只讀的歷史視圖與純 CSS 氛圍背景。"
                   "行動建議點燈泡圖示才產生（獨立的非串流呼叫、至多 3 個、每個畫面最多一次、不扣遊戲幣但計入全站 token 上限），另有「直接推進劇情」（一輪 30 幣）。分段只是前端呈現，不改串流協定、審查與扣幣。"
                   "劇情朗讀（rpg-story-tts，已部署、待驗收）：劇情卡旁的語音按鈕與選單，用瀏覽器內建語音朗讀當前段，純前端、不經伺服器、沒有新端點與遷移；第一次先選語音（可試聽，標示本機或線上），選擇只存在瀏覽器；翻段、重新生成、送出行動與離開頁面即停止，不自動接讀。")
    points = [("無窮盡劇情", "沒有死局，作者可自訂結局"), ("劇情卡閱讀", "依句分段，點擊或按鍵翻段"), ("劇情朗讀", "瀏覽器內建語音，不經伺服器"), ("行動建議", "點圖示才產生，不扣遊戲幣"), ("逐句審查", "通過才顯示給玩家")]
    for index, (head, sub) in enumerate(points):
        y = 1.75 + index * 1.02
        deck.badge(MARGIN_X, y + 0.06, 0.5, str(index + 1), size=24)
        deck.text(MARGIN_X + 0.8, y, 6.7, 0.5, [[(head, {"bold": True})]], size=28, name=f"{head}標題", margin=(0.02, 0.0))
        deck.text(MARGIN_X + 0.8, y + 0.49, 6.7, 0.46, [sub], size=24, name=f"{head}說明", margin=(0.02, 0.0))
    flow = ["送出行動", "預扣 30 幣", "串流＋逐句審查", "結算並存檔", "每 10 輪：記事簿"]
    x, w, h, gap = 8.55, 4.08, 0.72, 0.24
    deck.text(x, 1.45, w, 0.4, [[("一輪的流程", {"bold": True})]], size=24, color=ACCENT, name="流程標題", lint=False)
    for index, label in enumerate(flow):
        y = 1.95 + index * (h + gap)
        deck.shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h, fill=PANEL, line=ACCENT if index == 2 else LINE, line_w=1.5, radius=0.18, name=f"流程{index + 1}")
        deck.text(x + 0.1, y, w - 0.2, h, [[(label, {"bold": index == 2})]], size=26, align="c", anchor="middle", name=f"流程{index + 1}文字", margin=(0.02, 0.0))
        if index < len(flow) - 1:
            deck.arrow(x + w / 2, y + h + 0.02, x + w / 2, y + h + gap - 0.02)


def slide_cs(deck: Deck) -> None:
    deck.add_slide("06 ｜ CUSTOMER SERVICE", "客服：可驗證的結構化回覆",
                   "每次呼叫都以非串流請求強制輸出 JSON：intent、confidence、entities、reply_text、suggested_actions；信心低於 0.6 視為 unknown 並引導以意見回饋留下紀錄。"
                   "伺服器重新驗證，不信任模型輸出；嚴禁把 JSON 原文串流給前端。單次呼叫 20 秒逾時就切到備援模型，後端單次請求總預算 30 秒；失敗原文截斷 2000 字、保存 14 天、僅管理員可讀。")
    flow = ["使用者訊息", "AI 解析\n意圖與實體", "伺服器驗證", "回覆＋\n建議按鈕"]
    w, gap, y, h = 2.65, 0.44, 1.8, 1.75
    for index, label in enumerate(flow):
        x = MARGIN_X + index * (w + gap)
        deck.shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h, fill=PANEL, line=ACCENT if index == 2 else LINE, line_w=1.5, radius=0.1, name=f"流程{index + 1}")
        deck.badge(x + 0.15, y + 0.15, 0.5, str(index + 1), size=20)
        deck.text(x + 0.12, y + 0.7, w - 0.24, 1.0, [[(part, {"bold": True})] for part in label.split("\n")], size=26, name=f"流程{index + 1}文字", margin=(0.03, 0.0), anchor="middle")
        if index < len(flow) - 1:
            deck.arrow(x + w + 0.03, y + h / 2, x + w + gap - 0.03, y + h / 2)
    titled_card(deck, MARGIN_X, 3.85, 5.85, 3.0, "失敗時怎麼辦", ["20 秒逾時或 5xx：換備援", "驗證失敗：同模型重試", "仍失敗：繁體中文降級文案"], gap=0.06)
    titled_card(deck, MARGIN_X + 6.08, 3.85, 5.85, 3.0, "不信任模型", ["強制 JSON 輸出格式", "伺服器重新驗證", "日誌不記對話全文"], gap=0.06)


def slide_safety(deck: Deck) -> None:
    deck.add_slide("07 ｜ SAFETY", "內容安全：六道防線",
                   "六道防線都在伺服器端執行，不依賴模型自行判斷使用者年齡，也不依賴模型拒絕；使用不具拒絕行為的模型時，伺服器端的審查層是必要條件。"
                   "安全模式開（預設）時不得出現性行為與性暗示、露骨暴力血腥與自殺方法描寫；安全模式關閉須同意安全規範並記錄版本，部署層另有緊急煞車 SAFE_MODE_FORCED 可全站停用。"
                   "無論模式，一律禁止涉及未成年的性內容、性暴力與非自願情節，以及以真人名義的性內容；判定依年齡紀錄與文字的明確線索，沒有明確年齡的角色不因此被擋。")
    items = [("帳號閘門", ["只限滿 18 歲", "未滿即抹除"]), ("生效模式", ["取最保守者", "預設安全模式"]), ("輸入規則層", ["不呼叫模型", "預扣前就拒絕"]),
             ("創作審查", ["劇本與主角", "規則層加 AI"]), ("輸出審查", ["逐句檢查", "通過才顯示"]), ("被擋之後", ["轉場、重生一次", "退回預扣"])]
    w, h, gap_x, gap_y = 3.8, 2.05, 0.267, 0.17
    for index, (head, desc) in enumerate(items):
        x = MARGIN_X + (index % 3) * (w + gap_x)
        y = 1.8 + (index // 3) * (h + gap_y)
        deck.card(x, y, w, h)
        deck.badge(x + 0.2, y + 0.2, 0.55, str(index + 1), size=24)
        deck.text(x + 0.9, y + 0.2, w - 1.1, 0.55, [[(head, {"bold": True})]], size=28, anchor="middle", name=f"防線{index + 1}", margin=(0.02, 0.0))
        deck.text(x + 0.25, y + 0.92, w - 0.5, 1.05, desc, size=26, name=f"防線{index + 1}說明", margin=(0.03, 0.0))
    deck.text(MARGIN_X, 6.3, CONTENT_W, 0.55, [[("無論哪種模式", {"bold": True, "color": ACCENT}), "，一律禁止涉及未成年的性內容與性暴力；不依賴模型拒絕"]], size=24, name="底線說明", margin=(0.02, 0.0))


def slide_creators(deck: Deck) -> None:
    deck.add_slide("08 ｜ CREATORS", "創作者生態：先審後發",
                   "劇本屬於作者，公開分享採先審後發：每個送審的劇本須管理員核可才上架；已發佈的版本不可變，修改產生新版本；退回只告知類別，審核備註只有管理員看得到。"
                   "檢舉掛在上架的版本，每人每版本一次、每人每日 5 次，只升高審核佇列的優先度（涉及未成年性內容等紅線理由排最前），不自動下架；版本下架或被新版本取代時未結案的檢舉由系統自動結案。"
                   "帳號偏好「匿名保留我已發佈的劇本」預設關閉；開啟後因申請刪除或閒置 12 個月被抹除時，目前上架的版本以「已註銷的作者」保留，其餘全刪。"
                   "審核與目錄（0021）、檢舉與匿名保留（0022）都已部署到展示環境。管理員可在後台立即刪除一個玩家帳號（須重新輸入密碼、寫稽核）；這種刪除不套用匿名保留，劇本全刪。")
    cards = [("創作與審查", "已部署", OK, ["固定規格創作", "規則層＋AI 審查", "連續被拒暫停發佈"]),
             ("上架與目錄", "已部署", OK, ["管理員核可才上架", "公開目錄與詳情頁", "已發佈版本不可變"]),
             ("檢舉與下架", "已部署", OK, ["玩家可檢舉", "不自動下架", "管理員決定下架"])]
    w, gap = 3.8, 0.267
    for index, (head, status, color, lines) in enumerate(cards):
        x = MARGIN_X + index * (w + gap)
        deck.card(x, 1.8, w, 4.2)
        deck.text(x + 0.25, 1.95, w - 0.5, 0.6, [[(head, {"bold": True})]], size=30, color=ACCENT, name=f"{head}標題")
        deck.shape(MSO_SHAPE.ROUNDED_RECTANGLE, x + 0.25, 2.65, 2.55, 0.5, fill=None, line=color, line_w=2, radius=0.5, name=f"{head}狀態框")
        deck.text(x + 0.25, 2.65, 2.55, 0.5, [[(status, {"bold": True})]], size=24, color=color if color == ACCENT else OK, align="c", anchor="middle", name=f"{head}狀態", margin=(0.02, 0.0), lint=False)
        deck.text(x + 0.25, 3.35, w - 0.5, 2.6, lines, size=28, space_after=12, name=f"{head}內容")
    deck.text(MARGIN_X, 6.2, CONTENT_W, 0.6, [[("匿名保留：", {"bold": True, "color": ACCENT}), "刪除帳號時，上架作品改署名「已註銷的作者」，其餘全刪"]], size=26, name="匿名保留說明", margin=(0.02, 0.0))


def slide_coins(deck: Deck) -> None:
    deck.add_slide("09 ｜ COINS & TIERS", "遊戲幣與會員等級",
                   "遊戲幣：一輪 30 幣，失敗、中斷、被擋時不扣；首次註冊登入送 200（年齡確認通過時發放）、每日簽到 200（台灣時間 0 點刷新）、免費幣上限 1000；先扣免費幣再扣付費幣。"
                   "管理員帳號不消耗遊戲幣，供營運測試。儲值分頁目前只是示意，真實儲值需要先探索金流。客服使用每日對話輪數與每分鐘訊息數，RPG 改以遊戲幣控制；會員等級只影響額度，不影響權限。")
    stats = [("30", "幣／輪，失敗不扣"), ("200", "首登贈幣"), ("200", "每日簽到，免費幣上限 1,000"), ("0", "管理員帳號不扣幣")]
    for index, (number, label) in enumerate(stats):
        y = 1.8 + index * 1.2
        deck.card(MARGIN_X, y, 5.1, 1.08)
        deck.text(MARGIN_X + 0.15, y, 1.55, 1.08, [[(number, {"bold": True})]], size=44, color=ACCENT, anchor="middle", name=f"數字{index + 1}", margin=(0.03, 0.0))
        deck.text(MARGIN_X + 1.75, y, 3.25, 1.08, [label], size=24, anchor="middle", name=f"說明{index + 1}", margin=(0.03, 0.0))
    rows = [["", "free", "plus", "vip"], ["客服每日輪數", "30", "100", "300"], ["客服對話數", "20", "100", "500"], ["記事簿字數", "2,000", "5,000", "10,000"]]
    deck.table(6.1, 1.8, [2.4, 1.38, 1.38, 1.38], 0.82, rows)
    deck.text(6.1, 5.3, 6.54, 1.6, [[("等級只影響額度，不影響權限", {"bold": True, "color": ACCENT})], "RPG 不用每日輪數，改用遊戲幣控制"], size=26, space_after=8, name="等級說明")


def slide_privacy(deck: Deck) -> None:
    deck.add_slide("10 ｜ PRIVACY", "隱私與合規",
                   "生日與年齡區間不傳送至模型，管理員後台只提供年齡區間的彙總統計；未滿 18 歲、逾時未完成年齡確認（24 小時）與連續 12 個月未登入的帳號直接抹除並寫入 deletion_tombstones。"
                   "日誌是結構化 JSON，不記密碼、cookie、授權碼、信箱、生日、使用者訊息全文、模型原始輸出與劇本全文。使用者主動刪除帳號先停用、7 天寬限可撤銷，期滿由排程抹除。"
                   "管理員可在後台立即刪除一個玩家帳號（須重新輸入密碼、寫稽核，不經寬限期，劇本全刪），服務條款第 4 節已載明。"
                   "隱私權政策與服務條款（版本 2026-11-24-draft-11）仍標示草稿，法務覆核尚未完成。")
    cards = [("只限滿 18 歲", ["未滿 18 歲立即抹除", "帳號與全部個資"]), ("個資最小化", ["日誌不記信箱、訊息", "全文與 cookie"]),
             ("刪除與保存", ["自行刪除有 7 天寬限", "管理員刪除則立即抹除"]), ("公開揭露", ["條款說明內容經 AI 處理", "法務覆核尚待進行"])]
    w, h, gap = 5.85, 2.35, 0.233
    for index, (head, lines) in enumerate(cards):
        x = MARGIN_X + (index % 2) * (w + gap)
        y = 1.8 + (index // 2) * (h + 0.2)
        titled_card(deck, x, y, w, h, head, lines, gap=0.02)


def slide_foundation(deck: Deck) -> None:
    deck.add_slide("11 ｜ FOUNDATION", "技術底座：分層與雙資料庫",
                   "後端四層：MVC Page Controller 只回 HTML 外殼，Web API Controller 只回 JSON，Service 放業務邏輯與 AccessPolicy，Repository 唯一碰 ORM；依賴方向與禁止匯入的規則由 AST 測試守住。"
                   "前端是 Vue 3 全域變數模式（不用 .vue 與打包）、Bootstrap 5.3 只用排版，所有資產與字型自行託管，不使用 CDN。資料庫以 SQLAlchemy ORM 同時支援 SQLite 與 SQL Server，禁止 Raw SQL；遷移用 Alembic，是部署的獨立步驟。")
    layers = [("Controller", "頁面只回 HTML、API 只回 JSON"), ("Service", "業務邏輯、權限判斷"), ("Repository", "唯一碰資料庫的一層"), ("Model", "SQLAlchemy 資料模型")]
    w, h, gap = 6.1, 1.1, 0.18
    for index, (head, desc) in enumerate(layers):
        y = 1.8 + index * (h + gap)
        deck.card(MARGIN_X, y, w, h, line=ACCENT if index == 1 else LINE)
        deck.text(MARGIN_X + 0.25, y + 0.08, w - 0.5, 0.52, [[(head, {"bold": True})]], size=28, color=ACCENT, name=f"{head}", margin=(0.02, 0.0))
        deck.text(MARGIN_X + 0.25, y + 0.58, w - 0.5, 0.45, [desc], size=24, name=f"{head}說明", margin=(0.02, 0.0))
        if index < len(layers) - 1:
            deck.arrow(MARGIN_X + w / 2, y + h + 0.01, MARGIN_X + w / 2, y + h + gap - 0.01)
    bullet_pairs(deck, 7.45, 1.9, 5.18, [("離線前端", "Vue 3，不用 CDN"), ("雙資料庫", "SQLite 與 SQL Server"), ("只走 ORM", "禁止 Raw SQL"), ("自動測試", "守住分層規則")])


def slide_tools(deck: Deck) -> None:
    deck.add_slide("12 ｜ TOOLS", "開發工具：用了什麼、做了什麼",
                   "Claude Code（VS Code 擴充）：AI 協作開發，依專案規範（AGENTS.md）寫程式、測試與文件；專案技能固定流程（OpenSpec 各階段、簡報衝突檢查、設計文件同步）。"
                   "OpenSpec：規格驅動，每個功能依 explore、propose、apply、archive 進行，35 個 change 已封存、103 份主規格；每批做完更新交接文件並由使用者下指令才 commit，Git 與 GitHub 保存歷程。"
                   "測試：pytest 15,170 案例（SQLite 與 SQL Server 雙軌）、Node 內建測試 1,963 案例、AST 架構測試；變異驗證是在專案副本上刻意改壞程式，確認測試抓得到（admin-delete-account 的 39 個變異全數被抓到；劇情朗讀的狀態機 61 個變異殺死 60，剩 1 個等價變異）。"
                   "Playwright 與無頭 Chrome：瀏覽器實機驗收（手機與桌機寬度），另以 Chrome DevTools Protocol 擷取畫面、配離線旁白錄製 17 支功能示範影片。"
                   "AWS CLI、SSM、Docker：自寫的 scripts/aws_demo.py 依序做 RDS 快照、在主機上建置映像、遷移、換版與驗證，部署到 AWS 雪梨展示環境。"
                   "Mermaid、Chrome、python-pptx：產生 6 張 4K 架構圖解、2.0 簡報 PDF 與 PowerPoint（2.1、4.0、5.0）。這些簡報用的工具只在產生文件時使用，不進版本庫與應用程式。")
    cards = [("Claude Code", ["依專案規範寫程式與文件", "用技能固定開發流程"]),
             ("OpenSpec", ["先寫規格再實作功能", "35 個 change、103 份規格"]),
             ("測試與變異驗證", ["15,170 後端、1,963 前端", "封存前刻意改壞程式驗證"]),
             ("Playwright", ["瀏覽器實機驗收", "手機與桌機寬度各檢查"]),
             ("AWS CLI＋SSM", ["自寫腳本 aws_demo.py 部署", "快照→建置→遷移→換版→驗證"]),
             ("Mermaid＋python-pptx", ["產生 6 張 4K 架構圖解", "與 PDF、PowerPoint 簡報"])]
    w, h, gap_x, gap_y = 5.85, 1.66, 0.233, 0.12
    for index, (head, lines) in enumerate(cards):
        x = MARGIN_X + (index % 2) * (w + gap_x)
        y = 1.72 + (index // 2) * (h + gap_y)
        deck.card(x, y, w, h)
        deck.badge(x + 0.2, y + 0.16, 0.5, str(index + 1), size=20)
        deck.text(x + 0.85, y + 0.12, w - 1.05, 0.58, [[(head, {"bold": True})]], size=28, color=ACCENT, anchor="middle", name=f"工具{index + 1}", margin=(0.02, 0.0))
        deck.text(x + 0.25, y + 0.72, w - 0.5, 0.9, lines, size=24, name=f"工具{index + 1}用途", margin=(0.03, 0.0))


def slide_quality(deck: Deck) -> None:
    deck.add_slide("13 ｜ QUALITY", "品質保證：測試與規格守門",
                   "pytest 收集 15,170 案例（通過 15,165、略過 5）；SQLite 與 SQL Server 雙軌。Node 內建測試 1,963 案例全數通過。tests/architecture 以 AST 檢查分層、後台邊界、無 Raw SQL 與前端規則；"
                   "test_design_docs 與 test_project_db_schema_doc 確認資料表、欄位、路由、migration 與文件逐項一致。破壞驗證：封存前刻意改壞程式，確認至少一項測試會失敗。OpenSpec 規格以 validate --strict 驗證後才封存。")
    tiles = [("15,170", "後端測試案例"), ("1,963", "前端測試案例"), ("103", "份主規格")]
    w, gap = 3.8, 0.267
    for index, (number, label) in enumerate(tiles):
        x = MARGIN_X + index * (w + gap)
        deck.card(x, 1.8, w, 2.2)
        deck.text(x + 0.2, 1.95, w - 0.4, 1.1, [[(number, {"bold": True})]], size=60, color=ACCENT, name=f"{label}數字", anchor="middle", margin=(0.05, 0.0))
        deck.text(x + 0.2, 3.1, w - 0.4, 0.6, [[(label, {"bold": True})]], size=28, name=f"{label}標籤", margin=(0.05, 0.0))
    bullet_rows(deck, MARGIN_X, 4.4, CONTENT_W, ["架構測試守住分層、禁用 Raw SQL", "文件與程式逐項比對，不一致就失敗", "封存前破壞驗證：刻意改壞程式，確認測試抓得到"], size=28, row_h=0.78)


def slide_deploy(deck: Deck) -> None:
    deck.add_slide("14 ｜ DEPLOYMENT", "部署與營運：AWS 展示環境",
                   "展示環境是單機（EC2 t3.small，雪梨）加 RDS SQL Server Express，不是 production：沒有可計費的 Google Cloud 專案，所以展示改用 AWS（Free plan 抵用金，展示期到 2026-12-31），"
                   "目的是讓客服池與 RPG 串流池第一次真正分流。部署順序固定：快照 → 建置映像 → 遷移（獨立步驟）→ 換版重啟 → 驗證；回復只換回前一版映像，不降級資料庫。"
                   "最新部署是 2026-11-24 的映像 b63f4e7，資料庫在遷移 0023（回復點是前一版映像 bb665dd，不降級資料庫）；檢舉與匿名保留（0022）、行動建議（0023）都已部署；劇情朗讀（純前端）隨 b63f4e7 部署，沒有遷移。")
    boxes = [(0.7, 2.0, "瀏覽器", "使用者"), (3.35, 3.0, "Caddy", "HTTPS、HTTP/2")]
    for x, w, head, sub in boxes:
        deck.shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, 1.8, w, 1.7, fill=PANEL, line=LINE, line_w=1.5, radius=0.08, name=head)
        deck.text(x + 0.1, 1.95, w - 0.2, 0.6, [[(head, {"bold": True})]], size=28, align="c", name=f"{head}標題", margin=(0.02, 0.0))
        deck.text(x + 0.1, 2.6, w - 0.2, 0.7, [sub], size=24, align="c", name=f"{head}說明", margin=(0.02, 0.0))
    for index, label in enumerate(["客服池", "RPG 串流池"]):
        y = 1.8 + index * 0.9
        deck.shape(MSO_SHAPE.ROUNDED_RECTANGLE, 6.95, y, 2.75, 0.8, fill=PANEL, line=ACCENT, line_w=1.5, radius=0.15, name=label)
        deck.text(7.0, y, 2.65, 0.8, [[(label, {"bold": True})]], size=26, align="c", anchor="middle", name=f"{label}文字", margin=(0.02, 0.0))
    deck.shape(MSO_SHAPE.ROUNDED_RECTANGLE, 10.3, 1.8, 2.33, 1.7, fill=PANEL, line=LINE, line_w=1.5, radius=0.08, name="資料庫")
    deck.text(10.35, 1.95, 2.23, 0.6, [[("RDS", {"bold": True})]], size=28, align="c", name="資料庫標題", margin=(0.02, 0.0))
    deck.text(10.35, 2.6, 2.23, 0.7, ["SQL Server"], size=24, align="c", name="資料庫說明", margin=(0.02, 0.0))
    deck.arrow(2.73, 2.65, 3.32, 2.65)
    deck.arrow(6.38, 2.2, 6.92, 2.2)
    deck.arrow(6.38, 3.1, 6.92, 3.1)
    deck.arrow(9.73, 2.65, 10.27, 2.65)
    bullet_rows(deck, MARGIN_X, 3.95, CONTENT_W, ["單機展示環境，不是 production", "最新部署：2026-11-24（遷移 0023）", "部署順序：快照 → 建置 → 遷移 → 換版 → 驗證", "production 規劃：Google Cloud（尚未建立）"], size=28, row_h=0.72)


def slide_roadmap(deck: Deck) -> None:
    deck.add_slide("15 ｜ ROADMAP", "路線圖：已完成與下一步",
                   "八天內從 1.0 走到現在：AWS 展示環境、只限滿 18 歲、安全模式開關、錢包頁、劇本審核與目錄、檢舉與匿名保留，以及視覺小說式遊戲畫面、設定精靈與後台刪除帳號，都已封存並部署；劇情朗讀（rpg-story-tts）已部署、待使用者驗收，驗收通過才封存。下一步做串流壓測與監控（rpg-ops-loadtest），"
                   "最後是雲端預備與 production（需要可計費的 Google Cloud 專案，cloud-staging-run 擱置中）。另有玩家個人頁、性格原型庫與外部資料閘道等規劃中的項目。")
    deck.text(MARGIN_X, 1.75, 4.0, 0.5, [[("已完成（35 項功能）", {"bold": True})]], size=26, color=OK, name="已完成標題", margin=(0.02, 0.0))
    done = ["基礎與客服", "RPG 核心\n與遊戲畫面", "AWS\n展示環境", "18 歲與\n安全模式", "上架、檢舉\n與刪帳號"]
    w, gap = 2.2, 0.233
    for index, label in enumerate(done):
        x = MARGIN_X + index * (w + gap)
        deck.shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, 2.3, w, 1.3, fill=OK_BG, line=OK, line_w=1.5, radius=0.1, name=f"已完成{index + 1}")
        deck.text(x + 0.1, 2.3, w - 0.2, 1.3, [[(part, {"bold": True})] for part in label.split("\n")], size=26, align="c", anchor="middle", name=f"已完成{index + 1}文字", margin=(0.02, 0.0))
    deck.text(MARGIN_X, 3.95, 6.0, 0.5, [[("待驗收與下一步", {"bold": True})]], size=26, color=ACCENT, name="下一步標題", margin=(0.02, 0.0))
    nxt = ["劇情朗讀\n（待驗收）", "串流壓測\n與監控", "完成法務\n覆核", "雲端預備與\nproduction"]
    w2, gap2 = 2.8, 0.24
    for index, label in enumerate(nxt):
        x = MARGIN_X + index * (w2 + gap2)
        deck.shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, 4.5, w2, 1.3, fill=PANEL, line=ACCENT, line_w=2, radius=0.1, name=f"下一步{index + 1}")
        deck.text(x + 0.15, 4.5, w2 - 0.3, 1.3, [[(part, {"bold": True})] for part in label.split("\n")], size=26, align="c", anchor="middle", name=f"下一步{index + 1}文字", margin=(0.02, 0.0))
        if index < len(nxt) - 1:
            deck.arrow(x + w2 + 0.02, 5.15, x + w2 + gap2 - 0.02, 5.15)
    deck.text(MARGIN_X, 6.1, CONTENT_W, 0.7, ["另有玩家個人頁、性格原型庫、外部資料閘道等規劃中"], size=24, name="其他規劃", margin=(0.02, 0.0))


def slide_next(deck: Deck) -> None:
    deck.add_slide("16 ｜ NEXT", "限制與下一步",
                   "限制都已列入路線圖與設計文件：展示環境單機單點、數字不是容量依據；RPG 目前用免費模型並已設定備援，正式對外前須重新評估供應商與方案，免費模型不得輸入真實個資；"
                   "金流與訂閱尚待探索，儲值畫面只是示意；條款、隱私權政策、內容規範與檢舉處理列入法務覆核。入口層 IP 限流尚未做，是上線前關卡。"
                   "劇情朗讀已部署、待驗收：Windows Chrome 以腳本實機檢查 48／48（1280 與 360 寬），Edge、線上語音（約 15 秒中斷的風險）與真實手機觸控尚未實測；驗收通過才封存。")
    titled_card(deck, MARGIN_X, 1.8, 5.85, 4.1, "已知限制", ["展示環境單機單點", "RPG 暫用免費模型", "金流與訂閱待探索", "法務覆核尚未完成"], size=30, gap=0.12)
    titled_card(deck, MARGIN_X + 6.08, 1.8, 5.85, 4.1, "下一步", ["劇情朗讀驗收", "串流壓測與監控", "補齊展示環境缺口", "完成法務覆核", "建立 production"], size=28, gap=0.06, heading_color=OK)
    deck.shape(MSO_SHAPE.ROUNDED_RECTANGLE, MARGIN_X, 6.05, CONTENT_W, 0.85, fill=OK_BG, line=OK, radius=0.15, name="結語條")
    deck.text(MARGIN_X + 0.3, 6.05, CONTENT_W - 0.6, 0.85, [[("兩個核心共用同一套可驗證的基礎", {"bold": True})]], size=32, anchor="middle", name="結語")


SLIDES = [slide_cover, slide_overview, slide_dual_core, slide_journey, slide_rpg, slide_cs, slide_safety, slide_creators, slide_coins,
          slide_privacy, slide_foundation, slide_tools, slide_quality, slide_deploy, slide_roadmap, slide_next]
assert len(SLIDES) == TOTAL


# ---- 檢查與主程式 -----------------------------------------------------------------------------------------------------

def check_facts() -> list[str]:
    source = SOURCE_MD.read_bytes().decode("utf-8-sig")
    return [f"2.0簡報.md 找不到「{fact}」（5.0 用到這個數字或說法，請先對齊）" for fact in FACTS if fact not in source]


def build() -> Deck:
    LINT.clear()
    deck = Deck()
    for make in SLIDES:
        make(deck)
    return deck


def main() -> int:
    parser = argparse.ArgumentParser(description="產生 docs/5.0簡報.pptx（16 張重點版）")
    parser.add_argument("--pptx-libs", help="python-pptx 所在資料夾（也可用環境變數 PPTX_LIBS）")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="只檢查版面估算與數字，不寫檔")
    args = parser.parse_args()

    deck = build()
    problems = check_facts()
    for line in problems + LINT:
        print("問題：" + line)
    if args.check:
        print(f"共 {deck.number} 頁；數字檢查 {'失敗' if problems else '通過'}；版面估算問題 {len(LINT)} 處")
        return 3 if (problems or LINT) else 0
    deck.save(args.output)
    print(f"已輸出 {args.output}（{deck.number} 頁；版面估算問題 {len(LINT)} 處；數字問題 {len(problems)} 處）")
    return 3 if (problems or LINT) else 0


if __name__ == "__main__":
    sys.exit(main())
