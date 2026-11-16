"""把 docs/1.0簡報.md 轉成 16:9 的 PDF（docs/1.0簡報.pdf）。

用法（專案根目錄）：
    python docs/diagrams/build_deck.py --libs <資料夾>            產生 PDF
    python docs/diagrams/build_deck.py --libs <資料夾> --check    只檢查渲染結果（Mermaid 語法、圖片），不產生 PDF
    python docs/diagrams/build_deck.py --libs <資料夾> --html out.html   另存渲染用的 HTML 供除錯

<資料夾> 必須含 mermaid.min.js 與 marked.umd.js。它們是**只在產生文件時用到**的工具，不屬於應用程式，不放進版本庫：
    npm install --prefix <資料夾> mermaid@11.4.1 marked@12.0.2
    然後  --libs <資料夾>\\node_modules   （mermaid.min.js 在 mermaid/dist、marked.umd.js 在 marked/lib，本程式會自己找）
也可以用環境變數 DECK_LIBS 指定。

簡報的 Markdown 約定：
- 以單獨一行的 --- 分頁；程式碼區塊內的 --- 不算。
- 頁首的 HTML 註解是指令：<!-- layout: cover|diagram -->、<!-- tag: 標籤 -->、<!-- src: 來源說明 -->。
- <!-- col --> 把一頁分成左右兩欄；第一個「## 標題」成為整頁的標題（diagram 版面除外，標題在右欄）。
- ```mermaid 區塊由 mermaid 渲染；圖片的標題字串 "crop=0-0.58" 表示只顯示圖片高度 0% 到 58% 的區域。
所有字型是專案自行託管的 Noto Sans TC（app/static/fonts），不使用任何外部資源。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from pathlib import Path

from render_tools import DOCS_DIR, FONTS_CSS, FONT_STACK, MONO_STACK, PALETTE, RenderError, file_url, html_dump_dom, html_to_pdf, read_text, write_text

P = PALETTE
SOURCE = DOCS_DIR / "1.0簡報.md"
OUTPUT = DOCS_DIR / "1.0簡報.pdf"

DIRECTIVE = re.compile(r"^<!--\s*(layout|tag|src|ratio|size|dmax):\s*(.*?)\s*-->\s*$")
COL_MARK = re.compile(r"^<!--\s*col\s*-->\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def find_lib(root: Path, name: str, subdirs: tuple[str, ...]) -> Path:
    for candidate in (root / name, *(root / sub / name for sub in subdirs)):
        if candidate.is_file():
            return candidate
    raise RenderError(f"在 {root} 找不到 {name}；請先 npm install（見本檔開頭的說明）。")


def split_slides(markdown: str) -> list[str]:
    slides: list[list[str]] = [[]]
    in_fence = False
    for line in markdown.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and line.strip() == "---":
            slides.append([])
            continue
        slides[-1].append(line)
    return ["\n".join(lines).strip("\n") for lines in slides if "\n".join(lines).strip()]


def parse_slide(raw: str) -> dict:
    meta = {"layout": "default", "tag": "", "src": "", "ratio": "", "size": "", "dmax": ""}
    body: list[str] = []
    for line in raw.splitlines():
        match = DIRECTIVE.match(line.strip())
        if match:
            meta[match.group(1)] = match.group(2)
        else:
            body.append(line)
    columns: list[list[str]] = [[]]
    in_fence = False
    for line in body:
        if FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and COL_MARK.match(line.strip()):
            columns.append([])
            continue
        columns[-1].append(line)
    cols = ["\n".join(c).strip("\n") for c in columns]
    title = ""
    if meta["layout"] not in ("cover", "diagram"):
        for index, line in enumerate(cols[0].splitlines()):
            if line.startswith("## "):
                title = line[3:].strip()
                rest = cols[0].splitlines()
                del rest[index]
                cols[0] = "\n".join(rest).strip("\n")
                break
    return {"meta": meta, "title": title, "cols": cols}


CSS = f"""
@page {{ size: 1280px 720px; margin: 0; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: {P['bg']}; color: {P['text']}; font-family: {FONT_STACK}; font-size: 17px; line-height: 1.55; }}
.slide {{ position: relative; width: 1280px; height: 720px; overflow: hidden; page-break-after: always; break-after: page; background: {P['bg']}; padding: 0; }}
.slide:last-of-type {{ page-break-after: auto; break-after: auto; }}
.tag {{ position: absolute; left: 56px; top: 26px; font-family: {MONO_STACK}; font-size: 12px; letter-spacing: .08em; color: {P['accent']}; }}
.title {{ position: absolute; left: 56px; top: 46px; right: 56px; margin: 0; font-size: 31px; line-height: 1.25; font-weight: 700; color: {P['text']}; }}
.body {{ position: absolute; left: 56px; right: 56px; top: 104px; bottom: 50px; display: grid; gap: 36px; align-items: start; overflow: hidden; }}
.body.cols-1 {{ grid-template-columns: minmax(0, 1fr); }}
.stack .body {{ grid-template-columns: minmax(0, 1fr); gap: 14px; }}
.stack .col:last-child ul {{ columns: 2; column-gap: 40px; }}
.stack .col:last-child li {{ break-inside: avoid; font-size: 15px; }}
.body.cols-2 {{ grid-template-columns: minmax(0, 1.2fr) minmax(0, 0.8fr); }}
.foot {{ position: absolute; left: 56px; right: 56px; bottom: 18px; display: flex; justify-content: space-between; gap: 24px; font-size: 11.5px; color: {P['muted']}; border-top: 1px solid {P['line']}; padding-top: 7px; }}
.foot .src {{ overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }}
.col > :first-child {{ margin-top: 0; }}
p {{ margin: 0 0 10px; }}
ul {{ margin: 0 0 10px; padding-left: 22px; }}
li {{ margin: 0 0 8px; }}
li::marker {{ color: {P['accent']}; }}
strong {{ color: {P['text']}; font-weight: 700; }}
a {{ color: {P['accent']}; text-decoration: none; }}
code {{ font-family: {MONO_STACK}; font-size: .86em; color: {P['accent']}; background: {P['panel']}; border: 1px solid {P['line']}; border-radius: 4px; padding: 0 5px; }}
blockquote {{ margin: 0 0 10px; padding: 8px 14px; border-left: 2px solid {P['accent']}; background: rgba(28,30,35,.75); color: {P['muted']}; }}
blockquote p {{ margin: 0; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; line-height: 1.42; margin: 0 0 10px; }}
th {{ text-align: left; color: {P['accent']}; font-weight: 700; padding: 6px 10px; border-bottom: 1px solid {P['accent']}; background: {P['panel']}; }}
td {{ padding: 5px 10px; border-bottom: 1px solid {P['line']}; vertical-align: top; color: {P['text']}; }}
tr:nth-child(even) td {{ background: rgba(28,30,35,.55); }}
td code {{ font-size: .88em; }}
.sw {{ display: inline-block; width: 14px; height: 14px; border: 1px solid #4a4e58; border-radius: 3px; vertical-align: -2px; margin-right: 6px; }}
pre.mermaid {{ margin: 0; background: transparent; border: 0; padding: 0; text-align: center; visibility: hidden; }}
pre.mermaid[data-processed] {{ visibility: visible; }}
pre.mermaid svg {{ width: 100%; max-width: none !important; height: auto; max-height: 560px; }}
.stack pre.mermaid svg {{ max-height: var(--dmax, 340px); }}
.slide pre.mermaid svg {{ max-height: var(--dmax, 560px); }}
.cols-1 table {{ font-size: var(--tsize, 15px); }}
.cols-1 td {{ padding: 7px 12px; }}
.stack table {{ font-size: var(--tsize, 14px); }}
pre.mermaid-error {{ color: #ff8a8a; white-space: pre-wrap; font-family: {MONO_STACK}; font-size: 12px; border: 1px solid #ff5555; padding: 8px; }}
.crop {{ overflow: hidden; width: 100%; border: 1px solid {P['line']}; border-radius: 6px; }}
.crop img {{ display: block; width: 100%; }}
img {{ max-width: 100%; }}
/* cover */
.cover .cover-body {{ position: absolute; left: 90px; right: 90px; top: 190px; }}
.cover h1 {{ margin: 0 0 14px; font-size: 62px; line-height: 1.15; font-weight: 700; }}
.cover h3 {{ margin: 0 0 34px; font-size: 26px; font-weight: 400; color: {P['muted']}; }}
.cover blockquote {{ display: inline-block; font-size: 17px; }}
.cover .bar {{ position: absolute; left: 0; top: 0; bottom: 0; width: 6px; background: {P['accent']}; }}
/* diagram：左邊是圖，右邊是說明 */
.diagram .body {{ top: 14px; bottom: 46px; grid-template-columns: minmax(0, 1fr) 270px; gap: 28px; }}
.diagram .body.cols-1 {{ grid-template-columns: minmax(0, 1fr); }}
.diagram .col:first-child .crop img {{ width: 100%; }}
.diagram .col:last-child h2 {{ margin: 36px 0 16px; font-size: 25px; line-height: 1.3; font-weight: 700; }}
.diagram .col:last-child li {{ font-size: 15px; line-height: 1.5; }}
"""

SCRIPT = r"""
(async () => {
  mermaid.initialize({ startOnLoad: false });      // 必須最先執行：否則 mermaid 會在 DOMContentLoaded 自動渲染，搶在字型載入之前
  const errors = [];
  const state = JSON.parse(document.getElementById('deck-state').textContent);
  marked.use({
    renderer: {
      code(code, lang) {
        const source = typeof code === 'object' ? code.text : code;
        const language = typeof code === 'object' ? code.lang : lang;
        if (language === 'mermaid') return '<pre class="mermaid">' + source.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</pre>\n';
        return '<pre><code>' + source.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</code></pre>\n';
      },
      image(href, title, text) {
        const h = typeof href === 'object' ? href.href : href;
        const t = typeof href === 'object' ? href.title : title;
        const a = typeof href === 'object' ? href.text : text;
        const crop = /crop=([0-9.]+)-([0-9.]+)/.exec(t || '');
        if (crop) return '<div class="crop" data-from="' + crop[1] + '" data-to="' + crop[2] + '"><img src="' + h + '" alt="' + a + '"></div>';
        return '<img src="' + h + '" alt="' + a + '">';
      }
    }
  });
  document.querySelectorAll('script[type="text/markdown"]').forEach((node) => {
    const target = document.getElementById(node.dataset.target);
    target.innerHTML = marked.parse(node.textContent, { gfm: true, breaks: false });
  });

  // 圖片：載入後檢查，並套用裁切區域
  const images = Array.from(document.images);
  await Promise.all(images.map((img) => img.complete ? Promise.resolve() : new Promise((resolve) => { img.onload = img.onerror = resolve; })));
  images.forEach((img) => {
    if (!img.naturalWidth) { errors.push('圖片載入失敗：' + img.getAttribute('src')); return; }
    const box = img.closest('.crop');
    if (box) {
      const from = parseFloat(box.dataset.from), to = parseFloat(box.dataset.to);
      const ratio = img.naturalHeight / img.naturalWidth;
      const aspect = img.naturalWidth / (img.naturalHeight * (to - from));
      box.style.aspectRatio = (img.naturalWidth) + ' / ' + (img.naturalHeight * (to - from));
      img.style.marginTop = '-' + (from * ratio * 100) + '%';
      if (box.closest('.diagram')) { box.style.maxWidth = (650 * aspect) + 'px'; box.style.margin = '0 auto'; }
    }
  });

  // 字型：Noto Sans TC 以 unicode-range 切片，先把圖裡用到的字載入，Mermaid 才量得到正確的文字寬度
  const blocks = Array.from(document.querySelectorAll('pre.mermaid'));
  const sample = blocks.map((b) => b.textContent).join('') + document.body.innerText;
  try {
    await document.fonts.load('400 16px "Noto Sans TC Variable"', sample);
    await document.fonts.load('700 16px "Noto Sans TC Variable"', sample);
    await document.fonts.ready;
  } catch (e) { errors.push('字型載入失敗：' + e); }

  mermaid.initialize({
    startOnLoad: false, theme: 'base', securityLevel: 'loose', fontFamily: state.font,
    flowchart: { useMaxWidth: true, htmlLabels: true, curve: 'basis', padding: 10 },
    sequence: { useMaxWidth: true, mirrorActors: false, actorMargin: 40, messageMargin: 28, boxMargin: 8, noteMargin: 8 },
    er: { useMaxWidth: true },
    themeVariables: {
      background: '#0F1012', fontFamily: state.font, fontSize: '15px',
      primaryColor: '#1C1E23', primaryTextColor: '#E2E4E9', primaryBorderColor: '#5a5f6b',
      secondaryColor: '#16181C', tertiaryColor: '#14161A',
      lineColor: '#8A8F99', textColor: '#E2E4E9', mainBkg: '#1C1E23', nodeBorder: '#5a5f6b',
      clusterBkg: '#14161A', clusterBorder: '#3a3e46', titleColor: '#E2E4E9', edgeLabelBackground: '#0F1012',
      actorBkg: '#1C1E23', actorBorder: '#FF6B35', actorTextColor: '#E2E4E9', actorLineColor: '#5a5f6b',
      signalColor: '#B8BCC6', signalTextColor: '#E2E4E9', labelBoxBkgColor: '#1C1E23', labelBoxBorderColor: '#5a5f6b', labelTextColor: '#E2E4E9',
      loopTextColor: '#E2E4E9', noteBkgColor: '#2a2118', noteTextColor: '#E2E4E9', noteBorderColor: '#FF6B35',
      activationBkgColor: '#1C1E23', activationBorderColor: '#FF6B35', sequenceNumberColor: '#0F1012',
      attributeBackgroundColorOdd: '#16181C', attributeBackgroundColorEven: '#1C1E23',
      transitionColor: '#8A8F99', stateBkg: '#1C1E23', stateLabelColor: '#E2E4E9', labelColor: '#E2E4E9',
      altBackground: '#16181C', compositeBackground: '#14161A', compositeBorder: '#5a5f6b',
      specialStateColor: '#8A8F99', innerEndBackground: '#8A8F99', relationColor: '#8A8F99', relationLabelColor: '#E2E4E9', relationLabelBackground: '#0F1012'
    }
  });
  for (const block of blocks) {
    const source = block.textContent;
    try {
      await mermaid.parse(source);
    } catch (e) {
      errors.push('Mermaid 語法錯誤（第 ' + block.closest('.slide').dataset.page + ' 頁）：' + String(e.message || e).split('\n')[0]);
      block.className = 'mermaid-error';
      block.textContent = 'Mermaid 語法錯誤\n' + source;
    }
  }
  try { await mermaid.run({ querySelector: 'pre.mermaid' }); } catch (e) { errors.push('Mermaid 渲染失敗：' + String(e.message || e).split('\n')[0]); }
  document.body.setAttribute('data-errors', JSON.stringify(errors));
  document.body.setAttribute('data-ready', '1');
})();
"""


def render_html(slides: list[dict], libs: Path, base_dir: Path) -> str:
    mermaid_js = find_lib(libs, "mermaid.min.js", ("mermaid/dist", "node_modules/mermaid/dist"))
    marked_js = find_lib(libs, "marked.umd.js", ("marked/lib", "node_modules/marked/lib"))
    total = len(slides)
    sections: list[str] = []
    sources: list[str] = []
    for page, slide in enumerate(slides, start=1):
        meta, cols = slide["meta"], slide["cols"]
        layout = meta["layout"]
        ids = [f"s{page}c{i}" for i in range(len(cols))]
        for ident, md in zip(ids, cols):
            sources.append(
                f'<script type="text/markdown" data-target="{ident}">{md.replace("</script>", "<\\/script>")}</script>'
            )
        foot = f'<div class="foot"><span class="src">{html.escape(meta["src"])}</span><span>{page} / {total}</span></div>'
        if layout == "cover":
            sections.append(
                f'<section class="slide cover" data-page="{page}"><div class="bar"></div><div class="tag">{html.escape(meta["tag"])}</div>'
                f'<div class="cover-body" id="{ids[0]}"></div>{foot}</section>'
            )
            continue
        tag = f'<div class="tag">{html.escape(meta["tag"])}</div>' if layout != "diagram" else ""
        title = f'<h2 class="title">{html.escape(slide["title"])}</h2>' if slide["title"] else ""
        col_html = "".join(f'<div class="col" id="{ident}"></div>' for ident in ids)
        kind = "cols-2" if len(cols) > 1 and layout != "stack" else "cols-1"
        style = ""
        if kind == "cols-2" and meta["ratio"]:
            left, _, right = meta["ratio"].partition(":")
            style = f' style="grid-template-columns: minmax(0, {float(left)}fr) minmax(0, {float(right)}fr)"'
        vars_ = ""
        if meta["size"]:
            vars_ += f"--tsize: {int(meta['size'])}px;"
        if meta["dmax"]:
            vars_ += f"--dmax: {int(meta['dmax'])}px;"
        slide_style = f' style="{vars_}"' if vars_ else ""
        sections.append(
            f'<section class="slide {layout}"{slide_style} data-page="{page}">{tag}{title}<div class="body {kind}"{style}>{col_html}</div>{foot}</section>'
        )
    state = json.dumps({"font": FONT_STACK.replace("'", "")})
    return (
        "<!doctype html><html lang=\"zh-Hant-TW\"><head><meta charset=\"UTF-8\">"
        f'<base href="{base_dir.resolve().as_uri()}/">'
        f'<link rel="stylesheet" href="{file_url(FONTS_CSS)}">'
        f"<style>{CSS}</style></head><body>"
        + "".join(sections)
        + "".join(sources)
        + f'<script id="deck-state" type="application/json">{state}</script>'
        + f'<script src="{file_url(marked_js)}"></script><script src="{file_url(mermaid_js)}"></script>'
        + f"<script>{SCRIPT}</script></body></html>"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="把 docs/1.0簡報.md 轉成 PDF")
    parser.add_argument("--libs", default=os.environ.get("DECK_LIBS"), help="含 mermaid.min.js 與 marked.umd.js 的資料夾（或其 node_modules）")
    parser.add_argument("--check", action="store_true", help="只檢查渲染結果，不產生 PDF")
    parser.add_argument("--html", help="另存渲染用的 HTML")
    parser.add_argument("--source", default=str(SOURCE))
    parser.add_argument("--output", default=str(OUTPUT))
    args = parser.parse_args(argv[1:])
    if not args.libs:
        print("請以 --libs 或環境變數 DECK_LIBS 指定 mermaid 與 marked 所在的資料夾（見 build_deck.py 開頭的說明）。", file=sys.stderr)
        return 2
    source = Path(args.source)
    slides = [parse_slide(raw) for raw in split_slides(read_text(source))]
    page_html = render_html(slides, Path(args.libs), source.parent)
    work = Path(args.html) if args.html else Path(os.environ.get("TEMP", ".")) / "deck-render.html"
    write_text(work, page_html)

    dom = html_dump_dom(work, wait_ms=40000)
    ready = 'data-ready="1"' in dom
    match = re.search(r"data-errors='([^']*)'|data-errors=\"([^\"]*)\"", dom)
    errors = json.loads(html.unescape(match.group(1) or match.group(2))) if match else []
    print(f"共 {len(slides)} 頁；渲染完成：{'是' if ready else '否'}；Mermaid 圖 {dom.count('data-processed')} 張；錯誤 {len(errors)} 個")
    for message in errors:
        print("  ✗", message)
    if not ready:
        print("頁面的腳本沒有跑完（可能是 mermaid 或 marked 載入失敗）。", file=sys.stderr)
        return 1
    if errors:
        return 1
    if args.check:
        return 0
    html_to_pdf(work, Path(args.output), wait_ms=40000)
    print(f"已輸出 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
