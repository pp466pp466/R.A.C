# Proposal

## Why

RC-1 與 RC-7 之後，玩家已能創作並用劇本開存檔，但「玩家自己」在存檔裡只是兩個欄位：主角姓名與角色年齡（`SessionService.start_session` 的 `player_name`、`character_age`），沒有外貌、個性等角色資料，也沒有任何方式讓主角與劇本裡的人物、地點產生關聯。2026-11-04 的探索（`docs/RPG.md` T-13）與 AGENTS.md 第 5、6、7、10、11 節的修訂已經決定：玩家依「劇本角色規格」自創主角，可以寫一段選填的自我介紹請 AI 生成角色卡與**一個**錨定事實（與劇本內某個人物、地點或里程碑的關係），也可以自創；按「開始冒險」才開存檔。

沒有這一層，RC-2 的回合引擎無從產生「第 0 幕」開場，也無法把主角的特質與聯繫放進世界狀態；RC-3 的序章、聯繫、創角三個畫面也沒有可以呼叫的 API。這個 change 先把資料、驗證、審查、AI 生成預覽與開存檔 API 做完整。

## What Changes

- **主角角色卡**：欄位、長度與驗證**直接重用劇本角色規格**（姓名、年齡、外貌、個性必填，職業、說話方式、隱藏特質選填，圖片 v1 一律拒絕），驗證函式由劇本驗證器拆出可單獨使用的角色驗證，不另寫一套。主角年齡是劇中角色的年齡，與使用者年齡無關；開存檔後是錨定事實，沒有任何方法可以修改。
- **錨定事實**：每位主角至多一個，結構為 `{kind: character｜location｜milestone, ref, text}`：`ref` 必須指向該存檔劇本內容中存在的人物（姓名）、地點或里程碑（編號），`text` 是一句話（上限暫定 60 字）。由 Service 驗證；模型只提議，玩家接受或自行撰寫後才寫入。自創時預設要填，玩家可明確略過（略過時存檔沒有錨定事實）。**主角或對象之一未滿 18 歲時，聯繫不得是戀愛或婚姻關係（含已成婚與童養媳）；婚約類（娃娃親、指腹為婚）是歷史題材的背景設定，只在成年與未成年之間才擋**：以資料檔詞表的新群組判定，違反是欄位錯誤（不計入任何拒絕計數），模型層再補強。
- **AI 生成預覽**（無狀態，不寫存檔）：`POST /api/sessions/protagonist/suggest`，本文為劇本版本與選填的自我介紹；用 RPG 用途的模型（`OPENROUTER_MODEL`，不使用 Gemini），非串流、結構化輸出，回傳一張角色卡加一個錨定事實的候選。自我介紹是使用者文字，只放資料區塊、不進 `system_instruction`；先過規則層才送模型；輸出經角色驗證與錨定事實驗證，不合格依既有規則重試一次，仍失敗回 503 `suggestion_unavailable`，前端據此改走自創。**生成與重新生成每個台灣日有次數上限**（依會員等級，存資料庫的設定表、每次現讀）。自我介紹**不寫入資料庫**。
- **開存檔 API**：`POST /api/sessions`（本文為劇本版本、主角角色卡、錨定事實或明確略過）與 `GET /api/sessions/setup/<version_id>`（序章與創角畫面需要的資料：背景故事、可選的人物／地點／里程碑、欄位上限、今日剩餘的生成次數）。開存檔依序：年齡閘門、取得版本、內容模式、劇本審查把關（沿用）、**主角驗證、規則層、模型層審查**、寫入。模型層不可用一律擋下（503 `review_unavailable`），被拒絕回 422 `protagonist_rejected`（只有類別）；有疑慮（`flag`）放行，因為主角只屬於玩家自己的存檔、沒有人工審核佇列，絕對禁止項靠 `reject` 與回合層的審查擋下。
- **存檔資料**：`sessions` 新增可為空的 `protagonist_json`（角色卡加錨定事實）；`player_name`、`character_age` 保留並由角色卡同步寫入（expand 階段，舊程式照常運作）。舊存檔的 `protagonist_json` 為空，讀取端容忍。
- **限制存資料庫**：`runtime_limits` 新增 `protagonist_suggest_daily.free／plus／vip`（0 至 100）三個鍵，遷移寫入初始值；沿用 `flask set-script-limit`／`show-script-limits` 修改與稽核。
- **用量與日誌**：`llm_call_logs` 新增呼叫類別 `protagonist_gen`、`protagonist_review`（`kind` 欄位已是 20 字，不必加寬）；token 計入全站每日上限，**不計入每日對話輪數**；事件只含內部編號、整數長度與列舉。每日次數以帳本中 `attempt = 1` 的筆數計，不新增資料表。
- **生命週期**：個人資料匯出的存檔區塊納入主角角色卡與錨定事實；抹除、保存期限隨存檔（`protagonist_json` 在 `sessions` 內，不需新增路徑）；管理員預設不可檢視。
- **不做**：序章、聯繫、創角三個畫面（RC-3）；開場的「第 0 幕」與把主角、錨定事實放進世界狀態與提示詞（RC-2）；多個錨定事實；主角的人工審核；圖片；`/privacy` 與 `/terms` 的措辭（法務關卡）；存檔數量上限與開存檔的冪等鍵。

## Capabilities

### New Capabilities

- `rpg-protagonist`: 玩家主角：重用劇本角色規格的角色卡、年齡錨定、錨定事實的結構與驗證、未成年的戀愛／婚姻禁止、開存檔 API 與創角所需資料的 API、規則層與模型層審查、存放與個資處理。
- `protagonist-suggestion`: AI 生成預覽：無狀態、自我介紹只作資料、輸出驗證與重試、失敗改走自創、每日次數上限、用量帳本與日誌限制。

### Modified Capabilities

- `rpg-sessions`: 「存檔的初始狀態」改為記錄主角角色卡與錨定事實（姓名與年齡由角色卡同步）；「存檔與回合的刪除與日誌」補上主角內容不進日誌。
- `runtime-limits`: 限制的鍵增加三個（生成次數）、初始值、`show-script-limits` 的輸出改為十個。
- `personal-data-export`: 存檔區塊納入主角角色卡與錨定事實。
- `service-observability`: 白名單事件新增主角生成、審查與開存檔事件，不含任何主角文字或自我介紹。
- `llm-structured-calls`: 用量帳本的用途列舉納入 `protagonist_gen`、`protagonist_review`。

## Impact

- **資料庫**：遷移 `0014_rpg_character_creation`（只新增，expand）：`sessions.protagonist_json`（`JSON`，可為空）；`runtime_limits` 以 ORM 寫入三列初始值。SQLite 與 SQL Server 雙軌都要通過；降級移除欄位與三列。**同步 `docs/project資料結構文件.md`（字典、固定值、第 12 節 migration 歷程）並在第 14 節追加異動紀錄**。
- **程式**：`app/models/rpg_session.py`、`app/services/script_spec.py`（拆出角色驗證）、新 `protagonist.py`（卡與錨定事實的純函式驗證與詞表守衛）、`script_rules.py`（共用句子引擎，新增主角版本的檢查入口）、`app/data/review/lexicon.json`（新增戀愛／婚姻關係群組與版本）、新 `protagonist_prompt.py`、`protagonist_service.py`、`session_service.py`（開存檔改版）、`runtime_limits.py`、`app/repositories/session_repository.py`、`llm_call_log_repository.py`（次數計算）、新 `session_api_controller.py`、`app/services/errors.py`（新錯誤類別）、`app/observability/`（事件）、`personal_export_service.py`。
- **LLM**：RPG 用途的 `LlmService`（`run_structured`，兩個新呼叫類別）；每次開存檔最多多 1 次審查呼叫，每次生成 1 至 2 次呼叫。
- **測試**：角色驗證重用的等價測試（劇本角色與主角同一套）、錨定事實的指向與未成年守衛（含反向案例）、規則層與模型層（假供應商，不打真實 API）、生成次數與並行、無狀態保證（生成不寫資料庫）、自我介紹不落地、開存檔失敗不留半個存檔、匯出與抹除、日誌掃描、遷移雙軌、容器驗收。既有 `start_session(player_name, character_age)` 的測試改用新介面。
- **文件**：`docs/資料模型-data-model.md`、`docs/project資料結構文件.md`、`docs/系統架構-architecture.md`、`docs/模型呼叫層-llm-layer.md`、`docs/帳號與安全-auth-security.md`、`docs/營運與部署-operations.md`、`docs/路線圖-roadmap.md`、`docs/RPG.md`、README（端點表）。AGENTS.md 已於 `4f91946` 修訂，這個 change 不再修改它。
- **不影響**：客服、登入流程、既有存檔與回合的行為、劇本創作與送審。
