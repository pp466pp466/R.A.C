# 報告素材的產生工具（架構圖解與簡報）

這個資料夾放的是**產生文件素材的工具**，不屬於應用程式（不進映像檔，`.dockerignore` 已排除 `docs`）。它們產生：

| 產出 | 來源 | 產生指令（在專案根目錄執行） |
|---|---|---|
| `docs/登入與註冊架構圖解.png` | `build_diagrams.py` 的 `login_diagram()` | `python -X utf8 docs/diagrams/build_diagrams.py login` |
| `docs/MCP架構圖解.png` | `build_diagrams.py` 的 `mcp_diagram()` | `python -X utf8 docs/diagrams/build_diagrams.py mcp` |
| `docs/網站入口架構圖解.png` | `build_diagrams_system.py` 的 `entry_diagram()` | `python -X utf8 docs/diagrams/build_diagrams_system.py entry` |
| `docs/RPG回合引擎架構圖解.png` | `build_diagrams_system.py` 的 `rpg_diagram()` | `python -X utf8 docs/diagrams/build_diagrams_system.py rpg` |
| `docs/內容安全與審查架構圖解.png` | `build_diagrams_system.py` 的 `safety_diagram()` | `python -X utf8 docs/diagrams/build_diagrams_system.py safety` |
| `docs/部署與營運架構圖解.png` | `build_diagrams_system.py` 的 `deploy_diagram()` | `python -X utf8 docs/diagrams/build_diagrams_system.py deploy` |
| `docs/2.0簡報.pdf` | `docs/2.0簡報.md` | `python -X utf8 docs/diagrams/build_deck.py --libs <資料夾>` |
| `docs/2.1簡報.pptx` | `docs/2.0簡報.md`（與 PDF 同一份內容） | `python -X utf8 docs/diagrams/build_pptx.py --libs <資料夾> --pptx-libs <資料夾>` |
| `docs/4.0簡報.pptx` | `build_pptx_40.py` 內手工編排的 15 張重點版（給主管與評審；2026-11-22 的快照，不再更新） | `python -X utf8 docs/diagrams/build_pptx_40.py --pptx-libs <資料夾>` |
| `docs/5.0簡報.pptx` | `build_pptx_50.py` 內手工編排的 16 張重點版（給主管與評審；2026-11-24 的快照，當天補入劇情朗讀後定稿、不再更新；含一張「開發工具」） | `python -X utf8 docs/diagrams/build_pptx_50.py --pptx-libs <資料夾>` |

`build_diagrams.py` 與 `build_diagrams_system.py` 不帶參數（或帶 `all`）時產生各自負責的全部圖。上一版簡報（`1.0簡報.md` 與 PDF）保留為歷史版本，要重新產生用 `--source docs/1.0簡報.md --output docs/1.0簡報.pdf`。

六張圖解與簡報都使用專案的色彩規範（Cinematic Dark Minimalist，見 AGENTS.md 第 2 節）與自行託管的 Noto Sans TC（`app/static/fonts`），不使用任何外部字型或網路資源。

**所有圖解都輸出為 4K（UHD）**：版面寬度固定 1800，輸出時以 3840 / 1800 的倍率放大，成品寬度剛好是 **3840 像素**，高度依內容而定（約 3800 至 4900 像素）。

## 需要什麼

- Python 3（只用標準函式庫）與 Chrome 或 Edge（以無頭模式轉成 PNG 與 PDF；找不到時以環境變數 `CHROME_PATH` 指定）。
- 只有簡報的 PDF 與 pptx 需要 `mermaid` 與 `marked`（在瀏覽器裡渲染 Mermaid 與 Markdown）。它們是產生文件用的工具，**不放進版本庫**：

```powershell
npm install --prefix $env:TEMP\deck-libs mermaid@11.4.1 marked@12.0.2
python -X utf8 docs/diagrams/build_deck.py --libs $env:TEMP\deck-libs\node_modules
```

`--check` 只檢查渲染結果（每張 Mermaid 圖的語法、圖片是否載入），不產生 PDF；有任何錯誤就以非 0 結束。

**PowerPoint（`2.1簡報.pptx`）** 另外需要 `python-pptx`（同樣只在產生文件時用到，不放進版本庫與 `requirements*.txt`）：

```powershell
pip install python-pptx --target $env:TEMP\pptx-libs
python -X utf8 docs/diagrams/build_pptx.py --libs $env:TEMP\deck-libs\node_modules --pptx-libs $env:TEMP\pptx-libs
```

pptx 與 PDF 是**同一份** `docs/2.0簡報.md`：分頁與版面指令沿用 `build_deck.py` 的解析；6 張 4K 圖解用原本的 PNG（含 `crop`），Mermaid 圖是與 PDF 相同的渲染結果（由 Chrome 把每張圖的 SVG 截成 PNG）；標題、段落、項目符號與表格是 PowerPoint 的原生元素，可以編輯。封面的版本字樣是 `--label`（預設 2.1），內文裡的「2.0」是歷史事實，不改。字型用 Microsoft JhengHei（Noto Sans TC 是網頁用的自託管切片，無法內嵌）。版面以字數估算後排進 1280 x 720，放不下時會逐步縮小字級（最小 0.62 倍），仍放不下的頁面會列在輸出裡、結束代碼為 3：**有版面問題時不要自己改版面，先回報**。產生後建議用 PowerPoint 實際開啟檢查（PowerShell 的 `$ppt.Presentations.Open(...)` 加 `Export(資料夾, "PNG", 1280, 720)` 可以匯出每一頁的圖片）。

**4.0 簡報（`4.0簡報.pptx`）** 是把 2.0 簡報縮成 15 張、只放重點的 PowerPoint，給主管與評審：內文不小於 24 pt、標題 44 pt、深色高對比（不用偏淡的次要灰），投影幕上也看得清楚。與 2.1 不同，它是**手工編排**（內容寫在 `build_pptx_40.py` 的各個 `slide_*` 函式，不解析 Markdown）：圖是 PowerPoint 的原生形狀（不嵌入 4K 圖解，圖中的字太小），每張有講者備忘放被刪掉的細節。只需要 `python-pptx`（同上，不放進版本庫）：

```powershell
python -X utf8 docs/diagrams/build_pptx_40.py --pptx-libs $env:TEMP\pptx-libs
python -X utf8 docs/diagrams/build_pptx_40.py --pptx-libs $env:TEMP\pptx-libs --check   # 只檢查，不寫檔
```

`--check` 檢查兩件事：①以字數估算每個文字框的高度，放不下的列出來（結束代碼 3；**不要自己縮字級，先回報**）；②`FACTS` 裡的數字與說法是否都出現在 `docs/2.0簡報.md`。**4.0 是 2026-11-22 的快照**：2.0 簡報更新到 2026-11-24 之後，它的 `FACTS`（如 14,677、1,610、99 份主規格）已不在 2.0 簡報裡，`--check` 的數字檢查會失敗，這是預期的，不要為了讓它通過而改 4.0；5.0 日後也是同樣的情形。

**5.0 簡報（`5.0簡報.pptx`）** 是 4.0 的後繼版：版型與規則相同（16:9、內文不小於 24 pt、標題 44 pt、原生形狀、講者備忘），內容更新到 2026-11-24（遊戲畫面的劇情卡與行動建議、設定精靈、後台刪除帳號、103 份主規格與 35 個已封存 change、測試數字、展示環境 `b63f4e7`、劇情朗讀〔rpg-story-tts，已部署待驗收〕），並新增第 12 張「開發工具：用了什麼、做了什麼」（Claude Code、OpenSpec、測試與變異驗證、Playwright、AWS CLI＋SSM、Mermaid＋python-pptx）。`build_pptx_50.py` 是由 4.0 的產生器複製後修改的獨立檔案。**5.0 與 4.0 一樣是快照**：不隨 2.0 更新，也不納入 `deck-conflict-watch` 技能的監看（使用者 2026-11-24 的決定）；程式與 2.0 簡報更新之後若需要新的重點彙整報告，由使用者另外要求，產生新編號的新檔（可沿用這套版型），4.0 與 5.0 原樣保留（唯一的例外：2026-11-24 使用者要求把劇情朗讀補入 2.0 與 5.0，5.0 因此在當天再更新一次、數字與 2.0 對齊，`--check` 通過）。產生與檢查指令（`--check` 對照的是**產生當天**的 2.0 簡報）：

```powershell
python -X utf8 docs/diagrams/build_pptx_50.py --pptx-libs $env:TEMP\pptx-libs
python -X utf8 docs/diagrams/build_pptx_50.py --pptx-libs $env:TEMP\pptx-libs --check   # 只檢查，不寫檔
```

## 檔案

| 檔案 | 用途 |
|---|---|
| `build_diagrams.py` | 登入與 MCP 兩張 Azure 圖示架構圖的版面與內容。**圖中的函式、端點與資料表名稱取自程式碼**，架構改變時在這裡修改 |
| `build_diagrams_system.py` | 網站入口、RPG 回合引擎、內容安全與審查、部署與營運四張系統層級的圖（同上，名稱取自程式碼與設計文件） |
| `diagram_common.py` | 兩個圖檔共用的版面元件：標題列、「功能對應」表、色帶、頁面外殼與 4K PNG 輸出 |
| `svg_kit.py` | 手寫 SVG 的小工具（卡片、膠囊標籤、箭頭、步驟徽章、自動換行） |
| `build_deck.py` | 把 `docs/2.0簡報.md` 依 `---` 分頁，渲染成 16:9 的 PDF（版面指令見檔案開頭的說明） |
| `build_pptx.py` | 把同一份簡報轉成 16:9 的 PowerPoint（`docs/2.1簡報.pptx`）：沿用 `build_deck.py` 的解析與 Mermaid 渲染，文字與表格是原生元素 |
| `build_pptx_40.py` | 手工編排的 15 張重點版 PowerPoint（`docs/4.0簡報.pptx`，2026-11-22 的快照）：大字級、深色高對比、原生形狀與講者備忘 |
| `build_pptx_50.py` | 手工編排的 16 張重點版 PowerPoint（`docs/5.0簡報.pptx`，2026-11-24 的快照）：沿用 4.0 的版型，內容更新到 2026-11-24 並新增「開發工具」頁 |
| `render_tools.py` | 共用：找 Chrome、HTML 轉 PNG 或 PDF、色彩規範 |
| `icons/` | 圖解用到的 19 個 Azure 圖示（SVG，未經修改） |

## 圖解的讀法

- 橘色圓圈的數字是**流程步驟**，標在用到它的模組與資料表上，對應圖下方的「功能對應」表（步驟 ↔ 程式 ↔ 資料表的讀 R、寫 W、刪 D）。
- 實線是已實作的呼叫；虛線是設定、旁路，或（MCP 圖、內容安全圖、部署圖）**規劃中、尚未實作**的部分。
- 卡片裡「Azure 圖示：…」是圖示名稱，只表示它借用該圖示當元件符號；實際部署為 AWS（展示環境）與規劃中的 Google Cloud。
- **MCP 圖的現況**：專案沒有實作 MCP。客服 v1 不提供工具呼叫（AGENTS.md 第 10 節），模型只輸出結構化 JSON，資料存取全由 Service 層函式完成；MCP 只出現在 `docs/RPG.md` R-16 與 RC-6 的規劃（`ExternalDataProvider` ＋ MCP 轉接器，由 Service 發動）。圖中的「MCP 角色對照」是概念對應，不是現況。
- **內容安全圖的現況與規範**：圖上方的聲明列出兩者的差異：規範（AGENTS.md，2026-11-17 修訂）是「安全模式開／關」兩種；程式現況仍是 safe／standard／adult 三種，改為兩種屬 `safe-mode-toggle`（尚未實作）。
- **部署圖的範圍**：展示環境是 AWS 雪梨的單機部署，不是 production；Google Cloud 的 production 只是規劃，尚未建立。

## 簡報如何引用圖解

`docs/2.0簡報.md` 以 `![說明](圖檔.png "crop=起-迄")` 引用圖解，`crop` 是圖片高度的百分比區間（例如 `0-0.4735` 只顯示最上方 47% 的架構區）。**圖解的版面改變時，簡報裡的 `crop` 要跟著調整**：各張圖的「架構區高度／全圖高度」會在產生圖解時印在終端機（入口圖另印導向決策區的位置），把它換算成比例即可；調整後執行 `build_deck.py --check`，並目視檢查 PDF。

## 何時重新產生

登入流程、客服送出流程、網站入口與入口層設定、RPG 回合流程、內容安全規則、部署與營運、分層、資料表名稱或規劃狀態改變時：先改 `build_diagrams.py` 或 `build_diagrams_system.py`（與 `docs/2.0簡報.md`），重新產生圖解與 PDF，目視檢查 PNG 與 PDF，再一起提交。簡報的數字（模組數、測試數、資料表數）取自當時的程式碼，重新產生前請重新量一次。

## Azure 圖示的使用條款

圖示取自 Microsoft 的 Azure Architecture Icons（V25），原壓縮檔附的使用條款摘要：允許用於**架構圖、訓練教材或文件**；建議在圖中標示產品名稱；不得裁切、翻轉、旋轉或變形圖示；不得用 Microsoft 產品圖示代表自己的產品或服務。

本專案據此：圖示未經修改，每個圖示旁標示其名稱，並在圖上註明「僅作元件符號」。若要對外散布，請先自行確認條款（<https://learn.microsoft.com/azure/architecture/icons/>）。
