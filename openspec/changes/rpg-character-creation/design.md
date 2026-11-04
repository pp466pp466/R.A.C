# Design

## Context

動機與範圍見 proposal.md，行為見四份規格。規範（AGENTS.md 第 5、6、7、10、11 節）已於 `4f91946` 修訂，探索結論在 `docs/RPG.md` T-13。

現況：
- `SessionService.start_session(current, version_id, *, player_name, character_age)` 只驗證姓名與年齡；**目前沒有任何 HTTP 端點開存檔**（只有 `registry.get_session_service()`）。
- 劇本角色的驗證寫在 `script_spec._validate_characters`（私有、針對清單）。審查層的規則引擎 `script_rules.check_rules(content, tier=…)` 取固定規格的全部文字欄位切句，詞表為 `app/data/review/lexicon.json`；模型層 `ScriptReviewService.review_model` 走 `LlmService.run_structured`。
- `runtime_limits` 以鍵逐列存放、每次現讀（`RuntimeLimits.get`），`flask set-script-limit` 只認 `LIMIT_SPECS` 內的鍵。
- 用量帳本 `llm_call_logs.kind` 已是 `Unicode(20)`；`attempt` 欄位記第幾次嘗試；索引 `(user_id, created_at)` 可用於每日計次。
- `sessions` 有 `entities_json`（NPC 狀態，初始為空，其結構由 RC-2 定義）、`player_name`、`character_age`。

限制：AGENTS.md 第 4 節（純 ORM、遷移 expand/contract、JSON 欄位不可依內容篩選）、第 5 節（解析單一函式、`OPENROUTER_*` 與 Gemini 分用）、第 7 節（主角規則）、第 9 節（不依賴行程內狀態）、第 10、11 節（玩家文字只進資料區塊、個資不進日誌）。

## Goals / Non-Goals

**Goals:**
- 讓「生成預覽」與「開存檔」兩件事彼此獨立：預覽可以重複、被拒、失敗而不留任何狀態；開存檔是原子的，失敗不留半個存檔。
- 主角的驗證、審查與個資規則與劇本角色一致，不分叉出第二套。
- RC-2 與 RC-3 拿得到穩定的資料形狀（`protagonist_json`）與 API。

**Non-Goals:**
- 序章、聯繫、創角三個畫面（RC-3）；第 0 幕與把主角放進世界狀態、提示詞（RC-2）。
- 在 `entities_json` 定義 NPC 卡片切片的結構（RC-2）；本 change 不動它。
- 多個錨定事實、主角的人工審核、存檔數量上限、開存檔的冪等鍵。

## Decisions

### D1. 資料模型與遷移 `0014_rpg_character_creation`（只新增）
- `sessions.protagonist_json`：`JSON`、可為空。內容 `{"character": {name, personality, age, appearance, occupation, speech_style, hidden_trait, image}, "anchor": {kind, ref, text} | null}`；`image` 恆為 `null`。不依內容篩選查詢。
- `player_name`、`character_age` 欄位**保留**，由 Repository 在同一個 INSERT 以角色卡的 `name`、`age` 寫入。理由：expand/contract——舊程式、匯出、保存期限與既有測試都讀這兩欄；兩欄與 JSON 的一致性由測試守住（建立時比對，並有架構測試掃描「沒有其他地方寫這兩欄」）。移除兩欄屬日後的 contract 階段，本 change 不做。
- 錨定事實**放在 `protagonist_json` 而不是 `entities_json`**：`entities_json` 的結構由 RC-2 的 NPC 卡片切片決定，現在寫入會綁死 RC-2 的設計。RC-2 讀取 `protagonist_json.anchor`，把它併入所指向人物的切片（或地點、里程碑的說明）。
- `runtime_limits`：遷移以 `op.bulk_insert` 補三列（`protagonist_suggest_daily.free／plus／vip` = 5／15／40）。**升級時可能已有先前的七列**，所以只插入缺少的鍵（先查再插，沿用 `0013` 的冪等寫法）；降級只刪這三列。
- 降級：欄位以 `recreate="never"` 寫法移除（沿用 `0010`、`0012`、`0013`）。
- 替代方案：獨立的 `session_protagonists` 資料表（多一條 `users → sessions → …` 的串聯路徑與一次 JOIN，主角與存檔一對一、生命週期相同，沒有好處）；把角色欄位攤成多個欄位（錨定事實與角色卡的形狀日後可能演進，JSON 較有彈性，且沒有依內容篩選的需求）。

### D2. 角色驗證拆出為可單獨使用的函式
`script_spec.validate_character(raw, *, path, forbidden_markers, collector=None)`：把 `_validate_characters` 迴圈內的單一角色邏輯抽出，劇本驗證改為逐一呼叫它（行為與錯誤欄位路徑完全不變，由既有測試與新增的等價測試守住）。主角呼叫時路徑前綴為 `protagonist`，姓名不重複的檢查（劇本內）不適用。回傳標準化的角色字典與錯誤。為什麼不複製：複製會讓劇本角色與主角的上限日後分岔，違反「沿用劇本角色規格」。

### D3. 錨定事實驗證與未成年的戀愛／婚約守衛（純函式，`app/services/protagonist.py`）
- `validate_anchor(raw, *, script_content, protagonist_age, adult_age, lexicon, forbidden_markers)`：`kind` ∈ {`character`, `location`, `milestone`}；`ref` 依種類比對標準化後的劇本內容（人物以姓名、地點以名稱，皆 NFC＋不分大小寫；里程碑以編號精確比對）；`text` 單行、1 至 60 字、NFC、去控制字元。回傳標準化結果與逐欄位錯誤（`anchor.kind`／`anchor.ref`／`anchor.text`）。
- 守衛：當 `protagonist_age < adult_age`，或 `kind = character` 且該人物 `age < adult_age`，`text` 不得含詞表群組 `romantic_relation` 的詞。詞表放進既有的 `lexicon.json`（新群組、版本字串更新），以同一個載入函式讀取，程式不內嵌詞（沿用 script-review 的作法）；群組內容的初稿（戀人、情侶、未婚夫／妻、道侶、夫妻、婚約、定親、結為連理、愛慕、暗戀等）由實作時擬定，提交前請使用者審閱。
- 守衛是**欄位錯誤**而不是審查 `reject`：它是結構規則（像長度上限），不是內容裁決，所以不帶審查類別、不進任何拒絕計次，玩家看得到是哪個欄位要改。詞表抓不到的語意（「許了終身」）由模型層與提示詞補強，這是已知的限制。
- 成年門檻取 `AccessPolicy.adult_age`（年齡規則的單一來源），不寫死 18。

### D4. 審查：規則層共用引擎，模型層另立呼叫類別
- **規則層**：`script_rules` 把 `check_rules` 的核心拆成「給一串文字與角色年齡、回傳判定」的內部函式，`check_rules`（劇本）與新增的 `check_protagonist_rules(character, anchor_text, intro, *, tier)`（主角）共用。主角版本把角色卡文字、錨定事實與自我介紹各自切句檢查（不跨欄位串接），年齡取主角年齡；`minor_suspected` 的判定同樣適用（主角年齡小於成年門檻且內容模式為一般或成人時，個人文字出現性相關詞 → `flag`）。
- **模型層**：`ProtagonistReviewService.review(user_id, protagonist, anchor, tier)`，呼叫類別 `protagonist_review`，沿用審查的結構化輸出（verdict、categories）與語意規則，**提示詞另立**（`protagonist_prompt.py`）：資料是一張主角角色卡與一句錨定事實（加上劇本中被指向的項目名稱，供判斷關係合理性），審查標準與劇本審查一致，並特別提示未成年的戀愛或性的關係。結果**不快取**（沒有可附著的版本列；每次開存檔審查一次）。
- 結果處置：`reject` → 422 `protagonist_rejected`（類別列舉）；`flag` → 放行；`unavailable`（未設定、預算用盡、降級、被過濾）→ 503 `review_unavailable`，不放行。理由：主角只屬於玩家自己的存檔，沒有人工審核佇列可接 `flag`，絕對禁止項靠 `reject` 與回合層（RC-2）的審查把關；與劇本不同，沒有「公開給他人」的風險。
- 替代方案：把主角塞進劇本審查的 `ModelReview.ensure_reviewed`（它以版本列的內容雜湊快取結果，主角沒有版本列）；用簽章令牌讓預覽通過審查後開存檔時免審（多一組金鑰輪替與令牌失效的複雜度，預覽的候選本來就會被玩家修改，節省的呼叫有限）。

### D5. 生成預覽：無狀態、一次呼叫
- `ProtagonistService.suggest(current, version_id, intro)` 順序：①`require_chat_access` ②取得可見的版本（與開存檔相同的可見性）與內容模式 ③驗證自我介紹（NFC、去控制字元、≤300 字）④規則層檢查自我介紹（`reject` → 422，**不計次**）⑤每日次數（讀 `runtime_limits`，計 `llm_call_logs` 中該使用者、類別 `protagonist_gen`、`attempt = 1`、本台灣日的筆數；用盡 → 429 `suggestion_limit`）⑥RPG 模型未設定或全站預算用盡 → 503 `suggestion_unavailable`（**不計次**）⑦`run_structured(kind="protagonist_gen", conversation_id=None, …)` ⑧對輸出再跑角色驗證、錨定事實驗證與規則層（這三項寫成 `SemanticRule`，失敗進入既有的「驗證失敗同模型重試一次」）⑨回傳候選與剩餘次數。
- 結構描述：`character`（七個欄位）與 `anchor`（kind、ref、text），`additionalProperties: false`，沒有自由文字的說明欄位。語意規則：角色驗證通過、錨定事實通過、輸出不含不公開標記（HMAC 標記，標籤 `protagonist-marker-v1`，與劇本審查的標記分開）。
- 提示詞：繁體中文；內容模式定義沿用審查提示詞的三段（safe／standard／adult），要求生成的角色與聯繫不超出該模式；未成年的戀愛或性的關係一律不得出現；錨定事實必須指向資料區塊中列出的人物、地點或里程碑；自我介紹提到年齡時角色年齡須相符，沒有自我介紹則由背景故事決定。劇本的背景、人物、地點、里程碑與自我介紹都在資料區塊。資料區塊裡劇本人物只給姓名、年齡、職業、個性（**不給 `hidden_trait`**：玩家不應從生成結果推得他人的隱藏特質）。
- **計次用帳本、不另建資料表**：`attempt = 1` 的筆數就是「實際呼叫模型的請求數」。好處是不多一張表、與全站預算同一份資料；代價是帳本保存 12 個月且使用者刪除後 `user_id` 為空，不影響每日計次。新增 `LlmCallLogRepository.count_first_attempts(user_id, kind, start, end)`（僅 ORM 計數）。
- 並行：兩個請求同時通過次數檢查可能多呼叫一次；與劇本送審相同，上限是營運節流不是安全邊界，不另做鎖定。
- 為什麼預覽無狀態：玩家可能反覆生成、修改、放棄；寫入資料庫只會製造要清理的半成品，也會把自我介紹留在資料庫（違反資料最小化）。

### D6. 開存檔 API 與 `SessionService.start_session` 改版
- 簽名：`start_session(current, version_public_id, *, protagonist, anchor, skip_anchor)`。順序：①`require_chat_access` ②取版本（404）③內容模式（403）④劇本審查把關（沿用 `ReviewGate.ensure_reviewed`；內建劇本豁免）⑤**主角驗證**（D2）與**錨定事實存在性**（`anchor` 與 `skip_anchor` 二選一，皆無 → `anchor` 欄位錯誤）與**守衛**（D3）⑥**規則層**（D4）⑦**模型層**（D4）⑧`SessionRepository.create(…, protagonist=…)`。所有欄位錯誤一次回報；⑥⑦失敗不寫資料庫。
- 內容模式 `tier`：與存檔相同——玩家生效模式與劇本宣告中較保守者。
- 控制器：新 `session_api_controller.py`（Blueprint `/api/sessions`）：`GET /setup/<version_id>`、`POST /protagonist/suggest`、`POST ""`；固定路徑放在前面，沿用 `read_json_object`、`require_login`、統一錯誤轉換；`/api/sessions/` 加入 `cache_headers.py` 的禁止快取前綴。受限登入狀態由 `require_login` 預設拒絕。
- 逾時：開存檔最長約 30 秒（劇本審查若尚未審過加上主角審查，各自受 `request_budget_seconds` 約束，最壞兩次依序，約 60 秒）。**RC-3 的前端 axios 逾時須設為 65 秒並於離開頁面時中止**；本 change 在 API 文件與 README 記載。為了降低最壞情況：劇本審查結果已快取者（已上架的劇本在送審時已審查）不再呼叫，所以常態只有主角審查一次。
- 回應：201 `{ok, session: {id, title, protagonist, anchor}}`；`id` 是存檔的對外識別碼（UUID）。

### D7. 創角資料 `GET /api/sessions/setup/<version_id>`
回傳 `{title, background, characters: [{name, occupation}], locations: [...], milestones: [{id, title}], limits: {name, personality, …, intro, anchor_text}, suggestions_remaining}`。人物只給姓名與職業（**不給年齡與隱藏特質**：年齡是錨定事實的判定依據之一，但前端不需要；隱藏特質是劇情祕密）。限制值取自 `script_spec` 常數與 `protagonist` 常數，單一來源。剩餘次數讀 `runtime_limits` 與帳本。可見性與錯誤與開存檔相同（404）。

### D8. 限制、帳本與觀測
- `runtime_limits.py`：`SUGGEST_DAILY_PREFIX = "protagonist_suggest_daily"`，`LIMIT_SPECS` 以 `_per_tier` 加三個鍵（0 至 100）；`suggest_daily_for(tier)` 沿用「不認得的等級套用 free」。`flask set-script-limit／show-script-limits` 不用改（它們讀 `LIMIT_SPECS`），輸出自動變成十個鍵。
- `llm_call_log.py`：`KIND_PROTAGONIST_GEN = "protagonist_gen"`、`KIND_PROTAGONIST_REVIEW = "protagonist_review"` 加入 `CALL_KINDS`；長度 15 與 18 字，欄位已是 20 字。全站每日 token／呼叫彙總不分類別，自動計入；每日對話輪數只看 `kind = chat`，不受影響；後台的對話統計也只看 `chat`。
- 事件（`app/observability/`）：`protagonist_suggested`（內部使用者編號、模型名稱、嘗試次數、結果列舉、剩餘次數）、`protagonist_rejected`（類別列舉）、`session_started`（內部存檔編號、內容模式、是否有錨定事實）；函式簽名**不接收任何文字**（沿用 `llm_events` 的作法）；日誌掃描測試擴充到整個流程。

### D9. 生命週期
- 匯出：`personal_export_service` 的存檔區塊加 `protagonist`（角色卡各欄位與錨定事實），CSV 的 `sessions.csv` 加對應欄；舊存檔沒有 `protagonist_json` 時只輸出 `player_name`、`character_age`。不匯出自我介紹（本來就不存在）。
- 抹除與保存期限：`protagonist_json` 在 `sessions` 內，隨存檔串聯刪除與 12 個月期限清除；`AccountErasureService` 與 `purge-expired-data` 不用改，守門測試加上「存檔有主角資料時一併消失」。
- 管理員：沒有任何新路徑；既有守門測試（管理員讀不到存檔）擴充斷言主角資料。

## Risks / Trade-offs

- [每次開存檔多一次審查呼叫，加上最多兩次生成] → 常態是 1 次審查；生成有每日上限；token 計入全站預算，預算用盡時優雅降級為自創。
- [`flag` 放行：主角沒有人工審核] → 絕對禁止項靠規則層／模型層 `reject`；回合層（RC-2）對模型輸出再審；`flag` 與 `reject` 的差別寫進測試。若日後發現 `flag` 常是真問題，再改為擋下（不影響資料形狀）。
- [戀愛／婚約守衛靠詞表，抓不到婉轉說法] → 提示詞與模型層補強；詞表為資料檔、有版本，可在不改程式下調整；這是已知限制，寫進測試的「反向案例」說明邊界。
- [開存檔最壞約 60 秒（兩次審查依序）] → 劇本審查常態命中快取；RC-3 的前端逾時設 65 秒、按鈕禁用；監控屬 RC-4。
- [生成次數並行超額] → 節流而非安全邊界，與送審相同，不另做鎖定。
- [雙寫 `player_name`／`character_age` 與 `protagonist_json` 可能分歧] → 唯一寫入點是 `SessionRepository.create`，AST 架構測試掃描其他地方不得寫這兩欄；contract 階段再移除。
- [錨定事實指向的人物日後被作者改名] → 存檔複製了劇本內容，`ref` 以存檔內的複本為準，不受影響（`rpg-sessions` 既有保證）。
- [自我介紹送到 RPG 供應商] → 屬個資；規範已要求一般與成人模式不得使用免費模型、正式前改付費並啟用零保存（第 11 節）；本 change 不改變這個前提，README 與隱私權政策待辦已記錄。
- [沒有冪等鍵，連點兩次可能開出兩個存檔] → 前端（RC-3）在請求期間禁用按鈕；存檔數量沒有上限也沒有唯一性要求，重複的存檔是可接受的後果；日後要加冪等鍵可新增可為空的欄位與部分唯一索引，不影響這次的形狀。
- [舊程式遇到新欄位] → `protagonist_json` 可為空且舊程式不讀它；`runtime_limits` 多三列對舊程式無影響。

## Migration Plan

1. 備份資料庫。
2. 執行遷移 `0014`（新增一個可為空的欄位與三列限制，舊版程式不受影響）。
3. 部署新版程式。
4. 執行 `flask show-script-limits` 確認十個值（含三個新的）。
5. `alembic current`、`alembic check` 確認沒有漂移；依 `docs/project資料結構文件.md` 的規則，結構文件與第 14 節異動紀錄隨 change 一起更新，升級開發用資料庫後再追加一列「套用」。
6. 回復：降級遷移移除欄位與三列；程式回到舊版即可。

## Open Questions

- `romantic_relation` 詞表的初稿與主角生成、審查的提示詞用語：實作時擬定，提交前請使用者審閱（只影響資料檔與提示詞，不影響做法）。
- 生成次數的初始值（5／15／40）與自我介紹、錨定事實的字數上限（300／60）：暫定，之後以實際 token 預算與使用情形校正，改 `runtime_limits` 或常數即可。
- 隱私權政策與服務條款補充自我介紹與主角的措辭：留給法務關卡，不在本 change。
