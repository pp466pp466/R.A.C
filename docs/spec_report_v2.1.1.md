# 官方系統紀錄文檔

## 開放世界 AI 文字冒險遊戲平台 - 內容統整與開發規格報告

- **專案類型**：LLM 驅動開放世界 RPG 平台（與會員制智能客服構成雙核心平台）
- **文件版本**：v2.1.1（以專案 AGENTS.md 為準，對應 AGENTS_v3）
- **日期**：2026年10月27日

---

## 主題一：專案概述與總覽 (Project Overview)

本專案旨在建構一個以大型語言模型 (LLM) 驅動的「開放世界聊天式文字冒險遊戲平台」，並與「會員制智能客服機器人系統」共同組成雙核心平台。系統結合動態敘事生成、記憶體讀取與狀態轉移框架，為玩家提供高度自由、無硬性結局的 RPG 冒險體驗。

### 核心運作與開發規範 (AGENTS.md 遵循指南)

1. **動態內容生成**：由 AI 根據世界觀設定動態發展故事，不設固定劇情路線，亦無硬性結局（廢除死亡死局，採狀態轉移框架）。
2. **無縫同步與追蹤**：前端以 IndexedDB 暫存完整對話與世界線，連線正常時透過背景 Axios 非同步同步至後端永久儲存庫；同步衝突時以後端永久儲存庫為準。
3. **LLM 約束與邊界**：全系統統一調用 `gemini-3.8-flash`。RPG 劇情以 SSE 串流輸出自由文字；客服意圖解析必須以非串流請求輸出標準 JSON。所有結構化輸出須經 JSON 驗證與 Service 層驗證後，才可存入永久儲存庫。
4. **離線前端架構**：嚴禁使用 CDN。Vue 3、Bootstrap 5.3、Axios 一律下載至 `static/vendor/`；字型檔存放於 `static/fonts/`，嚴禁 Google Fonts 等外部服務；引入資源一律使用 `url_for`。
5. **視覺風格統一**：採用「極簡黑暗電影 / 文字雜誌風 (Cinematic Dark Minimalist)」，配色包含劇院深黑 (`#0F1012`)、薄霧灰白 (`#E2E4E9`，嚴禁純白)、鋼鐵灰 (`#8A8F99`)、導演琥珀橙 (`#FF6B35`) 與 1px 極細金屬邊框 (`#2A2D34`)。
6. **自製 UI 元件**：嚴禁使用外部彈窗套件（如 SweetAlert2），一律以 Vue 3 全域變數模式搭配 Bootstrap 5.3 自製 Modal 與 Toast；顯示狀態由 `v-if` / `v-show` 完全接管，禁止 `data-bs-*` 互動屬性，唯一例外為深色模式開關 `data-bs-theme="dark"`。
7. **雙軌資料庫抽象**：採用 SQLAlchemy ORM 支援 SQLite 與 SQL Server 雙軌運作；業務邏輯與 Repository 層嚴禁撰寫 Raw SQL，唯一例外為 SQLite 的連線初始化事件（PRAGMA journal_mode=WAL）。
8. **語系與編碼**：全系統統一採用繁體中文（zh-TW）；Python 讀取檔案使用 `utf-8-sig`，寫入 JSON 與一般文字檔使用 `utf-8`，匯出供 Excel 開啟的 CSV 使用 `utf-8-sig`。

---

## 主題二：技術架構與後端分層設計 (Architecture Spec)

### 1. 技術堆疊表

| 項目 | 規範 |
|---|---|
| 後端框架 | Python 3.10+ / Flask（MVC + Web API 雙 Controller） |
| LLM 引擎 | Google Gemini API（使用 Gemini API Key，金鑰管理方式見主題六），預設模型 `gemini-3.8-flash`，適用於 RPG 劇情生成、SSE 串流、會員制智能客服與對話摘要壓縮 |
| 資料庫與 ORM | SQLAlchemy ORM + SQLite + SQL Server（雙軌相容） |
| SQLite 條件式初始化 | 建立 Engine 後，若 `engine.dialect.name == "sqlite"`，才以 `event.listen(engine, "connect", ...)` 於底層連線建立時自動執行 `PRAGMA journal_mode=WAL;`（預寫日誌模式）。業務程式碼不得直接呼叫。 |
| SQL Server 初始化 | 不註冊上述事件，交由 SQLAlchemy 連線池與交易管理接管並發與非同步。 |
| 前端框架 | Vue 3.0 JavaScript（全域變數模式），不使用 Vue 單檔元件（`.vue`）與 Vite 打包 |
| RWD 框架 | Bootstrap 5.3（Dark Mode 以 `data-bs-theme="dark"` 全域啟用並覆蓋樣式） |
| 網路通訊 | 一般 API 採用 Axios（離線引入）；LLM 串流採用原生 fetch 搭配 ReadableStream |
| 打字機效果 | 前端 Typewriter Buffer 逐字平滑渲染：RPG 為串流片段；客服為完整 `reply_text` |

### 2. 後端關注點分離 (Separation of Concerns)

- **MVC Page Controller**（`controllers/page_controller.py`）：僅負責渲染基本 HTML 頁面外殼與初始狀態傳遞，包含 `/game` 與 `/cs` 兩套視圖。
- **Web API Controller**（`controllers/api_controller.py`）：一律回傳 JSON 格式，專門處理 Axios 發送的 Ajax 請求與 API 服務。
- **SSE 串流端點**：針對 RPG 劇情生成，採用 `flask.Response` 搭配 `mimetype='text/event-stream'`，為 Web API 層唯一的非 JSON 例外。
- **Service 層**（`services/game_service.py`）：負責 RPG 核心邏輯、System Prompt 組合、強約束事件錨點 (Milestones) 驗證、客服 NLP 意圖解析（非串流 JSON）、對話摘要壓縮，以及 Gemini API 呼叫。
- **Repository 層**（`repositories/game_repository.py`）：負責 SQLAlchemy CRUD 操作、封裝 ORM Session 釋放與連線生命週期管理；JSON 欄位由 SQLAlchemy JSON 型態負責序列化與反序列化。

### 3. 檔案編碼與 I/O 規範

- **讀取檔案**：一律使用 `encoding='utf-8-sig'`（可自動去除 BOM）。
- **寫入 JSON 與一般文字檔**：使用 `encoding='utf-8'`，避免 BOM 導致 JavaScript 的 `JSON.parse` 解析失敗。
- **匯出供 Windows / Excel 開啟的 CSV**：使用 `encoding='utf-8-sig'`。
- 嚴禁使用 ANSI 或未指定之編碼。HTML 模板須標註 `<meta charset="UTF-8">` 與 `<html lang="zh-Hant-TW">`。

---

## 主題三：資料庫 Schema 設計 (Database Schema Spec - SQLAlchemy ORM)

所有 JSON 型態欄位採用 SQLAlchemy 的 JSON 型態，文字欄位採用 Unicode / UnicodeText 型別（SQL Server 對應 NVARCHAR）。

### 1. 使用者資料表 (`users`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 使用者唯一識別碼 |
| `username` | VARCHAR(50) | UNIQUE, NOT NULL | 帳號名稱 |
| `password_hash` | VARCHAR(255) | NOT NULL | 加密密碼 |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 註冊時間 |

### 2. 劇本資料表 (`scripts`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 劇本唯一識別碼 |
| `creator_id` | INTEGER | FOREIGN KEY (users.id) | 創作者 ID |
| `title` | VARCHAR(100) | NOT NULL | 劇本名稱 |
| `world_view` | VARCHAR(50) | NOT NULL | 世界觀類別（如：古風仙俠） |
| `background_and_system` | TEXT | NOT NULL | 背景與境界體系詳細設定 |
| `characters_json` | JSON | NOT NULL | 重要 NPC 名冊（含預設好感度與性格） |
| `locations_json` | JSON | NULLABLE | 已知地點清單 |
| `milestones_json` | JSON | NOT NULL | 強約束必觸發事件 (Milestones) |
| `state_transition_rules` | TEXT | NOT NULL | 狀態轉移規則（取代原結局條件規則，本平台無硬性結局） |

### 3. 遊戲工作階段 / 玩家存檔表 (`sessions`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 遊戲 Session 唯一識別碼 |
| `user_id` | INTEGER | FOREIGN KEY (users.id) | 玩家 ID |
| `script_id` | INTEGER | FOREIGN KEY (scripts.id) | 對應劇本 ID |
| `player_name` | VARCHAR(50) | NOT NULL | 玩家角色名字 |
| `player_age` | INTEGER | DEFAULT 16 | 當前角色年齡（無年齡上限） |
| `current_location` | VARCHAR(100) | NULLABLE | 當前所在地點 |
| `status_summary` | TEXT | NULLABLE | 玩家狀態 / 修為 / 裝備摘要 |
| `entities_json` | JSON | NULLABLE | 動態世界實體紀錄（NPC 狀態、物品、地點等） |
| `is_archived` | BOOLEAN | DEFAULT FALSE | 玩家主動封存存檔（非劇情結局） |
| `updated_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 最後遊玩時間 |

### 4. 對話與記憶體紀錄表 (`memory_logs`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 對話紀錄 ID |
| `session_id` | INTEGER | FOREIGN KEY (sessions.id) | 對應遊戲 Session ID |
| `turn_number` | INTEGER | NOT NULL | 對話回合數（一輪 = 玩家訊息 + AI 回應） |
| `user_action` | TEXT | NOT NULL | 玩家輸入或選擇的行動 |
| `llm_raw_response` | TEXT | NOT NULL | LLM 完整回應原文（RPG 為串流完整文字；客服為非串流 JSON 字串）。串流中斷時的處理見主題六第 6 點。 |
| `structured_result` | JSON | NULLABLE | 經 JSON 驗證後的結構化結果（如客服 intent / entities / suggested_actions，詳見主題六） |
| `memory_summary` | TEXT | NULLABLE | 累積摘要（超出最近 10 輪的對話壓縮結果），由 Service 層更新 |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 生成時間 |

---

## 主題四：首發預設劇本設定 (Default Script Spec - 古風仙俠)

- **世界觀**：古風仙俠
- **故事背景**：乾歷朝 1202 年到 1208 年長達 6 年的正邪大戰落下帷幕，正道聯合對陣邪魔兩教聯合最後正道取得勝利。邪魔兩方幾乎殞落，正道也損失慘重，8 個門派只餘 4 個，其中青雲宗為其中一個存活的正道門派。現在為乾歷朝 1214 年 6 月。
- **修道體系**：入氣 > 練體 > 築基 > 金丹 > 元嬰 > 化神 > 渡劫 > 飛升成仙
- **靈根種類**：五行靈根 (金木水火土)、變異靈根 (冰雷風時空)
- **法寶 / 靈根品階**：下品、中品、上品、極品（法寶分為黃階 > 地階 > 天階 > 仙器）
- **重要 NPC**：夏侯傑（青雲宗宗主，老成沉穩帶點傲慢，口頭禪：「自從正邪大戰後未嘗一敗。」），初始好感度：20
- **已知地點**：青雲宗
- **必觸發事件 (Milestones)**：乾歷朝 1215 年 11 月青雲宗收徒大典。觸發後由 LLM 動態延伸新目標（對應 AGENTS.md 動態階段錨點）。
- **狀態轉移設置（取代原結局設置）**：本劇本無硬性結局。遭遇實力差距過大的敵人時，不以死亡作為唯一結果；具體的狀態轉移規則由劇本的 `state_transition_rules` 定義。無年齡上限。

---

## 主題五：歷次談話日誌與修訂稽核 (Conversation Logs Audit)

| 紀錄 | 日期 | 內容摘要 |
|---|---|---|
| 紀錄001 | 2026-10-20 | 確立開發討論與文件同步機制。 |
| 紀錄002 | 2026-10-20 | 確立開放世界文字冒險核心構想：隨機開頭、雙軌互動、重大事件必觸發三大核心。（*v2.1.0 已依 AGENTS_v2 修正為無硬性結局，多結局構想不再採用，見紀錄012*） |
| 紀錄003 | 2026-10-20 | 世界觀與帳號記憶體架構：首發「古風仙俠」；定義 User ID 續聊與新開局記憶體重置邏輯。 |
| 紀錄004 | 2026-10-20 | 劇本規格擴充：新增 Milestones 強約束必觸發機制。 |
| 紀錄005 | 2026-10-20 | 劇本實體欄位結構細節完善：精細化重要人物與地點之預設占位符與寫入規則。 |
| 紀錄006 | 2026-10-20 | 系統 Prompt 範本與 AGENTS.md 強約束規範：完成 Service / LLM 邊界與 JSON 驗證流程設計。 |
| 紀錄007 | 2026-10-20 | 預設劇本「古風仙俠」細節編撰：制定正邪大戰、境界、NPC 夏侯傑與收徒大典。 |
| 紀錄008 | 2026-10-20 | 資料庫 Schema 設計與 Repository 規範：設計 users、scripts、sessions、memory_logs 四大資料表。 |
| 紀錄009 | 2026-10-20 | NPC 好感度與動態狀態系統整合：原加入 favorability_change 與 status_effect 追蹤欄位，v2.1.0 改為併入 structured_result（見主題六）。 |
| 紀錄010 | 2026-10-22 | 極簡電影風 UI 與 Gemini API 整合校正：依 AGENTS.md 修訂視覺美學，廢除 SweetAlert2 改為 Vue 3 + Bootstrap 5.3 自製 Modal / Toast，確認離線架構與 Gemini API Key 串接。 |
| 紀錄011 | 2026-10-22 | SQLAlchemy ORM + SQLite + SQL Server 雙軌資料庫規範校正：加入「純 ORM 抽象原則」、「SQLite WAL 條件式初始化 / 方言隔離」與「SQLAlchemy JSON 欄位型態映射」。 |
| 紀錄012 | 2026-10-27 | 依 AGENTS_v2 全面對齊（v2.1.0）：<br>• 廢除死亡與老死結局：`ending_rules` 改為 `state_transition_rules`；`is_ended` 改為 `is_archived`；移除 `end_reason`；首發劇本結局設置改寫。<br>• RPG 劇情改為 SSE 串流自由文字；客服改為非串流 JSON；`llm_raw_response` 由 JSON 改為 TEXT，新增 `structured_result`。<br>• 對話上下文由 20 輪改為「摘要 + 最近 10 輪 + 當前世界狀態」，一輪定義為玩家訊息 + AI 回應。<br>• 模型統一為 `gemini-3.8-flash`（含對話摘要壓縮）；同步衝突以後端為準。<br>• 新增 `sessions.entities_json`，對齊 AGENTS 第 4 節 JSON 欄位規定。<br>• 新增字型本地化（`static/fonts/`）；Toast 統一為 2px 左側指示線；深色模式 `data-bs-theme` 例外；SQLite 事件條件式註冊。<br>• 新增檔案編碼規範（讀取 utf-8-sig、寫入 JSON 用 utf-8、CSV 匯出用 utf-8-sig）。<br>• 多結局構想依 AGENTS 廢除，對應紀錄002。 |
| 紀錄013 | 2026-10-27 | 與專案 AGENTS.md（對應 AGENTS_v3）對齊：<br>• Service 層職責補充「對話摘要壓縮」（主題二第 2 節）。<br>• 文件依據由 AGENTS_v2 改為專案 AGENTS.md，版本升為 v2.1.1。<br>• 主題六新增第 4 至 7 點待決定事項（Gemini API Key、JSON 驗證失敗、串流中斷紀錄、客服資料表與 Intent / Entity）。<br>• 修正版面與交叉引用。 |

---

## 主題六：待決定事項 (Open Items)

以下事項 AGENTS.md 尚未定義，需由專案負責人確認後再進入實作。

1. **RPG 狀態變更的取得方式**：NPC 好感度變更與狀態效果（原 `favorability_change`、`status_effect`）屬結構化資料，但 RPG 劇情為 SSE 自由文字，無法直接攜帶。建議方案：串流結束後，另以一次非串流請求取得狀態變更 JSON，經驗證後寫入 `structured_result`。
2. **`state_transition_rules` 的格式**：目前定義為 TEXT，尚未決定採自由文字或結構化 JSON。
3. **客服 `suggested_actions` 的前端呈現**：尚未定義按鈕、卡片或其他樣式，須配合 Cinematic Dark Minimalist 風格設計。
4. **Gemini API Key 的讀取方式**：金鑰只從環境變數讀取。本機開發與部署環境的區分，以及具體讀取方式，**延至 propose 輸出時定義**。
5. **JSON 驗證失敗的處理**：尚未定義重試次數、降級回覆文案（須為繁體中文），以及是否記錄失敗的原始回應供除錯。
6. **串流中斷時的紀錄處理**：`memory_logs.llm_raw_response` 目前為 NOT NULL，但串流中途斷線時沒有完整回應可寫入。需決定：改為 NULLABLE、記錄已接收的部分內容，或不寫入。
7. **客服資料表與 Intent / Entity 結構**：目前規格只涵蓋 RPG。客服對話是否獨立建立 `chat_conversations`、`chat_messages` 等資料表、Intent 清單與 Entity 結構，**待 explore 定案後補入本文件**。

---

本文件以專案 AGENTS.md（對應 AGENTS_v3）為準進行對齊。若與 AGENTS.md 有任何衝突，以 AGENTS.md 為準，並於主題五另行記錄。
