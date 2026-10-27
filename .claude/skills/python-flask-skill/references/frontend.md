# 前端離線架構與 UI/UX

對應 AGENTS.md 第 1、2 節。

## 離線架構（嚴禁使用 CDN）

- 所有第三方前端庫（Vue 3, Bootstrap 5.3, Axios）一律下載至本機，存放於 Flask 的 static/vendor/ 目錄中。
- 字型檔同樣必須本地化，存放於 Flask 的 static/fonts/ 目錄中，嚴禁引入 Google Fonts 等外部字型服務。預設字型為 Noto Sans TC（SIL OFL 授權），以 woff2 並依字元範圍 (unicode-range) 切片後自行託管，後備字型為 Microsoft JhengHei、PingFang TC。
- HTML 模板中引入資源時，必須一律使用 Flask 的 {{ url_for('static', filename='...') }} 語法。

## 前端技術棧

- MVVM 框架：採用 Vue 3.0 JavaScript。不使用 Vue 單檔元件（.vue）與 Vite 打包架構，一律採用全域變數模式（如 const { createApp, ref } = Vue）在 HTML 內編寫，嚴禁生成 .vue 單一檔案元件。
- 資料互動：一般 API 互動採用離線引入的 axios；LLM 文字串流回應則採用原生 fetch 搭配 ReadableStream（見 [llm-streaming.md](llm-streaming.md)）。
- RWD 框架：採用 Bootstrap 5.3，深色模式以 data-bs-theme="dark" 全域啟用並覆蓋樣式。

## 色彩規範 (Color Palette)

風格：Cinematic Dark Minimalist（極簡黑暗電影 / 文字雜誌風）。

1. 主背景 (Main Background): #0F1012 (劇院深黑 Obsidian Black)
2. 卡片/容器 (Container/Glass): rgba(28, 30, 35, 0.75) (半透明微光 Frosted Charcoal)
3. 主要文字 (Primary Text): #E2E4E9 (薄霧灰白 Muted Off-White，嚴禁純白 #FFFFFF)
4. 次要/副標文字 (Secondary Text): #8A8F99 (鋼鐵灰 Steel Grey)
5. 焦點/互動 (Accent/Hover): #FF6B35 (導演琥珀橙 Director's Amber)
6. 極細分隔線 (Borders/Lines): #2A2D34 (1px 極細幽暗金屬線)

## 自製彈窗與即時提示 (Modals & Toasts - 框架衝突防呆)

- 嚴禁使用外部彈窗套件（如 SweetAlert2），一律以 Vue 3 搭配 Bootstrap 5.3 自製電影感 Modal 與 Toast。
- Vue 狀態完全接管（關鍵防呆）：
  - Modal、Toast 與 Sidebar 之顯示/隱藏狀態一律由 Vue 3 響應式變數（v-if / v-show）完全接管。
  - 僅借用 Bootstrap 5.3 的 CSS 網格與排版類別（如 container, row, col, flex），嚴禁調用 Bootstrap 原生的 JavaScript 外掛（含 offcanvas）與 data-bs-* 互動邏輯（如 data-bs-toggle、data-bs-dismiss），避免 DOM 控制權衝突。唯一例外為深色模式開關 data-bs-theme="dark"。
- Modal 遮罩與容器樣式：
  - 全螢幕背景遮罩採用深黑半透明加高斯模糊：background: rgba(15, 16, 18, 0.85); backdrop-filter: blur(8px);。
  - Modal Container 背景為 rgba(22, 24, 28, 0.95)，四周邊框為 1px solid #2A2D34，左上角標註極小系統狀態碼（如 [ SYSTEM_PROMPT ]）。
- Toast 即時提示：靠右下角出現，具備 2px 左側琥珀橙色指示線（border-left: 2px solid #FF6B35），由下往上淡入。

## 客服介面：Sidebar 與建議動作

對應 AGENTS.md 第 7 節。

- 類 ChatGPT 介面：具備可開關的左側歷史對話 Sidebar (寬度 280px) 與會員等級標籤。桌面（寬度 992px 以上）預設開啟並推擠主區域，開關狀態記於 localStorage（讀寫須包 try/catch，失敗時退回預設值）；手機與平板（寬度未滿 992px）預設關閉，以覆蓋方式顯示並附遮罩，不記住狀態。
- 建議動作呈現：聊天視窗只顯示 reply_text（純文字），intent 與 entities 不顯示給一般使用者。suggested_actions 以快速回覆 chips 呈現於最後一則 AI 訊息下方，打字機渲染結束後淡入，一次最多 4 個；樣式為膠囊形、border: 1px solid #2A2D34、背景透明、文字 #8A8F99，hover 時邊框與文字改為 #FF6B35；點擊後以其 text 作為使用者訊息送出，並以 v-if 移除該組 chips。
