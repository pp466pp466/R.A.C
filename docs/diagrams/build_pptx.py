"""把 docs/2.0簡報.md 轉成 16:9 的 PowerPoint（docs/2.1簡報.pptx）。

用法（專案根目錄）：
    python docs/diagrams/build_pptx.py --libs <資料夾> --pptx-libs <資料夾>
    python docs/diagrams/build_pptx.py --libs <資料夾> --pptx-libs <資料夾> --label 2.1 --output docs/2.1簡報.pptx

內容與 PDF（build_deck.py）是**同一份** Markdown：
- 解析（分頁、版面指令、左右欄）直接匯入 build_deck.py 的函式，不另寫一份；
- 6 張 4K 架構圖解用原本的 PNG（含 crop 裁切），Mermaid 圖用**與 PDF 相同的渲染結果**（同一份 HTML、同一個 mermaid 與主題），
  由 Chrome 把每張圖的 SVG 截成 PNG 放進投影片；
- 其餘（標題、段落、項目符號、表格）是 PowerPoint 的原生元素，可以編輯；色彩與字級沿用簡報 CSS（Cinematic Dark Minimalist，沒有純白）。
- 字型用 Microsoft JhengHei（簡報的後備字型）。Noto Sans TC 是網頁用的自託管切片，無法內嵌在 pptx 裡。

兩個工具資料夾都是**只在產生文件時用到**的工具，不屬於應用程式，不放進版本庫與 requirements：
    npm install --prefix <資料夾> mermaid@11.4.1 marked@12.0.2                 （同 build_deck.py，--libs）
    pip install python-pptx --target <資料夾>                                  （--pptx-libs，或環境變數 PPTX_LIBS）

版面是以字數估算高度後排進固定的 1280 x 720 版面；估算放不下時會逐步縮小字級（最小 0.62 倍），仍放不下的頁面會列在輸出裡，
結束代碼為 3（檔案仍會寫出）。**有版面問題時不要自己改版面，先回報**。輸出會列出每一頁最後採用的字級倍率。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from build_deck import parse_slide, render_html, split_slides
from render_tools import DOCS_DIR, FONTS_CSS, PALETTE, RenderError, file_url, html_dump_dom, html_to_png, read_text, write_text

P = PALETTE
SOURCE = DOCS_DIR / "2.0簡報.md"
OUTPUT = DOCS_DIR / "2.1簡報.pptx"

W_PX, H_PX = 1280, 720                  # 版面（與 PDF 相同）；1 px = 9525 EMU，1 px = 0.75 pt
EMU = 9525
FONT = "Microsoft JhengHei"
MONO = "Consolas"
MIN_SCALE = 0.62
BODY_L, BODY_R, BODY_T, BODY_B = 56, 56, 104, 50
IMAGE_GAP = 10                         # 圖片後的間距（px）
PANEL_EVEN = "16181B"                   # rgba(28,30,35,.55) 疊在 #0F1012 上的結果

FENCE = re.compile(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_-]*)")
SWATCH = re.compile(r'<span class="sw" style="background:(#[0-9A-Fa-f]{6})"></span>')
INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|<span class=\"sw\"[^>]*></span>)")
IMAGE = re.compile(r'^!\[(.*?)\]\((.*?)(?:\s+"(.*?)")?\)\s*$')


# ---- 內容模型 -------------------------------------------------------------------------------------------------------

@dataclass
class Run:
    text: str
    bold: bool = False
    code: bool = False
    color: str | None = None


@dataclass
class Block:
    kind: str                                   # h1 h2 h3 p ul quote table mermaid image code
    runs: list[list[Run]] = field(default_factory=list)      # p／h：一個段落；ul：每個項目一個段落
    rows: list[list[list[Run]]] = field(default_factory=list)  # table：第 0 列是表頭
    ref: str = ""                               # mermaid：圖的序號；image：路徑
    alt: str = ""
    crop: tuple[float, float] | None = None


def parse_inline(text: str, bold: bool = False) -> list[Run]:
    runs: list[Run] = []
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            runs.extend(parse_inline(part[2:-2], True))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            runs.append(Run(part[1:-1], bold, True))
        elif SWATCH.fullmatch(part):
            runs.append(Run("■ ", False, False, SWATCH.fullmatch(part).group(1).lstrip("#").upper()))
        else:
            runs.append(Run(part.replace("\\|", "|").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"), bold))
    return runs


def split_row(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def parse_blocks(markdown: str, counter: list[int]) -> list[Block]:
    """一欄的 Markdown 轉成區塊。`counter` 是全簡報共用的 Mermaid 序號（與 build_deck 的 DOM 順序相同）。"""
    blocks: list[Block] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        fence = FENCE.match(line)
        if fence:
            language, body = fence.group(2), []
            i += 1
            while i < len(lines) and not FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1
            if language == "mermaid":
                blocks.append(Block("mermaid", ref=str(counter[0])))
                counter[0] += 1
            else:
                blocks.append(Block("code", runs=[[Run("\n".join(body), code=True)]]))
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            blocks.append(Block(f"h{len(heading.group(1))}", runs=[parse_inline(heading.group(2))]))
            i += 1
            continue
        image = IMAGE.match(stripped)
        if image:
            crop = re.search(r"crop=([0-9.]+)-([0-9.]+)", image.group(3) or "")
            blocks.append(Block("image", ref=image.group(2), alt=image.group(1), crop=(float(crop.group(1)), float(crop.group(2))) if crop else None))
            i += 1
            continue
        if stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = split_row(lines[i])
                if not all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
                    rows.append([parse_inline(cell) for cell in cells])
                i += 1
            blocks.append(Block("table", rows=rows))
            continue
        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(parse_inline(re.sub(r"^\s*[-*]\s+", "", lines[i])))
                i += 1
            blocks.append(Block("ul", runs=items))
            continue
        if stripped.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(Block("quote", runs=[parse_inline(" ".join(quote))]))
            continue
        paragraph = []
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,3}\s|[-*]\s|>|\||!\[|```|~~~)", lines[i].strip()):
            paragraph.append(lines[i].strip())
            i += 1
        blocks.append(Block("p", runs=[parse_inline(" ".join(paragraph))]))
    return blocks


# ---- 文字寬度與高度的估算 ---------------------------------------------------------------------------------------------

def char_em(ch: str, code: bool = False) -> float:
    o = ord(ch)
    if o >= 0x2E80 or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF or 0x2190 <= o <= 0x25FF:
        return 1.0
    if ch == " ":
        return 0.32
    if code:
        return 0.58
    if ch.isupper() or ch.isdigit():
        return 0.62
    return 0.54


def runs_em(runs: list[Run]) -> float:
    return sum(char_em(ch, run.code) * (1.04 if run.bold else 1.0) for run in runs for ch in run.text)


def lines_needed(runs: list[Run], size_px: float, width_px: float) -> int:
    """估算折行數：整段文字的寬度除以可用寬度，再留一點行尾斷詞的餘裕。"""
    if width_px <= 0:
        return 99
    total = 0
    for chunk in split_on_newlines(runs):
        total += max(1, math.ceil(runs_em(chunk) * size_px * 1.05 / width_px))
    return total


def split_on_newlines(runs: list[Run]) -> list[list[Run]]:
    chunks: list[list[Run]] = [[]]
    for run in runs:
        pieces = run.text.split("\n")
        for index, piece in enumerate(pieces):
            if index:
                chunks.append([])
            if piece:
                chunks[-1].append(Run(piece, run.bold, run.code, run.color))
    return chunks


# ---- 版面 ---------------------------------------------------------------------------------------------------------------

@dataclass
class Style:
    size: float                 # 字級（px，已乘上縮放倍率之前）
    line: float                 # 行高倍數
    gap: float                  # 段後間距（px）
    indent: float = 0.0


@dataclass
class Ctx:
    scale: float
    table_size: float
    kind: str                   # default／stack／diagram／cover
    column: int                 # 欄序號
    column_count: int
    dmax: float


def text_style(block: Block, ctx: Ctx) -> Style:
    if block.kind == "h2":
        return Style(25, 1.3, 16)
    if block.kind == "h3":
        return Style(26, 1.3, 20)
    if block.kind == "h1":
        return Style(62, 1.15, 14)
    if block.kind == "ul":
        small = ctx.kind == "diagram" or (ctx.kind == "stack" and ctx.column == ctx.column_count - 1 and ctx.column > 0)
        return Style(15 if small else 17, 1.5 if ctx.kind == "diagram" else 1.55, 8, 22)
    if block.kind == "quote":
        return Style(17, 1.55, 10)
    return Style(17, 1.55, 10)


def cell_pad(ctx: Ctx) -> tuple[float, float]:
    """表格儲存格的內距（px，已乘上縮放）：CSS 的 `.cols-1 td` 是 7px 12px（單欄的預設版面與 stack），其餘 5px 10px。"""
    single = ctx.kind == "stack" or (ctx.kind == "default" and ctx.column_count == 1)
    return (12 if single else 10) * ctx.scale, (7 if single else 5) * ctx.scale


def column_geometry(kind: str, count: int, ratio: str) -> list[tuple[float, float, float, float]]:
    """每一欄的 (x, y, w, h)，單位 px；stack 的第二欄在第一欄下方，位置由流程決定（這裡只給寬度）。"""
    if kind == "diagram":
        top, bottom, left_w, gap = 14, 46, 270, 28
        height = H_PX - top - bottom
        if count == 1:
            return [(BODY_L, top, W_PX - BODY_L - BODY_R, height)]
        image_w = W_PX - BODY_L - BODY_R - left_w - gap
        return [(BODY_L, top, image_w, height), (BODY_L + image_w + gap, top, left_w, height)]
    width, height = W_PX - BODY_L - BODY_R, H_PX - BODY_T - BODY_B
    if kind == "stack" or count == 1:
        return [(BODY_L, BODY_T, width, height)] * count
    left, _, right = (ratio or "1.2:0.8").partition(":")
    gap = 36
    total = float(left) + float(right)
    usable = width - gap
    first = usable * float(left) / total
    return [(BODY_L, BODY_T, first, height), (BODY_L + first + gap, BODY_T, usable - first, height)]


# ---- python-pptx 的小工具 -------------------------------------------------------------------------------------------------

def load_pptx(libs: str | None):
    if libs:
        sys.path.insert(0, str(Path(libs)))
    try:
        import pptx  # noqa: F401
    except ImportError as error:
        raise RenderError("找不到 python-pptx；請 `pip install python-pptx --target <資料夾>` 並以 --pptx-libs 指定（見本檔開頭的說明）。") from error


def px(value: float) -> int:
    return int(round(value * EMU))


def rgb(hex_color: str):
    from pptx.dml.color import RGBColor

    return RGBColor.from_string(hex_color.lstrip("#").upper())


def set_run_font(run, spec: Run, size_px: float, color: str, mono_default: bool = False) -> None:
    from pptx.util import Pt

    font = run.font
    font.size = Pt(round(size_px * 0.75 * 2) / 2)
    font.bold = spec.bold
    font.color.rgb = rgb(spec.color or (P["accent"] if spec.code else color))
    face = MONO if (spec.code or mono_default) else FONT
    font.name = face
    rpr = run._r.get_or_add_rPr()
    for tag in ("ea", "cs"):
        from lxml import etree

        existing = rpr.find(f"{{http://schemas.openxmlformats.org/drawingml/2006/main}}{tag}")
        if existing is None:
            existing = etree.SubElement(rpr, f"{{http://schemas.openxmlformats.org/drawingml/2006/main}}{tag}")
        existing.set("typeface", FONT)


def style_paragraph(paragraph, style: Style, scale: float, *, bullet: bool = False, align: str = "l") -> None:
    from lxml import etree
    from pptx.util import Pt

    paragraph.line_spacing = Pt(round(style.size * scale * style.line * 0.75, 1))
    paragraph.space_after = Pt(round(style.gap * scale * 0.75, 1))
    paragraph.space_before = Pt(0)
    ppr = paragraph._p.get_or_add_pPr()
    ppr.set("algn", align)
    ppr.set("eaLnBrk", "1")                    # 中文斷行規則
    ppr.set("hangingPunct", "1")               # 句尾標點可以懸掛，避免單獨一個「。」掉到下一行
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    if bullet:
        ppr.set("marL", str(px(style.indent * scale)))
        ppr.set("indent", str(-px(style.indent * scale)))
        color = etree.SubElement(ppr, f"{ns}buClr")
        etree.SubElement(color, f"{ns}srgbClr").set("val", P["accent"].lstrip("#"))
        etree.SubElement(ppr, f"{ns}buFont").set("typeface", "Arial")
        etree.SubElement(ppr, f"{ns}buChar").set("char", "•")
    else:
        ppr.set("marL", "0")
        ppr.set("indent", "0")
        etree.SubElement(ppr, f"{ns}buNone")


def fill_paragraph(paragraph, runs: list[Run], size_px: float, color: str, scale: float, mono_default: bool = False) -> None:
    for spec in runs:
        for index, piece in enumerate(spec.text.split("\n")):
            if index:
                paragraph.add_line_break()
            if piece == "":
                continue
            run = paragraph.add_run()
            run.text = piece
            set_run_font(run, Run(piece, spec.bold, spec.code, spec.color), size_px * scale, color, mono_default)


def new_textbox(slide, x: float, y: float, w: float, h: float):
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE

    box = slide.shapes.add_textbox(px(x), px(y), px(w), px(max(h, 4)))
    frame = box.text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.vertical_anchor = MSO_ANCHOR.TOP
    frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
    return box


def add_rect(slide, x: float, y: float, w: float, h: float, fill: str, name: str = ""):
    from pptx.enum.shapes import MSO_SHAPE

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, px(x), px(y), px(w), px(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb(fill)
    shape.line.fill.background()
    shape.shadow.inherit = False
    if name:
        shape.name = name
    return shape


# ---- 區塊的量測與輸出 ---------------------------------------------------------------------------------------------------

class Measure:
    """量測與輸出共用同一組公式，所以「放得下」的判斷與實際排版一致。"""

    def __init__(self, assets: dict[str, tuple[Path, int, int]], base_dir: Path) -> None:
        self.assets = assets
        self.base_dir = base_dir

    def text_height(self, block: Block, width: float, ctx: Ctx) -> float:
        style = text_style(block, ctx)
        size = style.size * ctx.scale
        line_h = size * style.line
        total = 0.0
        for runs in block.runs:
            avail = width - (style.indent * ctx.scale if block.kind == "ul" else 0)
            total += lines_needed(runs, size, avail) * line_h + style.gap * ctx.scale
        return total

    def table_layout(self, block: Block, width: float, ctx: Ctx) -> tuple[list[float], list[float], float]:
        size = ctx.table_size * ctx.scale
        columns = max(len(row) for row in block.rows)
        weights = []
        for c in range(columns):
            lengths = [runs_em(row[c]) for row in block.rows if c < len(row)]
            body = lengths[1:] or lengths
            weights.append(max(4.0, 0.55 * max(lengths) + 0.45 * (sum(body) / len(body))))
        pad_x, pad_y = cell_pad(ctx)
        cap = 0.5 * sum(weights)
        floors = [min(max(runs_em(row[c]) for row in block.rows if c < len(row)), 9.0) + 1.0 for c in range(columns)]
        weights = [max(min(w, cap), floor) for w, floor in zip(weights, floors)]
        total = sum(weights)
        widths = [width * w / total for w in weights]
        heights = []
        for r, row in enumerate(block.rows):
            lines = max(lines_needed(row[c], size, widths[c] - 2 * pad_x) if c < len(row) else 1 for c in range(columns))
            heights.append(lines * size * 1.42 + 2 * pad_y + (1 if r else 2))
        return widths, heights, pad_x

    def image_box(self, block: Block, width: float, max_h: float) -> tuple[float, float]:
        if block.kind == "mermaid":
            path, w_px, h_px = self.assets[block.ref]
            aspect = w_px / h_px
        else:
            path = (self.base_dir / block.ref)
            nw, nh = png_size(path)
            span = (block.crop[1] - block.crop[0]) if block.crop else 1.0
            aspect = nw / (nh * span)
        w = min(width, max_h * aspect)
        return w, w / aspect

    def height(self, block: Block, width: float, ctx: Ctx, max_h: float) -> float:
        if block.kind in ("p", "ul", "h1", "h2", "h3", "quote", "code"):
            extra = 36 * ctx.scale if (block.kind == "h2" and ctx.kind == "diagram") else 0
            return self.text_height(block, width, ctx) + extra
        if block.kind == "table":
            _, heights, _ = self.table_layout(block, width, ctx)
            return sum(heights) + 10 * ctx.scale
        if block.kind == "mermaid":
            return self.image_box(block, width, ctx.dmax)[1] + IMAGE_GAP
        if block.kind == "image":
            return self.image_box(block, width, min(max_h, 650.0))[1] + IMAGE_GAP
        return 0.0


def png_size(path: Path) -> tuple[int, int]:
    import struct

    with open(path, "rb") as handle:
        head = handle.read(32)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise RenderError(f"不是 PNG：{path}")
    return struct.unpack(">II", head[16:24])


def column_height(measure: Measure, blocks: list[Block], width: float, ctx: Ctx, max_h: float) -> float:
    total = sum(measure.height(block, width, ctx, max_h) for block in blocks)
    if blocks and blocks[-1].kind in ("mermaid", "image"):
        total -= IMAGE_GAP                      # 最後一張圖後面沒有間距
    return total


def is_list_column(blocks: list[Block]) -> bool:
    return bool(blocks) and all(block.kind == "ul" for block in blocks)


def split_list_blocks(measure: Measure, blocks: list[Block], width: float, ctx: Ctx) -> tuple[list[Block], list[Block]]:
    """stack 版面最後一欄的項目分成左右兩半（CSS 的 columns: 2），盡量讓兩邊高度接近；項目不拆開。"""
    items = [Block("ul", runs=[runs]) for block in blocks if block.kind == "ul" for runs in block.runs]
    heights = [measure.height(item, width, ctx, 0) for item in items]
    half, left_h, cut = sum(heights) / 2, 0.0, 0
    for index, height in enumerate(heights):
        if left_h + height / 2 > half:
            break
        left_h += height
        cut = index + 1
    return ([Block("ul", runs=[i.runs[0] for i in items[:cut]])] if cut else []), [Block("ul", runs=[i.runs[0] for i in items[cut:]])]


# ---- 輸出投影片 -----------------------------------------------------------------------------------------------------------

class Builder:
    def __init__(self, prs, assets, base_dir, label: str, total: int) -> None:
        self.prs, self.measure, self.label, self.total = prs, Measure(assets, base_dir), label, total
        self.problems: list[str] = []
        self.scales: dict[int, float] = {}

    # 各種區塊 ----------------------------------------------------------------------------------------

    def emit_text(self, slide, block: Block, x: float, y: float, w: float, ctx: Ctx) -> float:
        style = text_style(block, ctx)
        height = self.measure.text_height(block, w, ctx)
        if block.kind == "h2" and ctx.kind == "diagram":
            y += 36 * ctx.scale
        box = new_textbox(slide, x, y, w + 6, height)           # 多 6px：句尾標點剛好壓線時不會單獨掉到下一行
        frame = box.text_frame
        color = P["muted"] if block.kind == "quote" else P["text"]
        for index, runs in enumerate(block.runs):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            style_paragraph(paragraph, style, ctx.scale, bullet=block.kind == "ul")
            bold_runs = [Run(r.text, True, r.code, r.color) for r in runs] if block.kind in ("h1", "h2", "h3") and block.kind != "h3" else runs
            fill_paragraph(paragraph, bold_runs, style.size, color, ctx.scale, mono_default=block.kind == "code")
        return self.measure.height(block, w, ctx, 0)

    def emit_table(self, slide, block: Block, x: float, y: float, w: float, ctx: Ctx) -> float:
        from lxml import etree
        from pptx.enum.text import MSO_ANCHOR

        widths, heights, pad_x = self.measure.table_layout(block, w, ctx)
        rows, cols = len(block.rows), len(widths)
        frame = slide.shapes.add_table(rows, cols, px(x), px(y), px(w), px(sum(heights)))
        table = frame.table
        tbl_pr = table._tbl.tblPr
        for attribute in ("firstRow", "bandRow"):
            tbl_pr.set(attribute, "0")
        style_id = tbl_pr.find("{http://schemas.openxmlformats.org/drawingml/2006/main}tableStyleId")
        if style_id is None:
            style_id = etree.SubElement(tbl_pr, "{http://schemas.openxmlformats.org/drawingml/2006/main}tableStyleId")
        style_id.text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"          # No Style, No Grid
        for c, cell_w in enumerate(widths):
            table.columns[c].width = px(cell_w)
        size = ctx.table_size
        ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        for r, row in enumerate(block.rows):
            table.rows[r].height = px(heights[r])
            for c in range(cols):
                cell = table.cell(r, c)
                cell.margin_left = cell.margin_right = px(pad_x)
                cell.margin_top = cell.margin_bottom = px(cell_pad(ctx)[1])
                cell.vertical_anchor = MSO_ANCHOR.TOP
                tc_pr = cell._tc.get_or_add_tcPr()
                border = etree.Element(f"{ns}lnB", w=str(EMU * (1 if r else 1)), cap="flat", cmpd="sng", algn="ctr")
                color = P["accent"] if r == 0 else P["line"]
                etree.SubElement(etree.SubElement(border, f"{ns}solidFill"), f"{ns}srgbClr").set("val", color.lstrip("#"))
                tc_pr.insert(0, border)
                if r == 0:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = rgb(P["panel"])
                elif r % 2 == 0:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = rgb(PANEL_EVEN)
                else:
                    cell.fill.background()
                paragraph = cell.text_frame.paragraphs[0]
                style_paragraph(paragraph, Style(size, 1.42, 0), ctx.scale)
                runs = row[c] if c < len(row) else []
                if r == 0:
                    runs = [Run(run.text, True, run.code, P["accent"].lstrip("#").upper()) for run in runs]
                fill_paragraph(paragraph, runs, size, P["text"], ctx.scale)
        return sum(heights) + 10 * ctx.scale

    def emit_picture(self, slide, block: Block, x: float, y: float, w: float, ctx: Ctx, max_h: float, center: bool) -> float:
        width, height = self.measure.image_box(block, w, ctx.dmax if block.kind == "mermaid" else min(max_h, 650.0))
        left = x + (w - width) / 2 if center else x
        path = self.measure.assets[block.ref][0] if block.kind == "mermaid" else self.measure.base_dir / block.ref
        picture = slide.shapes.add_picture(str(path), px(left), px(y), px(width), px(height))
        if block.kind == "image" and block.crop:
            picture.crop_top, picture.crop_bottom = block.crop[0], 1 - block.crop[1]
            picture.line.color.rgb = rgb(P["line"])
            picture.line.width = EMU
        picture._element.nvPicPr.cNvPr.set("descr", block.alt or "Mermaid 圖（與 PDF 相同的渲染結果）")
        return height + IMAGE_GAP

    def flow(self, slide, blocks: list[Block], x: float, y: float, w: float, ctx: Ctx, max_h: float, center_images: bool) -> float:
        for block in blocks:
            if block.kind in ("mermaid", "image"):
                y += self.emit_picture(slide, block, x, y, w, ctx, ctx.dmax if block.kind == "mermaid" else max_h, center_images)
            elif block.kind == "table":
                y += self.emit_table(slide, block, x, y, w, ctx)
            else:
                y += self.emit_text(slide, block, x, y, w, ctx)
        return y

    # 一頁 ---------------------------------------------------------------------------------------------

    def chrome(self, slide, page: int, tag: str, src: str, title: str) -> None:
        if tag:
            box = new_textbox(slide, 56, 26, 600, 16)
            paragraph = box.text_frame.paragraphs[0]
            style_paragraph(paragraph, Style(12, 1.2, 0), 1.0)
            run = paragraph.add_run()
            run.text = tag
            set_run_font(run, Run(tag, False, True, P["accent"].lstrip("#").upper()), 12, P["accent"], True)
        if title:
            self.title(slide, title)
        self.footer(slide, page, src)

    def title(self, slide, title: str) -> None:
        from pptx.enum.text import MSO_ANCHOR

        shape = slide.shapes.title
        shape.left, shape.top, shape.width, shape.height = px(56), px(46), px(W_PX - 112), px(44)
        frame = shape.text_frame
        frame.word_wrap = True
        frame.vertical_anchor = MSO_ANCHOR.TOP
        frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
        size = 31.0
        em = runs_em(parse_inline(title)) * 1.04
        if em * size > W_PX - 112:
            size = (W_PX - 112) / em * 0.97
            self.problems.append(f"標題過長，縮小字級：{title[:30]}…（{size:.0f}px）")
        paragraph = frame.paragraphs[0]
        style_paragraph(paragraph, Style(size, 1.25, 0), 1.0)
        run = paragraph.add_run()
        run.text = title
        set_run_font(run, Run(title, True), size, P["text"])

    def footer(self, slide, page: int, src: str) -> None:
        add_rect(slide, 56, 677, W_PX - 112, 1, P["line"], "footer-line")
        limit = W_PX - 112 - 90
        text, em = "", 0.0
        for ch in src:
            if (em + char_em(ch)) * 11.5 * 1.04 > limit:
                text = text.rstrip() + "…"
                break
            text += ch
            em += char_em(ch)
        left = new_textbox(slide, 56, 683, limit, 16)
        paragraph = left.text_frame.paragraphs[0]
        style_paragraph(paragraph, Style(11.5, 1.2, 0), 1.0)
        fill_paragraph(paragraph, [Run(text)], 11.5, P["muted"], 1.0)
        right = new_textbox(slide, W_PX - 56 - 90, 683, 90, 16)
        paragraph = right.text_frame.paragraphs[0]
        style_paragraph(paragraph, Style(11.5, 1.2, 0), 1.0, align="r")
        fill_paragraph(paragraph, [Run(f"{page} / {self.total}")], 11.5, P["muted"], 1.0)

    def build(self, page: int, parsed: dict, counter: list[int]) -> None:
        from pptx.util import Emu

        meta, title = parsed["meta"], parsed["title"]
        layout = meta["layout"]
        blocks_by_col = [parse_blocks(md, counter) for md in parsed["cols"]]
        blank = self.prs.slide_layouts[6]
        slide = self.prs.slides.add_slide(self.prs.slide_layouts[5] if (title or layout == "cover") else blank)
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = rgb(P["bg"])
        src, tag = meta["src"], meta["tag"]
        count = len(blocks_by_col)

        if layout == "cover":
            self.cover(slide, page, blocks_by_col[0], tag, src)
            return

        size_override = float(meta["size"]) if meta["size"] else None
        dmax = float(meta["dmax"]) if meta["dmax"] else 560.0
        geometry = column_geometry(layout, count, meta["ratio"])
        stack = layout == "stack"
        tsize = size_override or (14 if stack else 15 if (count == 1 and layout == "default") else 13.5)

        def make_ctx(scale: float, index: int) -> Ctx:
            return Ctx(scale, tsize, layout, index, count, dmax)

        scale = 1.0
        chosen = None
        while scale >= MIN_SCALE - 1e-9:
            if self.fits(blocks_by_col, geometry, layout, count, make_ctx, scale, dmax):
                chosen = scale
                break
            scale = round(scale - 0.04, 2)
        if chosen is None:
            chosen = MIN_SCALE
            self.problems.append(f"第 {page} 頁（{tag or title}）：縮到 {MIN_SCALE} 倍仍放不下，內容可能溢出投影片")
        self.scales[page] = chosen

        self.chrome(slide, page, tag if layout != "diagram" else "", src, title)
        measure = self.measure
        if stack:
            ctx0 = make_ctx(chosen, 0)
            y = BODY_T
            y = self.flow(slide, blocks_by_col[0], geometry[0][0], y, geometry[0][2], ctx0, 0, False) + 4
            if count > 1:
                last = make_ctx(chosen, 1)
                if is_list_column(blocks_by_col[1]):
                    col_w = (geometry[1][2] - 40) / 2
                    left, right = split_list_blocks(measure, blocks_by_col[1], col_w, last)
                    self.flow(slide, left, geometry[1][0], y, col_w, last, 0, False)
                    self.flow(slide, right, geometry[1][0] + col_w + 40, y, col_w, last, 0, False)
                else:
                    self.flow(slide, blocks_by_col[1], geometry[1][0], y, geometry[1][2], last, 0, False)
            return
        for index, blocks in enumerate(blocks_by_col):
            x, y0, w, h = geometry[index]
            ctx = make_ctx(chosen, index)
            self.flow(slide, blocks, x, y0, w, ctx, h, layout == "diagram" and index == 0)

    def fits(self, blocks_by_col, geometry, layout, count, make_ctx, scale: float, dmax: float) -> bool:
        measure = self.measure
        if layout == "stack":
            avail = H_PX - BODY_T - BODY_B
            ctx0 = make_ctx(scale, 0)
            top = column_height(measure, blocks_by_col[0], geometry[0][2], ctx0, 0) + 4
            bottom = 0.0
            if count > 1:
                last = make_ctx(scale, 1)
                if is_list_column(blocks_by_col[1]):
                    col_w = (geometry[1][2] - 40) / 2
                    left, right = split_list_blocks(measure, blocks_by_col[1], col_w, last)
                    bottom = max(column_height(measure, left, col_w, last, 0), column_height(measure, right, col_w, last, 0))
                else:
                    bottom = column_height(measure, blocks_by_col[1], geometry[1][2], last, 0)
            return top + bottom <= avail + 1
        for index, blocks in enumerate(blocks_by_col):
            _, _, w, h = geometry[index]
            if column_height(measure, blocks, w, make_ctx(scale, index), h) > h + 1:
                return False
        return True

    def cover(self, slide, page: int, blocks: list[Block], tag: str, src: str) -> None:
        from pptx.enum.text import MSO_ANCHOR

        add_rect(slide, 0, 0, 6, H_PX, P["accent"], "cover-bar")
        self.chrome(slide, page, tag, src, "")
        y = 190.0
        for block in blocks:
            if block.kind == "h1":
                shape = slide.shapes.title
                shape.left, shape.top, shape.width, shape.height = px(90), px(y), px(W_PX - 180), px(80)
                frame = shape.text_frame
                frame.word_wrap = True
                frame.vertical_anchor = MSO_ANCHOR.TOP
                frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
                paragraph = frame.paragraphs[0]
                style_paragraph(paragraph, Style(62, 1.15, 0), 1.0)
                run = paragraph.add_run()
                run.text = "".join(r.text for r in block.runs[0])
                set_run_font(run, Run(run.text, True), 62, P["text"])
                y += 62 * 1.15 + 14
            elif block.kind == "h3":
                box = new_textbox(slide, 90, y, W_PX - 180, 36)
                paragraph = box.text_frame.paragraphs[0]
                style_paragraph(paragraph, Style(26, 1.3, 0), 1.0)
                fill_paragraph(paragraph, block.runs[0], 26, P["muted"], 1.0)
                y += 26 * 1.3 + 34
            elif block.kind == "quote":
                runs = block.runs[0]
                width = min(W_PX - 180, runs_em(runs) * 17 * 1.05 + 40)
                add_rect(slide, 90, y, 2, 17 * 1.55 + 16, P["accent"], "quote-bar")
                add_rect(slide, 92, y, width - 2, 17 * 1.55 + 16, P["panel"], "quote-fill")
                box = new_textbox(slide, 92 + 14, y + 8, width - 28, 17 * 1.55)
                paragraph = box.text_frame.paragraphs[0]
                style_paragraph(paragraph, Style(17, 1.55, 0), 1.0)
                fill_paragraph(paragraph, runs, 17, P["muted"], 1.0)
                y += 17 * 1.55 + 16 + 10


# ---- Mermaid：與 PDF 相同的渲染結果 -----------------------------------------------------------------------------------------

def render_mermaid(slides: list[dict], libs: Path, base_dir: Path, workdir: Path) -> dict[str, tuple[Path, int, int]]:
    page_html = render_html(slides, libs, base_dir)
    work = workdir / "deck-render.html"
    write_text(work, page_html)
    dom = html_dump_dom(work, wait_ms=40000)
    if 'class="mermaid-error"' in dom or 'data-ready="1"' not in dom:
        raise RenderError("簡報渲染有錯誤或沒有跑完；請先用 build_deck.py --check 確認。")
    svgs = re.findall(r'<pre class="mermaid"[^>]*>\s*(<svg.*?</svg>)\s*</pre>', dom, re.S)
    assets: dict[str, tuple[Path, int, int]] = {}
    for index, svg in enumerate(svgs):
        if svg.count("<svg") != svg.count("</svg>"):
            raise RenderError(f"第 {index} 張 Mermaid 圖的 SVG 含巢狀的 <svg>，無法用簡單的方式擷取")
        box = re.search(r'viewBox="[-0-9.]+ [-0-9.]+ ([0-9.]+) ([0-9.]+)"', svg)
        if not box:
            raise RenderError(f"第 {index} 張 Mermaid 圖沒有 viewBox")
        w, h = math.ceil(float(box.group(1))), math.ceil(float(box.group(2)))
        page = workdir / f"mermaid-{index:02d}.html"
        write_text(
            page,
            "<!doctype html><html lang=\"zh-Hant-TW\"><head><meta charset=\"UTF-8\">"
            f'<link rel="stylesheet" href="{file_url(FONTS_CSS)}">'
            f"<style>html,body{{margin:0;padding:0;background:{P['bg']};overflow:hidden}}"
            f"svg{{display:block;width:{w}px !important;height:{h}px !important;max-width:none !important}}</style></head>"
            f"<body>{svg}</body></html>",
        )
        png = workdir / f"mermaid-{index:02d}.png"
        html_to_png(page, png, width=w, height=h, scale=min(3.0, 4200 / w), wait_ms=15000)
        assets[str(index)] = (png, w, h)
    return assets


# ---- 主程式 ---------------------------------------------------------------------------------------------------------------------

def apply_label(parsed: dict, label: str) -> None:
    """封面的版本字樣（標籤與標題）改成 pptx 的版本；其餘內文裡的 2.0（例如 1.0 到 2.0 的里程碑）是歷史事實，不動。"""
    if parsed["meta"]["layout"] != "cover":
        return
    parsed["meta"]["tag"] = re.sub(r"\b2\.0\b", label, parsed["meta"]["tag"])
    parsed["cols"][0] = re.sub(r"(^# .*?)\b2\.0\b", rf"\g<1>{label}", parsed["cols"][0], count=1, flags=re.M)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="把 docs/2.0簡報.md 轉成 PowerPoint（.pptx）")
    parser.add_argument("--libs", default=os.environ.get("DECK_LIBS"), help="含 mermaid.min.js 與 marked.umd.js 的資料夾（同 build_deck.py）")
    parser.add_argument("--pptx-libs", default=os.environ.get("PPTX_LIBS"), help="含 python-pptx 的資料夾（pip install python-pptx --target）")
    parser.add_argument("--label", default="2.1", help="封面標示的版本字樣")
    parser.add_argument("--source", default=str(SOURCE))
    parser.add_argument("--output", default=str(OUTPUT))
    parser.add_argument("--keep", help="保留 Mermaid 的中間檔（HTML 與 PNG）的資料夾，供除錯")
    parser.add_argument("--reuse", action="store_true", help="與 --keep 併用：資料夾裡已有上次渲染的 Mermaid PNG 時直接使用（調整版面時省去重新渲染）")
    args = parser.parse_args(argv[1:])
    if not args.libs:
        print("請以 --libs 或環境變數 DECK_LIBS 指定 mermaid 與 marked 所在的資料夾（見 build_deck.py 開頭的說明）。", file=sys.stderr)
        return 2
    try:
        load_pptx(args.pptx_libs)
    except RenderError as error:
        print(error, file=sys.stderr)
        return 2
    from pptx import Presentation
    from pptx.util import Emu

    source = Path(args.source)
    slides = [parse_slide(raw) for raw in split_slides(read_text(source))]
    workdir = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="deck-pptx-"))
    workdir.mkdir(parents=True, exist_ok=True)
    index_file = workdir / "assets.json"
    if args.reuse and index_file.is_file():
        assets = {key: (Path(item[0]), item[1], item[2]) for key, item in json.loads(index_file.read_text(encoding="utf-8")).items()}
    else:
        assets = render_mermaid(slides, Path(args.libs), source.parent, workdir)
        index_file.write_text(json.dumps({key: [str(item[0]), item[1], item[2]] for key, item in assets.items()}, ensure_ascii=False), encoding="utf-8")
    for parsed in slides:
        apply_label(parsed, args.label)

    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(W_PX * EMU), Emu(H_PX * EMU)
    builder = Builder(prs, assets, source.parent, args.label, len(slides))
    counter = [0]
    for page, parsed in enumerate(slides, start=1):
        builder.build(page, parsed, counter)
    if counter[0] != len(assets):
        print(f"Mermaid 圖數量不一致：Markdown {counter[0]} 張、渲染 {len(assets)} 張", file=sys.stderr)
        return 1
    prs.core_properties.title = f"雙核心平台 {args.label} 簡報"
    prs.core_properties.subject = "開放世界文字 RPG × 會員制智能客服機器人：專案狀態報告"
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))

    reopened = Presentation(str(out))
    print(f"共 {len(slides)} 頁；Mermaid 圖 {len(assets)} 張；重新開啟後 {len(reopened.slides)} 頁；已輸出 {out}")
    shrunk = [f"第 {page} 頁 {scale:.2f}" for page, scale in builder.scales.items() if scale < 1.0]
    if shrunk:
        print("字級縮小（倍率）：" + "、".join(shrunk))
    for problem in builder.problems:
        print("  ✗", problem)
    if len(reopened.slides) != len(slides):
        print("投影片數與簡報頁數不一致。", file=sys.stderr)
        return 1
    return 3 if builder.problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
