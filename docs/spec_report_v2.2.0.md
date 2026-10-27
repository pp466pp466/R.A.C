# 官方系統紀錄文檔

## 開放世界 AI 文字冒險遊戲平台 - 內容統整與開發規格報告

- **專案類型**：LLM 驅動開放世界 RPG 平台（與會員制智能客服構成雙核心平台）
- **文件版本**：v2.2.0（以專案 AGENTS.md 為準；納入 docs/report.md 的 explore 決策）
- **日期**：2026年10月27日
- **範圍說明**：依 explore 決策 D-00，本期實作範圍為「會員制智能客服」與共用層；RPG 模組的設計方向另見 docs/RPG.md，本文件主題三的 RPG 資料表維持 v2.1.1 定義，待 RPG 的 change 啟動時依 docs/RPG.md 第 2 章調整。

---

## 主題一：專案概述與總覽 (Project Overview)

本專案旨在建構一個以大型語言模型 (LLM) 驅動的「開放世界聊天式文字冒險遊戲平台」，並與「會員制智能客服機器人系統」共同組成雙核心平台。系統結合動態敘事生成、記憶體讀取與狀態轉移框架，為玩家提供高度自由、無硬性結局的 RPG 冒險體驗。

### 核心運作與開發規範 (AGENTS.md 遵循指南)

1. **動態內容生成**：由 AI 根據世界觀設定動態發展故事，不設固定劇情路線，亦無硬性結局（廢除死亡死局，採狀態轉移框架）。
2. **無縫同步與追蹤**：前端以 IndexedDB 暫存完整對話與世界線，連線正常時透過背景 Axios 非同步同步至後端永久儲存庫；同步衝突時以後端永久儲存庫為準。客服對話以後端為權威，後端指派 `seq`，並以 `client_msg_id` 冪等去重，重送不重複呼叫模型。
3. **LLM 約束與邊界**：全系統統一調用 `gemini-3.8-flash`。RPG 劇情以 SSE 串流輸出自由文字；客服意圖解析必須以非串流請求輸出標準 JSON（至少包含 intent, confidence, entities, reply_text, suggested_actions）。所有結構化輸出須經 JSON 驗證與 Service 層驗證後，才可存入永久儲存庫；驗證失敗的處理流程見 AGENTS.md 第 5 節。
4. **離線前端架構**：嚴禁使用 CDN。Vue 3、Bootstrap 5.3、Axios 一律下載至 `static/vendor/`；字型檔存放於 `static/fonts/`，嚴禁 Google Fonts 等外部服務；引入資源一律使用 `url_for`。
5. **視覺風格統一**：採用「極簡黑暗電影 / 文字雜誌風 (Cinematic Dark Minimalist)」，配色包含劇院深黑 (`#0F1012`)、薄霧灰白 (`#E2E4E9`，嚴禁純白)、鋼鐵灰 (`#8A8F99`)、導演琥珀橙 (`#FF6B35`) 與 1px 極細金屬邊框 (`#2A2D34`)。
6. **自製 UI 元件**：嚴禁使用外部彈窗套件（如 SweetAlert2），一律以 Vue 3 全域變數模式搭配 Bootstrap 5.3 自製 Modal、Toast 與 Sidebar；顯示狀態由 `v-if` / `v-show` 完全接管，禁止 `data-bs-*` 互動屬性與 Bootstrap 原生 JavaScript 外掛（含 offcanvas），唯一例外為深色模式開關 `data-bs-theme="dark"`。
7. **雙軌資料庫抽象**：採用 SQLAlchemy ORM 支援 SQLite 與 SQL Server 雙軌運作；業務邏輯與 Repository 層嚴禁撰寫 Raw SQL，唯一例外為 SQLite 的連線初始化事件（PRAGMA journal_mode=WAL、foreign_keys=ON、busy_timeout）。資料表變更一律使用 Alembic。
8. **語系與編碼**：全系統統一採用繁體中文（zh-TW）；Python 讀取檔案使用 `utf-8-sig`，寫入 JSON 與一般文字檔使用 `utf-8`，匯出供 Excel 開啟的 CSV 使用 `utf-8-sig`。
9. **會員制與安全**：以 email 為帳號識別，一組 email 只能對應一個帳號；採簽章 session cookie、CSRF 防護與 Argon2id 密碼雜湊，並提供 Google 登入；對話資料依 `user_id` 隔離，非擁有者一律回 404。
10. **部署與機密**：正式環境以 Docker 映像部署於雲端 PaaS，使用託管 SQL Server，以 gunicorn（gthread）運行；機密（含資料庫連線帳密）只存於環境變數或平台機密，嚴禁寫入程式碼、文件或版本庫。

---

## 主題二：技術架構與後端分層設計 (Architecture Spec)

### 1. 技術堆疊表

| 項目 | 規範 |
|---|---|
| 後端框架 | Python 3.10+ / Flask（MVC + Web API 雙 Controller） |
| LLM 引擎 | Google Gemini API（金鑰只從環境變數 `GEMINI_API_KEY` 讀取，見 AGENTS.md 第 9 節），預設模型 `gemini-3.8-flash`，適用於 RPG 劇情生成、SSE 串流、會員制智能客服與對話摘要壓縮 |
| 資料庫與 ORM | SQLAlchemy ORM + SQLite + SQL Server（雙軌相容）。本機開發與正式環境均使用 SQL Server（連線資訊只存於 `DATABASE_URL`）；SQLite 用於快速單元測試 |
| SQLite 條件式初始化 | 建立 Engine 後，若 `engine.dialect.name == "sqlite"`，才以 `event.listen(engine, "connect", ...)` 於底層連線建立時自動執行 `PRAGMA journal_mode=WAL;`、`PRAGMA foreign_keys=ON;` 與 `PRAGMA busy_timeout`。業務程式碼不得直接呼叫。 |
| SQL Server 初始化 | 偵測到 SQL Server 時不註冊上述事件，交由 SQLAlchemy 連線池與交易管理接管並發與非同步，並設定 `pool_pre_ping`、`pool_recycle`。本機開發連線可使用 `TrustServerCertificate=yes`，正式環境嚴禁，且不得使用 `sa` 帳號。 |
| 資料表變更 | Alembic（`render_as_batch`），正式環境禁用 `create_all()`，migration 於部署前獨立執行 |
| 前端框架 | Vue 3.0 JavaScript（全域變數模式），不使用 Vue 單檔元件（`.vue`）與 Vite 打包 |
| RWD 框架 | Bootstrap 5.3（Dark Mode 以 `data-bs-theme="dark"` 全域啟用並覆蓋樣式） |
| 網路通訊 | 一般 API 採用 Axios（離線引入）；RPG 的 LLM 串流採用原生 fetch 搭配 ReadableStream |
| 打字機效果 | 前端 Typewriter Buffer 逐字平滑渲染：RPG 為串流片段；客服為完整 `reply_text` |
| 伺服器 | gunicorn（gthread worker）；客服與 RPG 串流使用獨立 worker 池；入口層啟用 HTTPS、HTTP/2 |

### 2. 後端關注點分離 (Separation of Concerns)

- **MVC Page Controller**（`controllers/page_controller.py`）：僅負責渲染基本 HTML 頁面外殼與初始狀態傳遞，包含 `/game`、`/cs` 兩套視圖，以及 `/login`、`/register` 等帳號頁面。
- **Web API Controller**（`controllers/api_controller.py`）：一律回傳 JSON 格式，專門處理 Axios 發送的 Ajax 請求與 API 服務，包含 `/api/auth/*`、`/api/cs/*`、`/api/admin/*`，以及不需登入的 `/healthz`、`/readyz`。
- **SSE 串流端點**（RPG 適用）：針對 RPG 劇情生成，採用 `flask.Response` 搭配 `mimetype='text/event-stream'`，為 Web API 層唯一的非 JSON 例外。
- **Service 層**（`services/game_service.py` 等）：負責 RPG 核心邏輯、System Prompt 組合、強約束事件錨點 (Milestones) 驗證、客服 NLP 意圖解析（非串流 JSON）、對話摘要壓縮、Gemini API 呼叫，以及身分驗證與資料存取政策檢查（`AccessPolicy`）。
- **Repository 層**（`repositories/*.py`）：負責 SQLAlchemy CRUD 操作、封裝 ORM Session 釋放與連線生命週期管理；JSON 欄位由 SQLAlchemy JSON 型態負責序列化與反序列化。對話相關方法必須接收 `user_id`。

### 3. 檔案編碼與 I/O 規範

- **讀取檔案**：一律使用 `encoding='utf-8-sig'`（可自動去除 BOM）。
- **寫入 JSON 與一般文字檔**：使用 `encoding='utf-8'`，避免 BOM 導致 JavaScript 的 `JSON.parse` 解析失敗。
- **匯出供 Windows / Excel 開啟的 CSV**：使用 `encoding='utf-8-sig'`。
- 嚴禁使用 ANSI 或未指定之編碼。HTML 模板須標註 `<meta charset="UTF-8">` 與 `<html lang="zh-Hant-TW">`。系統寄出的信件為 UTF-8 與繁體中文。

---

## 主題三：資料庫 Schema 設計 (Database Schema Spec - SQLAlchemy ORM)

所有 JSON 型態欄位採用 SQLAlchemy 的 JSON 型態，文字欄位採用 Unicode / UnicodeText 型別（SQL Server 對應 NVARCHAR）。對外暴露的資源識別碼使用 UUID（`public_id`）。

### A. 共用層與客服模組（本期實作範圍）

#### 1. 使用者資料表 (`users`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 使用者唯一識別碼 |
| `email` | Unicode(255) | UNIQUE, NOT NULL | 帳號識別；寫入前正規化為小寫。一組 email 只能對應一個帳號 |
| `email_canonical` | Unicode(255) | UNIQUE, NOT NULL | 判斷重複用的比對鍵：小寫；`gmail.com`、`googlemail.com` 另去除「.」與「+標籤」。信件仍寄 `email` |
| `display_name` | Unicode(50) | NOT NULL | 顯示名稱（不要求唯一；取代 v2.1.1 的 `username`） |
| `password_hash` | Unicode(255) | NULLABLE | Argon2id 雜湊；純 Google 帳號為 NULL |
| `email_verified_at` | DATETIME | NULLABLE | 非空代表信箱已驗證 |
| `role` | Unicode(10) | NOT NULL, DEFAULT 'user' | `user` 或 `admin` |
| `member_tier` | Unicode(10) | NOT NULL, DEFAULT 'free' | `free`、`plus`、`vip`，僅影響額度 |
| `status` | Unicode(20) | NOT NULL | `pending_verification`、`active`、`suspended`、`deletion_pending` |
| `failed_login_count` | INTEGER | NOT NULL, DEFAULT 0 | 連續登入失敗次數 |
| `locked_until` | DATETIME | NULLABLE | 登入鎖定截止時間 |
| `token_version` | INTEGER | NOT NULL, DEFAULT 1 | 加 1 即撤銷既有 session |
| `terms_version` | Unicode(20) | NOT NULL | 註冊時同意的服務條款與隱私權政策版本 |
| `terms_accepted_at` | DATETIME | NOT NULL | 同意時間 |
| `last_login_at` | DATETIME | NULLABLE | 最後登入時間 |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 註冊時間 |
| `deleted_at` | DATETIME | NULLABLE | 刪除帳號流程使用 |

#### 2. 外部登入身分表 (`user_identities`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `user_id` | INTEGER | FOREIGN KEY (users.id), NOT NULL | |
| `provider` | Unicode(20) | NOT NULL | 例如 `google` |
| `provider_subject` | Unicode(255) | NOT NULL | 供應商端的使用者識別（OIDC `sub`） |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | |

`(provider, provider_subject)` 唯一。

#### 3. 驗證權杖表 (`auth_tokens`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `user_id` | INTEGER | FOREIGN KEY (users.id), NOT NULL | |
| `purpose` | Unicode(20) | NOT NULL | `verify_email` 或 `reset_password` |
| `token_hash` | Unicode(64) | UNIQUE, NOT NULL | 權杖的 SHA-256 雜湊，不存明文 |
| `expires_at` | DATETIME | NOT NULL | 驗證信 24 小時、重設信 1 小時 |
| `used_at` | DATETIME | NULLABLE | 單次使用 |

#### 4. 客服對話表 (`chat_conversations`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 內部使用 |
| `public_id` | Unicode(36) | UNIQUE, NOT NULL | 對外 UUID |
| `user_id` | INTEGER | FOREIGN KEY (users.id), INDEX | 擁有者 |
| `title` | Unicode(100) | NOT NULL | 對話標題 |
| `summary` | UnicodeText | NULLABLE | 累積摘要（超出最近 10 輪的壓縮結果） |
| `summary_upto_seq` | INTEGER | NOT NULL, DEFAULT 0 | 摘要已涵蓋到的訊息序號 |
| `state_json` | JSON | NULLABLE | 當前狀態：`member_tier`、`last_intent`、`pending_entities`、`open_issue` |
| `state_version` | INTEGER | NOT NULL, DEFAULT 1 | 狀態版本號 |
| `processing_since` | DATETIME | NULLABLE | 處理中標記，逾 60 秒視為過期 |
| `deleted_at` | DATETIME | NULLABLE | 軟刪除 |
| `created_at`、`updated_at` | TIMESTAMP | | |

#### 5. 客服訊息表 (`chat_messages`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `conversation_id` | INTEGER | FOREIGN KEY (chat_conversations.id), INDEX | |
| `client_msg_id` | Unicode(36) | NOT NULL | 前端產生的 UUID；AI 訊息由後端產生。`(conversation_id, client_msg_id)` 唯一 |
| `seq` | INTEGER | NOT NULL | 後端指派的單調遞增序號。`(conversation_id, seq)` 唯一 |
| `role` | Unicode(10) | NOT NULL | `user` 或 `assistant` |
| `content` | UnicodeText | NOT NULL | 使用者原文，或 AI 的 `reply_text` |
| `intent` | Unicode(50) | NULLABLE, INDEX | 僅 assistant 訊息 |
| `entities_json` | JSON | NULLABLE | 驗證後的 entities |
| `suggested_actions_json` | JSON | NULLABLE | 驗證後的建議動作 |
| `confidence` | FLOAT | NULLABLE | |
| `msg_status` | Unicode(10) | NOT NULL, DEFAULT 'ok' | `ok` 或 `fallback`（降級回覆，不計入配額） |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | |

不得依 `entities_json` 內容做篩選查詢；統計使用 `intent` 欄位。

#### 6. LLM 呼叫紀錄表 (`llm_call_logs`)

兼作用量帳本與失敗紀錄，**不保存對話全文**。

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `user_id` | INTEGER | FOREIGN KEY (users.id), NULLABLE | 帳號刪除後設為 NULL（匿名化） |
| `conversation_id` | INTEGER | NULLABLE | |
| `kind` | Unicode(20) | NOT NULL | `chat`、`summary`（RPG 之後新增 `rpg_stream`、`rpg_delta`） |
| `status` | Unicode(20) | NOT NULL | `ok`、`validation_failed`、`timeout`、`api_error`、`blocked` |
| `prompt_tokens`、`completion_tokens` | INTEGER | NULLABLE | |
| `latency_ms` | INTEGER | NULLABLE | |
| `attempt` | INTEGER | NOT NULL, DEFAULT 1 | |
| `error_detail` | UnicodeText | NULLABLE | 失敗的模型原文，截斷至 2000 字，保存 14 天，僅管理員可讀 |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | |

#### 7. 其他支援表（於對應 change 建立）

| 資料表 | 用途 | 所屬 change |
|---|---|---|
| `admin_audit_logs` | 管理員操作稽核紀錄，保存 24 個月 | usage-safety-ops |
| `deletion_tombstones` | 刪除帳號的墓碑紀錄（僅內部 ID 與刪除日期），供還原備份後重新執行刪除 | usage-safety-ops |

### B. RPG 模組（延後實作，欄位維持 v2.1.1 定義）

> 下列三張表待 RPG 的 change 啟動時，依 docs/RPG.md 第 2 章調整（新增 `sessions.version`、`memory_logs.client_msg_id`、`turn_status` 等）。`scripts.creator_id` 與 `sessions.user_id` 外鍵指向上方新版 `users`。

#### 8. 劇本資料表 (`scripts`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 劇本唯一識別碼 |
| `creator_id` | INTEGER | FOREIGN KEY (users.id) | 創作者 ID |
| `title` | VARCHAR(100) | NOT NULL | 劇本名稱 |
| `world_view` | VARCHAR(50) | NOT NULL | 世界觀類別（如：古風仙俠） |
| `background_and_system` | TEXT | NOT NULL | 背景與境界體系詳細設定 |
| `characters_json` | JSON | NOT NULL | 重要 NPC 名冊（含預設好感度與性格） |
| `locations_json` | JSON | NULLABLE | 已知地點清單 |
| `milestones_json` | JSON | NOT NULL | 強約束必觸發事件 (Milestones)，**唯讀** |
| `state_transition_rules` | TEXT | NOT NULL | 狀態轉移規則（取代原結局條件規則，本平台無硬性結局） |

#### 9. 遊戲工作階段 / 玩家存檔表 (`sessions`)

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

#### 10. 對話與記憶體紀錄表 (`memory_logs`)

| 欄位名稱 | 型態 | 約束 | 說明 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 對話紀錄 ID |
| `session_id` | INTEGER | FOREIGN KEY (sessions.id) | 對應遊戲 Session ID |
| `turn_number` | INTEGER | NOT NULL | 對話回合數（一輪 = 玩家訊息 + AI 回應） |
| `user_action` | TEXT | NOT NULL | 玩家輸入或選擇的行動 |
| `llm_raw_response` | TEXT | NOT NULL | RPG 串流完整文字。串流中斷時的處理見 docs/RPG.md R-03 |
| `structured_result` | JSON | NULLABLE | 經 JSON 驗證後的結構化結果（如 NPC 好感度與狀態變更） |
| `memory_summary` | TEXT | NULLABLE | 累積摘要（建議移至 `sessions`，見 docs/RPG.md） |
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
| 紀錄009 | 2026-10-20 | NPC 好感度與動態狀態系統整合：原加入 favorability_change 與 status_effect 追蹤欄位，v2.1.0 改為併入 structured_result。 |
| 紀錄010 | 2026-10-22 | 極簡電影風 UI 與 Gemini API 整合校正：依 AGENTS.md 修訂視覺美學，廢除 SweetAlert2 改為 Vue 3 + Bootstrap 5.3 自製 Modal / Toast，確認離線架構與 Gemini API Key 串接。 |
| 紀錄011 | 2026-10-22 | SQLAlchemy ORM + SQLite + SQL Server 雙軌資料庫規範校正：加入「純 ORM 抽象原則」、「SQLite WAL 條件式初始化 / 方言隔離」與「SQLAlchemy JSON 欄位型態映射」。 |
| 紀錄012 | 2026-10-27 | 依 AGENTS_v2 全面對齊（v2.1.0）：廢除死亡與老死結局、RPG 改 SSE 串流與客服非串流 JSON、上下文改為「摘要 + 最近 10 輪 + 當前世界狀態」、模型統一為 `gemini-3.8-flash`、新增字型本地化與檔案編碼規範等（詳見 v2.1.1 同列）。 |
| 紀錄013 | 2026-10-27 | 與專案 AGENTS.md（對應 AGENTS_v3）對齊（v2.1.1）：Service 層補充對話摘要壓縮；主題六新增第 4 至 7 點待決定事項。 |
| 紀錄014 | 2026-10-27 | 納入 explore 決策（v2.2.0，詳見 docs/report.md 與 docs/RPG.md）：<br>• 範圍採方案 A：本期做客服，RPG 延後（D-00）。<br>• `users` 改以 email 為帳號識別（唯一），`username` 改為 `display_name`，`password_hash` 可為 NULL，新增 `user_identities`、`auth_tokens`、`role`、`member_tier`、`status` 等欄位。<br>• 新增客服資料表 `chat_conversations`、`chat_messages`，以及用量與失敗紀錄 `llm_call_logs`。<br>• 客服 Drawer 改稱 Sidebar；AGENTS.md 新增第 10 節（身分驗證與安全）與第 11 節（個資、保存與可觀測性），並補充第 4、5、6、7、9 節。<br>• 部署決策：Docker 映像部署於雲端 PaaS，使用託管 SQL Server；本機開發亦使用 SQL Server。<br>• 管理員後台採 Web 形式（`/admin`）；客服僅 AI，不設真人通道；訂單串接另開 change。 |
| 紀錄015 | 2026-10-27 | C-1 實作回饋補入 AGENTS.md 與 skill：<br>• 第 4 節：可為空的唯一欄位必須用部分唯一索引（SQL Server 的 UNIQUE 只允許一個 NULL）；`alembic.ini` 與 `script.py.mako` 必須純 ASCII（Alembic 以系統地區編碼讀取）。<br>• 第 8 節：以系統地區編碼讀取的檔案與腳本規則（含中文的 `.ps1` 須 UTF-8 with BOM；維運與驗收腳本一律以 Python 撰寫，`.ps1` 只作純 ASCII 薄包裝），並註明繁體中文規定的純 ASCII 例外。 |

---

## 主題六：待決定事項 (Open Items)

### 已結案（v2.2.0）

| 原編號 | 事項 | 結論 |
|---|---|---|
| 3 | 客服 `suggested_actions` 的前端呈現 | 快速回覆 chips，見 AGENTS.md 第 7 節 |
| 4 | Gemini API Key 的讀取方式 | 見 AGENTS.md 第 9 節 |
| 5 | JSON 驗證失敗的處理 | 見 AGENTS.md 第 5 節 |
| 7 | 客服資料表與 Intent / Entity 結構 | 見主題三 A 區與 docs/report.md D-01、D-02、D-05 |

### 屬 RPG 模組（延後，建議方案見 docs/RPG.md）

| 原編號 | 事項 | 建議方案位置 |
|---|---|---|
| 1 | RPG 狀態變更的取得方式 | RPG.md R-04：串流後另以一次非串流請求取得狀態變更 JSON |
| 2 | `state_transition_rules` 的格式 | RPG.md R-05：維持 TEXT，並加 Service 層不變量檢查 |
| 6 | 串流中斷時的紀錄處理 | RPG.md R-03：保持 NOT NULL，新增 `turn_status` |

### 新增的待決定事項（來自 explore 查核，詳見 docs/report.md 第 4 章）

| 編號 | 事項 | 狀態 | 影響的 change |
|---|---|---|---|
| 8 | 雲端平台供應商 | **已決定**：Google Cloud（Cloud Run、Cloud SQL for SQL Server、Secret Manager）；Google AI Studio 僅用於取得 Gemini 金鑰 | production-launch |
| 9 | 訂單系統整合規格：資料來源與 API、使用者與訂單的對應、退款為申請或直接執行、回覆是否採二階段 | **已決定方向**：預設以模擬訂單服務（`OrderProvider` 介面）開發，日後換真實 API；若已有真實系統再提供文件。其餘子項見 docs/report.md 4.3 | order-integration |
| 10 | Gemini 免費方案的配額數字 | 使用者先使用免費方案；配額數字待提供，正式對會員開放前須重新評估方案 | usage-safety-ops |
| 11 | 網域名稱 | 預備環境先用雲端平台預設網域，正式上線前取得（上線前關卡） | production-launch |
| 12 | email 唯一性細節 | **已決定**：Gmail 別名去點與 +標籤比對（`email_canonical`）；刪除後可重新註冊；`display_name` 不唯一 | member-auth |
| 13 | 管理員後台細節 | **已決定**：一次性指令建立首位管理員；只看統計、意見回饋與被標記對話；高風險操作重輸密碼；v1 不限制來源 IP | usage-safety-ops |
| 14 | 註冊同意紀錄與隱私權政策 | **已決定**：記錄 `terms_version` 與 `terms_accepted_at`；政策與條款為應用程式內頁面；文字由 AI 擬草稿，須法務覆核 | member-auth |
| 15 | 本機 SQL Server 連線帳號 | **已決定**：本機沿用 `sa`；正式環境禁用 | platform-foundation |
| 16 | 託管 SQL Server 費用與「先使用免費」的衝突 | 預設延到 C-5 評估（C-1 至 C-4 在本機開發） | production-launch |
| 17 | 預備環境的寄信方式 | **已決定**：預備環境不做信箱驗證與寄信（`EMAIL_VERIFICATION_REQUIRED=false`，忘記密碼一併停用）；正式上線前開啟並設定寄信服務（上線前關卡） | member-auth |
| 18 | 開發工具 | Git 已安裝，提交者資訊已提供；Windows 無法更新到 22H2，Docker Desktop 4.97 無法安裝，**已決定**改用 WSL 2 加 Docker Engine（備案 A，詳見 docs/report.md 4.2），已安裝並驗證：WSL 2 發行版 `Ubuntu-2204`、Docker Engine 29.9.2 | platform-foundation |
| 19 | 客服知識來源（`general_qa` 回答什麼內容） | **已決定**：v1 只回答平台自身資訊，有 FAQ 時再擴充，不做 RAG | cs-chat-core |
| 20 | 字型選擇 | **已決定**：Noto Sans TC，woff2 切片自行託管 | platform-foundation |

---

本文件以專案 AGENTS.md（對應 AGENTS_v3，含 explore 決策後的第 10、11 節）為準進行對齊。若與 AGENTS.md 有任何衝突，以 AGENTS.md 為準，並於主題五另行記錄。
