# 報告素材的產生工具（架構圖解與簡報）

這個資料夾放的是**產生文件素材的工具**，不屬於應用程式（不進映像檔，`.dockerignore` 已排除 `docs`）。它們產生：

| 產出 | 來源 | 產生指令（在專案根目錄執行） |
|---|---|---|
| `docs/登入與註冊架構圖解.png` | `build_diagrams.py` 的 `login_diagram()` | `python -X utf8 docs/diagrams/build_diagrams.py login` |
| `docs/MCP架構圖解.png` | `build_diagrams.py` 的 `mcp_diagram()` | `python -X utf8 docs/diagrams/build_diagrams.py mcp` |
| `docs/1.0簡報.pdf` | `docs/1.0簡報.md` | `python -X utf8 docs/diagrams/build_deck.py --libs <資料夾>` |

兩張圖解與簡報都使用專案的色彩規範（Cinematic Dark Minimalist，見 AGENTS.md 第 2 節）與自行託管的 Noto Sans TC（`app/static/fonts`），不使用任何外部字型或網路資源。

## 需要什麼

- Python 3（只用標準函式庫）與 Chrome 或 Edge（以無頭模式轉成 PNG 與 PDF；找不到時以環境變數 `CHROME_PATH` 指定）。
- 只有簡報的 PDF 需要 `mermaid` 與 `marked`（在瀏覽器裡渲染 Mermaid 與 Markdown）。它們是產生文件用的工具，**不放進版本庫**：

```powershell
npm install --prefix $env:TEMP\deck-libs mermaid@11.4.1 marked@12.0.2
python -X utf8 docs/diagrams/build_deck.py --libs $env:TEMP\deck-libs\node_modules
```

`--check` 只檢查渲染結果（每張 Mermaid 圖的語法、圖片是否載入），不產生 PDF；有任何錯誤就以非 0 結束。

## 檔案

| 檔案 | 用途 |
|---|---|
| `build_diagrams.py` | 兩張 Azure 圖示架構圖的版面與內容。**圖中的函式、端點與資料表名稱取自程式碼**，架構改變時在這裡修改 |
| `svg_kit.py` | 手寫 SVG 的小工具（卡片、膠囊標籤、箭頭、步驟徽章、自動換行） |
| `build_deck.py` | 把 `docs/1.0簡報.md` 依 `---` 分頁，渲染成 16:9 的 PDF（版面指令見檔案開頭的說明） |
| `render_tools.py` | 共用：找 Chrome、HTML 轉 PNG 或 PDF、色彩規範 |
| `icons/` | 圖解用到的 19 個 Azure 圖示（SVG，未經修改） |

## 圖解的讀法

- 橘色圓圈的數字是**流程步驟**，標在用到它的模組與資料表上，對應圖下方的「功能對應」表（步驟 ↔ 程式 ↔ 資料表的讀 R、寫 W、刪 D）。
- 實線是已實作的呼叫；虛線是設定、旁路，或（MCP 圖）**規劃中、尚未實作**的部分。
- 卡片裡「Azure 圖示：…」是圖示名稱，只表示它借用該圖示當元件符號；實際部署為 Google Cloud。
- **MCP 圖的現況**：專案沒有實作 MCP。客服 v1 不提供工具呼叫（AGENTS.md 第 10 節），模型只輸出結構化 JSON，資料存取全由 Service 層函式完成；MCP 只出現在 `docs/RPG.md` R-16 與 RC-6 的規劃（`ExternalDataProvider` ＋ MCP 轉接器，由 Service 發動）。圖中的「MCP 角色對照」是概念對應，不是現況。

## 何時重新產生

登入流程、客服送出流程、分層、資料表名稱或規劃狀態改變時：先改 `build_diagrams.py`（與 `docs/1.0簡報.md`），重新產生三個檔案，目視檢查 PNG 與 PDF，再一起提交。簡報的數字（模組數、測試數、資料表數）取自當時的程式碼，重新產生前請重新量一次。

## Azure 圖示的使用條款

圖示取自 Microsoft 的 Azure Architecture Icons（V25），原壓縮檔附的使用條款摘要：允許用於**架構圖、訓練教材或文件**；建議在圖中標示產品名稱；不得裁切、翻轉、旋轉或變形圖示；不得用 Microsoft 產品圖示代表自己的產品或服務。

本專案據此：圖示未經修改，每個圖示旁標示其名稱，並在圖上註明「僅作元件符號」。若要對外散布，請先自行確認條款（<https://learn.microsoft.com/azure/architecture/icons/>）。
