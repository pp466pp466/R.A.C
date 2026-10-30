# 專案架構與開發規範

本專案為一個以 LLM 驅動的「開放世界文字 RPG 平台」與「會員制智能客服機器人系統」之雙核心平台 (Dual-Core Platform)。開發時請嚴格遵守以下架構與技術規範：

---

## 1. 前端離線架構（嚴禁使用 CDN）
- 所有第三方前端庫（Vue 3, Bootstrap 5.3, Axios）一律下載至本機，存放於 Flask 的 static/vendor/ 目錄中。
- 字型檔同樣必須本地化，存放於 Flask 的 static/fonts/ 目錄中，嚴禁引入 Google Fonts 等外部字型服務。預設字型為 Noto Sans TC（SIL OFL 授權），以 woff2 並依字元範圍 (unicode-range) 切片後自行託管，後備字型為 Microsoft JhengHei、PingFang TC。
- HTML 模板中引入資源時，必須一律使用 Flask 的 {{ url_for('static', filename='...') }} 語法。

---

## 2. 前端技術棧 (UI/UX & Cinematic Vibe)
- MVVM 框架：採用 Vue 3.0 JavaScript。不使用 Vue 單檔元件（.vue）與 Vite 打包架構，一律採用全域變數模式（如 const { createApp, ref } = Vue）在 HTML 內編寫，嚴禁生成 .vue 單一檔案元件。
- 資料互動：一般 API 互動採用離線引入的 axios；LLM 文字串流回應則採用原生 fetch 搭配 ReadableStream。
- RWD 框架：採用 Bootstrap 5.3，深色模式以 data-bs-theme="dark" 全域啟用並覆蓋樣式。

### Color Palette (色彩規範)
風格：Cinematic Dark Minimalist（極簡黑暗電影 / 文字雜誌風）。

1. 主背景 (Main Background): #0F1012 (劇院深黑 Obsidian Black)
2. 卡片/容器 (Container/Glass): rgba(28, 30, 35, 0.75) (半透明微光 Frosted Charcoal)
3. 主要文字 (Primary Text): #E2E4E9 (薄霧灰白 Muted Off-White，嚴禁純白 #FFFFFF)
4. 次要/副標文字 (Secondary Text): #8A8F99 (鋼鐵灰 Steel Grey)
5. 焦點/互動 (Accent/Hover): #FF6B35 (導演琥珀橙 Director's Amber)
6. 極細分隔線 (Borders/Lines): #2A2D34 (1px 極細幽暗金屬線)

### 自製彈窗與即時提示 (Modals & Toasts - 框架衝突防呆)
- 嚴禁使用外部彈窗套件（如 SweetAlert2），一律以 Vue 3 搭配 Bootstrap 5.3 自製電影感 Modal 與 Toast。
- Vue 狀態完全接管（關鍵防呆）：
  - Modal、Toast 與 Sidebar 之顯示/隱藏狀態一律由 Vue 3 響應式變數（v-if / v-show）完全接管。
  - 僅借用 Bootstrap 5.3 的 CSS 網格與排版類別（如 container, row, col, flex），嚴禁調用 Bootstrap 原生的 JavaScript 外掛（含 offcanvas）與 data-bs-* 互動邏輯（如 data-bs-toggle、data-bs-dismiss），避免 DOM 控制權衝突。唯一例外為深色模式開關 data-bs-theme="dark"。
- Modal 遮罩與容器樣式：
  - 全螢幕背景遮罩採用深黑半透明加高斯模糊：background: rgba(15, 16, 18, 0.85); backdrop-filter: blur(8px);。
  - Modal Container 背景為 rgba(22, 24, 28, 0.95)，四周邊框為 1px solid #2A2D34，左上角標註極小系統狀態碼（如 [ SYSTEM_PROMPT ]）。
- Toast 即時提示：靠右下角出現，具備 2px 左側琥珀橙色指示線（border-left: 2px solid #FF6B35），由下往上淡入。

---

## 3. 後端架構（關注點分離）
- 專案結構劃分：MVC Controller 層、Web API Controller 層、Service 層、Repository 層。
- Controller 分流：
  - 【MVC Page Controller】：僅負責渲染基本 HTML 頁面外殼與初始狀態傳遞（包含 /game 與 /cs 兩套視圖，以及 /login、/register 等帳號頁面）。
  - 【Web API Controller】：一律回傳 JSON 格式，專門處理 Ajax 請求與 API 服務（SSE 串流端點除外，見第 5 節）。包含 /api/auth/*、/api/cs/*，以及不需登入的 /healthz（存活）與 /readyz（含資料庫檢查），兩者不得回傳內部細節。
- Service 層：負責 RPG 核心邏輯、強約束事件錨點 (Milestones) 驗證、客服 NLP 意圖解析 (Intent/Entity)、客服對話摘要壓縮與 RPG 記事簿重點抓取、Gemini API 呼叫，以及身分驗證與資料存取政策檢查 (AccessPolicy，見第 10 節)。
- Repository 層：負責 SQLAlchemy CRUD 操作，封裝 ORM Session 生命週期與 JSON 序列化。

---

## 4. 後端資料庫與 SQLAlchemy ORM 規範（SQLite 與 SQL Server 雙軌相容）

- 純 ORM 抽象原則：業務邏輯與 Repository 層一律透過 SQLAlchemy ORM 模型進行操作，嚴禁撰寫任何 Raw SQL（原生語法），確保雙軌切換相容性。唯一例外為下方的資料庫連線初始化事件（基礎設施層）。
- 條件式初始化與方言隔離 (Dialect Check)：
  - 建立 Engine 後，若 engine.dialect.name == "sqlite"，才透過 SQLAlchemy 的連線事件（event.listen(engine, "connect", ...)）於底層連線建立時自動執行 PRAGMA journal_mode=WAL;（預寫日誌模式）、PRAGMA foreign_keys=ON;（SQLite 預設不強制外鍵）與 PRAGMA busy_timeout（皆僅限此事件內），不得在業務程式碼中直接呼叫。
  - 若底層為 SQL Server，則不註冊該事件，改由 SQLAlchemy 的連線池與交易管理接管並發與非同步，並設定連線池參數（如 pool_pre_ping、pool_recycle）以避免閒置斷線。
- JSON 欄位與資料序列化：高動態記憶體與設定（如 characters_json, milestones_json, entities_json 等）一律採用 SQLAlchemy 的 JSON 型態，由 Repository 層負責讀寫時的序列化與反序列化。不得依 JSON 欄位內容做篩選查詢（SQLite 與 SQL Server 的 JSON 查詢語法不同，會破壞雙軌相容）。
- 字串欄位編碼：一般文字欄位一律使用 SQLAlchemy 的 Unicode / UnicodeText 型別（SQL Server 對應 NVARCHAR），確保雙軌環境中文不亂碼。
- 唯一約束與大小寫：SQLite 的字串比對預設區分大小寫、SQL Server 預設不區分。具唯一約束的字串（如 email）一律在 Service 層正規化（email 轉小寫）後再寫入與查詢，使兩種資料庫行為一致。
- 可為空的唯一欄位：SQL Server 的 UNIQUE 約束只允許一個 NULL，SQLite 則允許多個，直接設定 unique=True 會讓雙軌行為分歧。可為空且需要唯一的欄位一律改用「只涵蓋非 NULL 列」的部分唯一索引（sqlite_where 與 mssql_where，以 ORM 表達式建立，不得使用原生 SQL 字串；專案提供 unique_when_not_null()），且該欄位本身不再設 unique=True。
- 對外識別碼：對外暴露的資源識別碼（網址與 API）使用 UUID（如 public_id），內部整數主鍵不對外；但授權不得依賴識別碼的不可猜測性（見第 10 節）。
- 資料表變更 (Migration)：
  - 使用 Alembic（啟用 render_as_batch 以相容 SQLite），正式環境嚴禁使用 create_all() 建表。
  - migration 腳本使用 Alembic op API 與 ORM 模型，不得含方言專屬的原生 SQL（含 op.execute 的原生語法）。
  - alembic.ini 與 migration 範本 (script.py.mako) 必須是純 ASCII：Alembic 以系統地區編碼讀取它們（繁體中文 Windows 為 cp950），中文字元會造成解析失敗。alembic.ini 不得含連線字串，連線一律取自 DATABASE_URL；migration 腳本 (.py) 不受影響，仍使用 UTF-8 與繁體中文註解。
  - 升級採 expand/contract：新增欄位先設為可為空，部署相容新舊結構的程式後再回填與加上約束；任何版本的程式碼都必須能在「目前結構」與「下一版結構」上運作。
  - migration 為部署流程中獨立的步驟，於新版程式上線之前執行（執行前先備份），不得在應用程式啟動時自動執行。

---

## 5. LLM 即時串流響應與打字機特效 (SSE Streaming)
- 後端 SSE 串流端點（RPG 適用）：針對 LLM 文字生成（開放世界 RPG 劇情），應採用 flask.Response 結合 mimetype='text/event-stream' 進行 Server-Sent Events (SSE) 串流推送。
- SSE 心跳與斷線偵測（RPG 適用）：後端須定期送出心跳以維持連線；前端以任何收到的位元組重設閒置計時器，逾時即中止；後端偵測到客戶端斷線時須停止生成並關閉上游模型串流（RPG 為 OpenAI 相容端點）。間隔、事件協定與逾時數值依 docs/RPG.md。

### LLM 模型選用與調用規範 (Model Configuration)
- 預設核心模型：客服（會員制智能客服對話與客服對話摘要壓縮）使用 Gemini，預設調用 gemini-3.8-flash（環境變數 GEMINI_MODEL）；RPG 用途（開放世界文字 RPG 劇情生成、SSE 串流打字機特效與記事簿重點抓取）使用 OpenAI 相容端點，模型為環境變數 OPENROUTER_MODEL（備援為 OPENROUTER_FALLBACK_MODEL），不使用 Gemini；只有客服使用 GEMINI_MODEL。兩者皆以確保最低的首字延遲（TTFT）與高性價比為目標。模型名稱為環境變數設定值，不寫死於程式，且可依用途指定不同模型；每次呼叫實際使用的模型須記入 llm_call_logs。
- 備援模型：主要模型發生 429、5xx 或單次呼叫逾時 20 秒時，一律切換到備援模型：客服為 gemini-3.5-flash-lite（GEMINI_FALLBACK_MODEL），RPG 為 OPENROUTER_FALLBACK_MODEL（RPG 不切換到 Gemini），皆為環境變數設定值，切換不等待 retryDelay；安全過濾阻擋與 400、403（設定錯誤）不切換。備援呼叫的逾時取後端單次請求總預算的剩餘時間，備援模型也失敗時回覆降級文案。串流呼叫的切換時機依 docs/RPG.md。
- 結構化輸出 (Structured Outputs)：客服模組進行意圖與實體解析時，必須以非串流請求強制約束 Gemini API 輸出標準 JSON 格式（至少包含 intent, confidence, entities, reply_text, suggested_actions），並使用 Gemini 的 response schema 強制格式；Service 層仍須重新驗證，不信任模型輸出。嚴禁將 JSON 原文以串流方式送達前端。
- LLM JSON 解析：解析前須去除 Markdown 程式碼區塊標記（如 ```json）；客服使用 Gemini 結構化輸出模式降低發生率，RPG 的 OpenAI 相容端點依 OPENROUTER_JSON_SCHEMA 是否支援 JSON schema 決定（不支援時以提示詞約束，仍由本函式驗證）。解析統一由單一 Service 層函式處理，依序做 JSON 解析、schema 驗證與語意驗證。失敗時最多重試 1 次（總共最多 2 次呼叫；JSON 解析或驗證失敗時以同一模型重試；暫時性錯誤 429、5xx、逾時則改為切換到備援模型，這次切換視為該次重試，不受「已耗時小於 15 秒」的限制；內容被安全過濾阻擋時不重試，RPG 劇情的被擋回合另見第 10 節）。仍失敗則回覆繁體中文降級文案，該訊息標記為 fallback 且不計入每日配額；失敗的模型原文截斷至 2000 字，記錄於 llm_call_logs.error_detail，保存 14 天，僅管理員可讀。
- 前端串流接收：RPG 劇情的 LLM 串流端點改用瀏覽器原生 fetch 搭配 ReadableStream 接收片段文字；客服模組因採非串流，前端直接取得完整 reply_text；其餘一般 API 仍使用離線 axios。
- UI 平滑渲染：前端接收到片段文字（或客服的完整 reply_text）後，透過打字機緩衝區 (Typewriter Buffer) 逐字平滑渲染，確保流暢度。
- 串流逾時與中斷（RPG 串流適用）：前端串流必須設定逾時，並以 AbortController 中止請求。無論成功、失敗或中斷，皆須在 finally 中恢復 UI 狀態（解除按鈕禁用、清除載入動畫）。後端於串流結束時送出明確的結束標記，前端以此判定已完整接收。
- 非串流請求逾時與中斷（客服適用）：Gemini 單次呼叫逾時 20 秒（逾時即切換備援模型），後端單次請求總預算（含重試與切換備援）30 秒；前端 axios 逾時 35 秒（略大於後端總預算），並以 AbortController 於使用者取消、切換對話或離開頁面時中止；無論成功、失敗或中斷，皆須在 finally 中恢復 UI 狀態。前端中止不代表後端中止，後端仍會完成並寫入結果，由 since_seq 同步補齊。

---

## 6. 對話歷史記憶體與滑動視窗管理 (Memory Management)
- 一輪定義：一輪 = 一則玩家訊息 + 一則 AI 回應。
- 前端暫存與同步：前端利用 IndexedDB 暫存完整對話與世界線，連線正常時透過背景 Axios 非同步同步至後端永久儲存庫。同步衝突時以後端永久儲存庫為準；前端僅保留尚未同步成功的待送紀錄。
- 同步細則：聊天訊息為 append-only，前端為每則訊息產生唯一 ID，後端以此去重（冪等）。可變的世界狀態須帶版本號（version），寫入時比對版本，不一致時以後端為準，並通知前端。
- 客服對話同步補充：
  - 客服以後端為權威：訊息由後端先寫入資料庫；離線時前端只能閱讀 IndexedDB 快取的歷史並保存待送訊息，產生新回覆需要連線。
  - 後端為每則訊息指派單調遞增的 seq，顯示順序以 seq 為準，不使用前端時鐘。
  - 冪等呼叫：以 (conversation_id, client_msg_id) 查到已處理的訊息，直接回傳既有回覆，不得重複呼叫 Gemini。
  - 前端以 since_seq 增量拉取後端訊息（換裝置、重新整理或前端中止後補齊）。
  - 客服「當前狀態」(state_json) 包含 member_tier、last_intent、pending_entities、open_issue，並帶 state_version；不放入使用者 email、完整個資或其他對話內容。
- 滑動視窗限制 (Sliding Window)：單次發送給 Gemini API 的 Context 嚴格限制為「長期記憶 + 最近 10 輪原文 + 當前世界狀態」。客服的長期記憶是摘要：「摘要 + 最近 10 輪原文 + 當前世界狀態」；RPG 的長期記憶是記事簿（見下方），**RPG 不使用摘要**：「記事簿 + 最近 10 輪原文 + 當前世界狀態」。RPG 的「當前世界狀態」包含在場 NPC 的角色卡切片（特質、表層或隱藏的歸屬、弧線階段、錨定事實），其大小須有上限，且特質與階段不得只存在於記事簿中（記事簿可能已滿，也可被玩家編輯）。
- 摘要機制 (Memory Log，客服)：後端 Service 層於同步歷史紀錄時，若對話超出最近 10 輪，即將最舊的一輪併入既有摘要並壓縮，避免 Token 爆量與延遲。摘要以第 5 節設定的模型產生，並儲存於後端。客服採補償式摘要：以 summary_upto_seq 標記已摘要的範圍並存於資料庫，回覆寫入後觸發；觸發失敗或 worker 重啟時，由下次請求補做。
- RPG 記事簿 (Session Notebook)：RPG 以記事簿取代摘要，保存長期設定，使劇情不偏離先前確立的設定。每個存檔（session）一本，新存檔的記事簿為空（0 記憶），不受其他存檔影響，隨存檔刪除而刪除。容量依會員等級為 free 2000、plus 5000、vip 10000 字（Unicode 字元，NFC 正規化後計），另有容量 25% 的預備空間（500 / 1250 / 2500 字），預備空間只供 AI 寫入。每完成 10 輪（第 10、20、30… 輪，中斷的回合不計），後端以 RPG 用途的模型（第 5 節的 OPENROUTER_MODEL，備援 OPENROUTER_FALLBACK_MODEL，不使用 Gemini），從最近 10 輪抓取重點，只在記事簿末尾追加新條目，不改寫既有內容；新條目超過剩餘空間時仍完整寫入，但總長不得超過容量加預備空間（超過時要求精簡並重試 1 次，仍超過則放棄本次）；記事簿長度已超過容量時為「已滿」：不呼叫模型、不補抓被跳過的輪次，AI 仍帶最近 10 輪，所以可能遺忘（這是刻意的會員差異，等級僅影響額度，不影響權限）。以 notebook_upto_turn 標記已處理到的輪次，回覆寫入後觸發；觸發失敗或 worker 重啟時，由下次請求補做。記事簿以資料區塊放在使用者內容區，system_instruction 只放固定的使用說明（記事簿是已確立的設定，劇情不得與之矛盾，玩家明確改變設定時以最新為準）。
- 記事簿的玩家檢視與編輯：玩家可檢視記事簿，按編輯後可自行修改，儲存後的長度不得超過容量，沒有預備空間；長度已超過容量時（例如 2240 字），須一次刪到容量以內才能儲存與恢復 AI 更新。以 notebook_version 偵測與 AI 寫入的衝突，不一致時以後端為準並通知前端。降級後若超過新容量，不自動刪除，視同已滿；升級後立即有新容量，AI 於下個 10 輪起恢復更新。介面須顯示用量（如「1800 / 2000」）與已滿提示，說明之後的設定不再被記住、AI 可能遺忘；內容一律以純文字渲染，嚴禁 v-html。

---

## 7. 雙核心業務邏輯規範 (Dual-Core Business Spec)
- 開放世界 RPG 模組：
  - 無硬性結局 (Endless Open World)：廢除傳統死亡死局，採用「無窮盡開放世界 + 狀態轉移框架 (State Transition Framework)」。
  - 動態階段錨點：透過 milestones_json 驅動階段性事件（如 20 歲宗門大測），觸發後由 LLM 動態延伸新目標，玩家離線時對話 Session 自動存檔。
  - 原始劇本（`scripts.milestones_json`）為唯讀。LLM 動態延伸的目標不得寫回原始劇本，應另存於 session 層，完成後歸檔。
  - NPC 角色卡：NPC 的特質（表層或隱藏）、弧線階段（surface、cracking、revealed）與錨定事實存放於 sessions.entities_json，每回合進入世界狀態；階段由模型提議、Service 驗證轉移，不由模型直接寫入。個性的玩家層修正存放於 user_persona_overrides（跨存檔共用，只存差異），其餘內容（人物、背景、世界觀、地點）一律屬於單一存檔。全站性格原型庫（persona_prototypes）玩家唯讀，由 AI 擬草案、管理員核可後上架，核可上架的原型不得含可回溯到使用者的資料。
  - 內容分級：存檔帶 content_tier 與 sexual_content_level（0 至 3）旗標，預設最保守；sexual_content_level 3（成年角色之間合意的露骨內容）須已取得所用模型供應商的書面許可與法務意見（許可涵蓋的供應商、模型與日期須記錄），且只能以環境設定開啟：預設關閉，development 可開啟供個人測試，staging 與 production 須明確設定才生效，不得在程式中寫死為開啟；對一般會員開放前，須先於第 10 節訂定強化成年驗證的做法，在那之前不得對一般會員開啟。內容上限由 AccessPolicy 依使用者年齡決定（年齡由生日計算，見第 10 節）：未滿 18 歲者一律為安全模式，sexual_content_level 固定為 0（content_tier 同樣取最保守值），不論存檔旗標為何都不得提高，介面須顯示提示說明原因；成年者由玩家主動在 sexual_content_level 1（浪漫、曖昧）與 2（感官但不露骨）之間切換，預設從 0 開始，當玩家輸入曖昧內容而目前分級較低時，系統只提醒玩家可切換分級，不得自動調高；存檔旗標只能在上限之內調整。安全模式由伺服器端的 system_instruction 與安全設定套用，不依賴模型自行判斷使用者的年齡。無論分級，一律禁止涉及未成年（含看似未成年的角色）的性內容、性暴力與非自願的性情節，以及以真人名義的性內容；露骨場景中的每個角色須有成年年齡紀錄。上述禁止項須由伺服器端自有的審查層執行，不得僅依賴模型拒絕；使用不具拒絕行為的模型時，此層為必要條件。
- 會員制智能客服機器人模組：
  - 類 ChatGPT 介面：具備可開關的左側歷史對話 Sidebar (寬度 280px) 與會員等級標籤。桌面（寬度 992px 以上）預設開啟並推擠主區域，開關狀態記於 localStorage（讀寫須包 try/catch，失敗時退回預設值）；手機與平板（寬度未滿 992px）預設關閉，以覆蓋方式顯示並附遮罩，不記住狀態。
  - 會員制：以 email 為帳號識別，一組 email 只能對應一個帳號。會員等級為 free（一般）、plus（進階）、vip（尊榮），僅影響額度、不影響權限；初始額度為每日對話輪數 30 / 100 / 300、每分鐘訊息數 6 / 10 / 20、對話數上限 20 / 100 / 500，單則訊息上限皆為 1000 字；RPG 記事簿容量 2000 / 5000 / 10000 字（見第 6 節）。
  - NLP 意圖與實體解析：Service 層呼叫 Gemini API 時強制要求以非串流請求輸出標準 JSON（至少包含 intent, confidence, entities, reply_text, suggested_actions）。
  - 建議動作呈現：聊天視窗只顯示 reply_text（純文字），intent 與 entities 不顯示給一般使用者。suggested_actions 以快速回覆 chips 呈現於最後一則 AI 訊息下方，打字機渲染結束後淡入，一次最多 4 個；樣式為膠囊形、border: 1px solid #2A2D34、背景透明、文字 #8A8F99，hover 時邊框與文字改為 #FF6B35；點擊後以其 text 作為使用者訊息送出，並以 v-if 移除該組 chips。
  - 客服僅由 AI 提供，不設真人客服通道。使用者要求真人客服時，AI 須以繁體中文說明並引導以意見回饋留下紀錄，供管理員於後台檢視。
  - 知識範圍：v1 僅回答平台自身資訊（登入與帳號、會員等級與額度、意見回饋流程）；超出範圍或沒有資料來源的問題歸為 unknown，並引導以意見回饋留下紀錄。有 FAQ 文件時再另行擴充，不採檢索式 (RAG)。

---

## 8. 語系與字元編碼規範（繁體中文與 UTF-8 強約束）
- 全系統語言統一（Traditional Chinese）：
  - 所有 UI 介面文字、系統 Prompt、錯誤訊息、註解、選單、Modal/Toast 提示、預設劇本內容與客服回應，一律強制採用繁體中文（zh-TW，台灣常用術語）；唯一例外是必須純 ASCII 的檔案（見下方檔案編碼規範）。
  - 要求 Gemini API 生成內容時，必須在 System Prompt 中明確約束回傳繁體中文。
  - 系統寄出的信件（信箱驗證、重設密碼）一律為 UTF-8 編碼與繁體中文內容。
- 檔案編碼與 I/O 規範：
  - 後端檔案讀寫：Python 處理文字、JSON、CSV、檔案讀寫（open()）或匯出/匯入操作時，必須明確指定 encoding，嚴禁使用 ANSI 或未指定之編碼。
    - 讀取：一律使用 encoding='utf-8-sig'（可自動去除 BOM）。
    - 寫入 JSON 與一般文字檔：使用 encoding='utf-8'，避免 BOM 導致 JavaScript 的 JSON.parse 解析失敗。
    - 匯出供 Windows/Excel 開啟的 CSV：使用 encoding='utf-8-sig'，確保繁體中文絕不亂碼。
  - 資料庫與 HTML 標籤：SQLAlchemy 連線與文字欄位必須支援 Unicode（見第 4 節）；HTML 模板標註 <meta charset="UTF-8"> 與 <html lang="zh-Hant-TW">。
  - 以系統地區編碼讀取的檔案與腳本：
    - alembic.ini 與 script.py.mako 必須純 ASCII（見第 4 節）。
    - Windows PowerShell 5.1 以系統編碼讀取沒有 BOM 的 .ps1；內含繁體中文或其他非 ASCII 字元的 .ps1 須存成 UTF-8 with BOM，否則會亂碼。
    - 需要輸出繁體中文訊息，或要把字串傳給外部程式（例如 wsl、docker）的維運與驗收腳本，一律以 Python 撰寫（UTF-8、參數以清單傳遞）；.ps1 只作為純 ASCII 的薄包裝（例如容器驗收的 scripts/verify_container.py 與 scripts/verify-container.ps1）。

---

## 9. 部署與正式環境 (Deployment)
- 部署方式：正式環境以 Docker 映像部署於雲端 PaaS（容器型），並使用託管的 SQL Server；雲端平台為 Google Cloud（Cloud Run、Cloud SQL for SQL Server、Secret Manager）。映像檔須包含 static/vendor/ 與 static/fonts/，以非 root 使用者執行，且不得包含 .env。
- 正式環境不得使用 Flask 開發伺服器（app.run）。正式環境以 gunicorn（gthread worker）運行，worker 數與執行緒數依負載測試結果設定；客服與 RPG 串流使用獨立的 worker 池。（RPG 適用）SSE 端點須運行於支援長連線並發的正式伺服器配置。
- 入口層須啟用 HTTPS 與 HTTP/2 並設定 HSTS；Flask 以 ProxyFix 信任轉發標頭，否則 Secure cookie 與 OAuth 回呼網址會判斷錯誤。
- 多 worker 運行時，應用程式不得依賴行程內記憶體保存狀態（如快取、串流進度、限流計數）；狀態須存放於資料庫或其他共用儲存。限流第一道使用入口層（如 Nginx limit_req），帳號層級的計數與鎖定存放於資料庫。
- 模型供應商的 API Key 只能由後端從環境變數讀取，嚴禁寫入程式碼、模板或版本庫：客服使用 Gemini，變數名稱 GEMINI_API_KEY（只有客服使用）；RPG 用途使用 OpenAI 相容端點，變數名稱 OPENROUTER_API_KEY（與 OPENROUTER_MODEL 成對，不使用 GEMINI_API_KEY）。本機開發以 `.env` 檔注入（`.env` 不進版本庫），正式環境以平台機密功能注入。
- 環境與機密管理：
  - 以環境變數 APP_ENV（development、testing、staging、production）選擇設定類別；未設定時拒絕啟動，不預設為 development。staging 為部署於雲端的預備環境，除可關閉信箱驗證（見第 10 節）外，其餘設定與 production 相同（Secure cookie、禁用除錯、不得使用 sa 與 TrustServerCertificate）。staging 與 production 缺少必要機密時，應用程式啟動即失敗 (fail-fast)。
  - 所有機密（Gemini 金鑰、OpenRouter 金鑰、SECRET_KEY、資料庫連線、OAuth、寄信服務）採同一套注入機制，不得另設例外；機密不得出現在日誌、錯誤訊息或前端回應中。
  - 金鑰至少每 90 天輪替，疑似外洩時立即輪替；輪替流程為「建立新金鑰 → 更新機密 → 重新部署 → 驗證 → 停用舊金鑰」。SECRET_KEY 輪替可暫用 SECRET_KEY_FALLBACKS（Flask 3.1 以上）。
- 資料庫連線：連線帳號與密碼僅存於 DATABASE_URL（`.env` 或平台機密）。本機開發的 SQL Server 連線可設定 TrustServerCertificate=yes 以信任自簽憑證；正式環境嚴禁使用，須驗證伺服器憑證，且不得使用 sa 帳號。
- 專案提供 `.env.example` 列出所需變數名稱，不含實際值。
- 開發與正式設定須分離，正式環境不得沿用開發設定（如除錯模式、本機資料庫路徑）。

---

## 10. 身分驗證與安全 (Authentication & Security)
- 登入與 session：採簽章 session cookie（HttpOnly、Secure、SameSite=Lax；正式環境使用 __Host- 前綴，存活 7 天），內容只含 user_id 與 token_version；不使用 JWT。改密碼、登出所有裝置或停權時使 token_version 加 1，以撤銷既有 session。
- 密碼：以 Argon2id 雜湊，長度限制 8 至 128 字，不強制字元組合。
- 帳號唯一：一組 email 只能對應一個帳號，email 正規化為小寫後唯一；判斷是否重複時，gmail.com 與 googlemail.com 的信箱另去除「.」與「+標籤」後比對（email_canonical 欄位，同樣唯一），寄信仍使用原始地址。Google 登入與密碼登入共用同一帳號（見合併規則）。帳號刪除後，同一信箱可重新註冊。
- 註冊與驗證：以信箱加密碼註冊後為 pending_verification，須驗證信箱才可使用客服對話。信箱驗證與重設密碼信件由環境變數 EMAIL_VERIFICATION_REQUIRED 控制：development、testing、staging 可設為 false，此時註冊後直接為 active、email_verified_at 保持空值，忘記密碼功能一併停用（不得以日誌輸出權杖替代寄信）；production 必須為 true 並已設定寄信服務，否則啟動失敗。註冊時須勾選同意服務條款與隱私權政策，並記錄 terms_version 與 terms_accepted_at。驗證與重設密碼的權杖為 32 位元組隨機值，資料庫只存其 SHA-256 雜湊，單次使用；驗證信 24 小時、重設信 1 小時失效。連結的 GET 只顯示確認頁，由 POST 完成動作。
- 年齡確認：註冊（含 Google 首次註冊）須填寫生日，並從下拉式選項選擇年齡區間；兩者皆為使用者自行聲明，不蒐集證件、不核實。伺服器由生日計算年齡：未滿 13 歲者不得註冊（不建立帳號，也不保存其填寫的生日）；與所選年齡區間不一致時，一律回覆通用的「資料有誤」訊息並要求重新註冊，不建立帳號。通過後記錄 birth_date、age_range、age_confirmed_at 與 age_policy_version，並可另記錄當時是否已滿 18 歲的結果（僅供紀錄）；是否已滿 18 歲一律由 birth_date 於當下計算，不以儲存的結果判斷，使用者滿 18 歲後限制自動解除。生日與年齡區間不傳送至 Gemini；管理員後台只提供年齡區間的彙總統計，不顯示個別使用者的生日。13 至 17 歲者可以使用本平台（含 AI 功能），但只能使用安全模式（見第 7 節）並在介面顯示提示；已滿 18 歲者不受此限。尚未完成年齡確認的帳號（例如由管理員指令建立的帳號）可登入與管理帳號，但不得使用任何會呼叫 Gemini 的功能，由 AccessPolicy 判斷並回 403 與代碼 age_confirmation_required；管理員與各會員等級不豁免；這類帳號於帳號設定頁補填。原因與前提：Gemini API 條款要求使用者年滿 18 歲，且不得用於可能被未滿 18 歲者使用的應用，因此「13 至 17 歲者可使用」是以 Google 同意為前提的設計；Google 不同意時，本條須修訂為未滿 18 歲者不得使用。
- 防帳號列舉：註冊、忘記密碼與登入失敗一律回應相同的通用訊息，不洩漏帳號是否存在。重設密碼成功後 token_version 加 1，並寄通知信。
- Google 登入：採 Authorization Code 流程搭配 PKCE、state 與 nonce；須驗證 ID token 的 iss、aud、exp、nonce，且要求 email_verified 為真；開發與正式環境使用不同的 OAuth 用戶端。
- 同信箱帳號合併：以 email_verified 為前提。既有本機帳號的信箱已驗證者，直接綁定 Google 身分；未驗證者，清除其未驗證的密碼憑證後綁定並視為已驗證。以信箱註冊時若該信箱已有 Google 帳號，不建立新帳號。
- CSRF：所有非 GET 請求須通過 CSRF 檢查（token 置於頁面 meta，axios 預設帶 X-CSRF-Token 標頭）並驗證 Origin。不得使用需載入外部腳本的驗證碼服務（違反離線前端架構）。
- 登入失敗限制：同一帳號連續失敗 5 次鎖定 15 分鐘，計數存放於資料庫。
- 輸入限制：客服單則訊息上限 1000 字（以 Unicode 字元計），送出前去除控制字元並做 NFC 正規化，空白訊息在 Service 層擋下，不呼叫 Gemini。
- LLM 安全：指令只放在 Gemini 的 system_instruction，使用者文字不得串接進指令；玩家撰寫的個性、角色與世界文字同樣視為使用者文字（記事簿內容，含由玩家文字抓取的重點，亦同），只能以資料區塊放在使用者內容區，不得進入 system_instruction；System Prompt 內放不公開的標記字串，輸出若包含則視為驗證失敗；客服 v1 不提供工具呼叫 (function calling)。reply_text 一律以純文字渲染，嚴禁 v-html。客服：同一使用者一日內被內容安全過濾阻擋達 3 次，暫停對話 1 小時並記錄。RPG：被擋的回合顯示劇情轉場文字而非錯誤、標記為中斷且不進入上下文、自動重新生成一次並記錄；暫停對話的計數只計玩家輸入被擋的次數，模型輸出被擋（多為誤判）不計入，並須記錄結束原因與被擋類別。
- 資料隔離與權限：
  - 所有對話相關的 Repository 方法簽名必須接收 user_id，查詢一律帶 user_id 條件，不提供只憑 ID 取資料的方法給 API 路徑使用；Service 層以 AccessPolicy 集中判斷擁有者與管理員權限，Controller 不自行寫權限判斷。
  - 非擁有者存取一律回 404（非 403），避免洩漏資源是否存在。
  - 角色僅 user 與 admin。管理員存取他人資料須走專用的 Repository 方法，並寫入稽核紀錄 (admin_audit_logs)。
  - 管理員後台為獨立的 /admin 頁面與 /api/admin/*，僅 role 為 admin 者可存取，功能含停權、查看用量與刪除違規內容。首位管理員由部署者以一次性 flask 指令建立。管理員預設只能檢視統計資料、使用者主動提交的意見回饋對話、被內容安全過濾標記的對話，以及待審的性格原型草案（僅含去識別後的草案內容，不含對話全文），不得任意讀取其他對話全文；停權、刪除內容與調整會員等級等高風險操作須重新輸入密碼。
- 用量控制：每位使用者設有速率限制與每日對話輪數上限（依會員等級）；另設全站每日 token 上限，達 80% 告警、達 100% 以繁體中文回覆系統忙碌文案。計數以資料庫帳本 (llm_call_logs) 彙總，fallback 訊息不計入；日界以 Asia/Taipei 計算（資料庫存 UTC）。同一對話同時只處理 1 則訊息。超額與限流的回覆文案一律為繁體中文。

---

## 11. 個資、保存與可觀測性 (Privacy & Observability)
- 日誌格式：結構化 JSON，輸出至標準輸出。
  - 可記錄：時間、層級、request_id、user_id（內部整數）、路由路徑（不含查詢字串）、HTTP 方法、狀態碼、延遲、對話 id（內部整數）、intent（列舉值）、模型名稱、嘗試次數、prompt_tokens、completion_tokens、finish_reason、錯誤類型、截斷或雜湊後的 IP。
  - 絕不記錄：密碼與 password_hash、session cookie、CSRF 與驗證/重設權杖、OAuth code、Authorization 標頭、完整 API Key、使用者 email、生日與年齡區間、使用者訊息全文、reply_text、模型原始輸出（Gemini 與 OpenRouter）、含權杖的完整網址。失敗的模型輸出僅依第 5 節寫入 llm_call_logs.error_detail。
- 保存期限：對話與訊息為最後一則訊息後 12 個月（或使用者主動刪除）；帳號（含生日、年齡區間與年齡確認紀錄）自最後一次登入起算，連續 12 個月未登入即直接刪除（不經 7 天寬限期，刪除內容與使用者主動刪除相同，並寫入 deletion_tombstones；角色為 admin 的帳號不納入，否則唯一的管理員閒置一年就會消失）；auth_tokens 為到期後 7 天；llm_call_logs（不含對話全文）12 個月；失敗原文 14 天；應用程式日誌 30 天；管理稽核紀錄 24 個月；備份 30 天。
- 備份：託管 SQL Server 啟用自動備份與時間點還原，備份加密並存放於不同帳號或區域；每季進行一次還原演練。
- 刪除帳號：使用者申請後先進入 deletion_pending（立即停用，7 天寬限期可撤銷），期滿由排程指令刪除 email、名稱、密碼雜湊、生日與年齡區間、user_identities、auth_tokens 與全部對話；llm_call_logs 的 user_id 設為 NULL 匿名化；備份於保存期限屆滿後自然汰除，還原時依 deletion_tombstones（僅存內部 ID 與刪除日期）重新執行刪除。
- 當事人權利：提供匯出個人對話與帳號資料（含生日與年齡區間）的功能，JSON 以 utf-8、CSV 以 utf-8-sig 輸出。隱私權政策須告知對話內容會傳送至 Google 的 Gemini 服務、玩家的角色描述會經 AI 處理，且可能於去識別後由管理員審閱；並須揭露生日與年齡區間的蒐集目的（年齡確認、內容分級，以及以去識別的統計方式了解用戶群）與保存期限，日後若新增生日祝賀等其他用途，須先更新隱私權政策。隱私權政策與服務條款為應用程式內的繁體中文頁面（/privacy、/terms）。
- Gemini 方案與個資：免費方案的資料可能被用於改進產品，使用免費方案期間不得將訂單、金流等敏感個資送入模型；正式對外開放給會員前，須重新評估所用方案的資料使用條款。現階段為個人測試與展示，使用免費方案且不得輸入真實個資，正式推出前改用付費方案；敏感內容（成熟分級）不得使用免費方案，因其輸入輸出可能被人工審閱。
- RPG 模型供應商（OpenAI 相容端點，目前為 OpenRouter）與個資：免費模型的輸入輸出可能被供應商留存或用於改進產品，使用免費模型期間不得輸入真實個資，玩家的角色描述與記事簿同樣屬於個資；正式對外開放給會員前，須重新評估所用供應商與模型的資料使用條款，改用付費模型並啟用零保存（OPENROUTER_ZDR）；敏感內容（成熟分級）不得使用免費模型。
- 性格原型草案：草案含玩家衍生文字，視為個資，退回或逾期即清除，帳號刪除時一併刪除，保存期限另訂；成熟分級的紀錄採資料最小化，保存期限與加密方式於法務確認後另訂。
- RPG 記事簿：內容由玩家文字衍生，視為個資；保存期限與所屬存檔相同，存檔刪除或帳號刪除時一併刪除，並納入個人資料匯出；不在管理員預設可檢視的範圍內；日誌只記內部編號與整數長度，絕不記錄記事簿內容。
- 排程工作（清除過期資料、執行刪除帳號、清除連續 12 個月未登入的帳號）以 flask 自訂指令實作，由平台排程器觸發，不使用行程內計時器；閒置帳號清除不納入管理員。
- 監控與告警：5xx 比率、對話延遲 p95、Gemini 錯誤與 429 比率、每日 token 用量、登入失敗激增、寄信失敗與服務存活皆須有告警，管道為電子郵件或 webhook；不得使用會將例外內容送出本系統的外部錯誤追蹤服務，除非完成個資清洗。
