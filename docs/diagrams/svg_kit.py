"""手寫 SVG 的小工具：卡片、膠囊標籤、箭頭、步驟徽章與自動換行。

圖由 Python 直接組成 SVG（沒有外部繪圖套件），再由 Chrome 轉成 PNG；版面座標寫在 build_diagrams.py。
文字寬度以「中日韓字元 = 1 em、拉丁字元 = 約 0.56 em」估算，所以換行是估計值，改完圖請目視檢查成品。
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

from render_tools import FONT_STACK, MONO_STACK, PALETTE, icon_data_uri

P = PALETTE


def tw(text: str, size: float, mono: bool = False) -> float:
    """估算文字寬度（像素）。"""
    total = 0.0
    for ch in text:
        code = ord(ch)
        if code >= 0x2E80 or 0xFF00 <= code <= 0xFFEF or ch in "・／：，。、；「」（）【】《》":
            total += size
        elif 0x2000 <= code <= 0x206F:
            total += size * 0.9
        else:
            total += size * (0.6 if mono else 0.56)
    return total


_NO_LINE_START = set("）」』】》〕，。、；：！？・／,.;:!?)]}")
_NO_LINE_END = set("（「『【《〔([{")
_TOKEN = re.compile(r"[A-Za-z0-9_.\/:()<>\-@#+*=&,;'\"\[\]{}|?!~`$%^]+|\s+|.", re.S)


def _split_long(tokens: list[str], size: float, max_width: float, mono: bool) -> list[str]:
    """比整行還寬的拉丁字詞（例如很長的端點路徑）：優先在 / _ . - 之後斷開，仍太寬就逐字斷開。"""
    result: list[str] = []
    for token in tokens:
        if tw(token, size, mono) <= max_width:
            result.append(token)
            continue
        pieces = [p for p in re.split(r"(?<=[/_.\-])", token) if p]
        for piece in pieces:
            if tw(piece, size, mono) <= max_width:
                result.append(piece)
            else:
                chunk = ""
                for ch in piece:
                    if tw(chunk + ch, size, mono) > max_width and chunk:
                        result.append(chunk)
                        chunk = ""
                    chunk += ch
                if chunk:
                    result.append(chunk)
    return result


def wrap(text: str, size: float, max_width: float, mono: bool = False) -> list[str]:
    """依估算寬度換行；拉丁字詞不拆開，中日韓字元逐字可斷。明確的 \\n 一律換行。"""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for token in _split_long(_TOKEN.findall(paragraph), size, max_width, mono):
            if token.isspace() and not current:
                continue
            if tw(current + token, size, mono) <= max_width or not current:
                current += token
            elif token in _NO_LINE_START and tw(current + token, size, mono) <= max_width + size:
                current += token                          # 避頭：右括號、頓號、句號不放行首（允許懸掛一個字寬）
            else:
                carry = ""
                if current and current[-1] in _NO_LINE_END:   # 避尾：左括號不放行尾，連同下一個字詞移到下一行
                    carry, current = current[-1], current[:-1]
                lines.append(current.rstrip())
                current = "" if token.isspace() else carry + token
        lines.append(current.rstrip())
    return lines


class Svg:
    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.parts: list[str] = []

    # ---- 基本元素 -------------------------------------------------------------------

    def add(self, markup: str) -> None:
        self.parts.append(markup)

    def rect(self, x, y, w, h, *, fill=None, stroke=None, rx=10, dash=None, sw=1.2, opacity=None) -> None:
        attrs = [f'x="{x:.1f}"', f'y="{y:.1f}"', f'width="{w:.1f}"', f'height="{h:.1f}"', f'rx="{rx}"']
        attrs.append(f'fill="{fill or "none"}"')
        if stroke:
            attrs.append(f'stroke="{stroke}" stroke-width="{sw}"')
        if dash:
            attrs.append(f'stroke-dasharray="{dash}"')
        if opacity is not None:
            attrs.append(f'opacity="{opacity}"')
        self.add(f"<rect {' '.join(attrs)}/>")

    def text(self, x, y, value, *, size=16, fill=None, weight=400, anchor="start", mono=False, halo=False) -> None:
        family = MONO_STACK if mono else FONT_STACK
        halo_attrs = f' stroke="{P["bg"]}" stroke-width="5" stroke-linejoin="round" paint-order="stroke"' if halo else ""
        self.add(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
            f'font-family="{family}" fill="{fill or P["text"]}"{halo_attrs}>{escape(value)}</text>'
        )

    def lines(self, x, y, items: list[str], *, size=16, lh=None, fill=None, weight=400, mono=False, anchor="start") -> float:
        """逐行輸出，回傳最後一行下方的 y。"""
        lh = lh or round(size * 1.5)
        for i, item in enumerate(items):
            self.text(x, y + i * lh, item, size=size, fill=fill, weight=weight, mono=mono, anchor=anchor)
        return y + len(items) * lh

    def icon(self, name: str, x, y, size=44) -> None:
        self.add(f'<image x="{x:.1f}" y="{y:.1f}" width="{size}" height="{size}" href="{icon_data_uri(name)}"/>')

    def line(self, x1, y1, x2, y2, *, stroke=None, sw=1, dash=None) -> None:
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke or P["line"]}" stroke-width="{sw}"{dash_attr}/>')

    # ---- 步驟徽章與箭頭 -------------------------------------------------------------------

    def badge(self, cx, cy, label, *, r=11, fill=None, color=None, size=13) -> None:
        self.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{fill or P["accent"]}"/>')
        self.text(cx, cy + size * 0.36, str(label), size=size, fill=color or P["bg"], weight=700, anchor="middle")

    def arrow(self, points, *, color=None, sw=2.2, dash=None, both=False, label=None, label_at=None, label_size=14) -> None:
        color = color or P["accent"]
        coords = " ".join(f"{px:.1f},{py:.1f}" for px, py in points)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        marker_id = "arr_" + color.lstrip("#")
        start = f' marker-start="url(#{marker_id})"' if both else ""
        self.add(
            f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{sw}" stroke-linejoin="round" '
            f'stroke-linecap="round"{dash_attr} marker-end="url(#{marker_id})"{start}/>'
        )
        if label:
            lx, ly = label_at or points[len(points) // 2]
            self.text(lx, ly, label, size=label_size, fill=color if color != P["line"] else P["muted"], anchor="middle", halo=True)

    # ---- 組合元件 -------------------------------------------------------------------

    def card(self, x, y, w, *, icon=None, azure=None, title="", body=(), body_size=15, title_size=20, dashed=False,
             fill=None, stroke=None, min_h=0, mono_body=False, badges=()) -> float:
        """圖示卡片。回傳卡片高度。標題與圖示在上，內文在下，內文寬度為整張卡片。"""
        pad = 14
        icon_size = 44 if icon else 0
        head_x = x + pad + (icon_size + 12 if icon else 0)
        title_lines = wrap(title, title_size, w - (head_x - x) - pad)
        azure_lines = wrap(f"Azure 圖示：{azure}", 11, w - (head_x - x) - pad) if azure else []
        head_h = max(icon_size, len(title_lines) * round(title_size * 1.3) + 4 + len(azure_lines) * 14)
        body_lines: list[str] = []
        for item in body:
            body_lines.extend(wrap(item, body_size, w - 2 * pad, mono=mono_body))
        lh = round(body_size * 1.5)
        height = max(min_h, pad + head_h + (10 + len(body_lines) * lh if body_lines else 0) + pad - 2)
        self.rect(x, y, w, height, fill=fill or P["panel"], stroke=stroke or P["line"], rx=12, dash="7 5" if dashed else None)
        if icon:
            self.icon(icon, x + pad, y + pad, icon_size)
        ty = y + pad + title_size * 0.95
        self.lines(head_x, ty, title_lines, size=title_size, lh=round(title_size * 1.3), weight=700)
        if azure_lines:
            self.lines(head_x, ty + (len(title_lines) - 1) * round(title_size * 1.3) + 19, azure_lines, size=11, lh=14, fill=P["muted"])
        if body_lines:
            self.lines(x + pad, y + pad + head_h + 10 + body_size, body_lines, size=body_size, lh=lh, fill=P["muted"], mono=mono_body)
        for i, number in enumerate(badges):          # 徽章釘在卡片上緣的右側，不佔標題的位置
            self.badge(x + w - 20 - i * 26, y, number)
        return height

    def chips(self, x, y, w, items, *, size=14, gap=8, padx=10, pady=6, mono=False, dashed=False) -> float:
        """膠囊標籤流式排列。items 為 (文字, [步驟編號]) 或 文字；回傳最後一列下方的 y。"""
        cx, cy = x, y
        row_h = size + 2 * pady
        for item in items:
            label, steps = (item, ()) if isinstance(item, str) else item
            lines = label.split("\n")
            text_w = max(tw(line, size, mono) for line in lines)
            chip_w = text_w + 2 * padx + (len(steps) * 24 + 2 if steps else 0)
            chip_h = len(lines) * round(size * 1.35) + 2 * pady - (round(size * 1.35) - size)
            if cx + chip_w > x + w and cx > x:
                cx, cy = x, cy + row_h + gap + (chip_h - row_h if chip_h > row_h else 0)
            self.rect(cx, cy, chip_w, chip_h, fill=P["bg"], stroke=P["line"] if not dashed else P["muted"], rx=chip_h / 2 if len(lines) == 1 else 10, dash="5 4" if dashed else None)
            for i, line in enumerate(lines):
                self.text(cx + padx, cy + pady + size * 0.88 + i * round(size * 1.35), line, size=size, mono=mono)
            for i, step in enumerate(steps):
                self.badge(cx + text_w + padx + 13 + i * 24, cy + chip_h / 2, step, r=10, size=12)
            cx += chip_w + gap
        return cy + row_h

    def render(self) -> str:
        markers = []
        for color in {P["accent"], P["muted"], P["ok"], P["text"]}:
            markers.append(
                f'<marker id="arr_{color.lstrip("#")}" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="12" markerHeight="12" '
                f'orient="auto-start-reverse" markerUnits="userSpaceOnUse"><path d="M1,1 L11,6 L1,11 L3.5,6 Z" fill="{color}"/></marker>'
            )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" viewBox="0 0 {self.width} {self.height}">'
            f'<defs>{"".join(markers)}</defs>'
            f'<rect width="{self.width}" height="{self.height}" fill="{P["bg"]}"/>'
            + "".join(self.parts)
            + "</svg>"
        )
