# Design

## Context

- 決定來源：`docs/RPG.md` 7.10（故事偏好）、7.17（RC-3 探索：獨立成 `story-preference`、不修訂 AGENTS.md）。動機見 proposal.md。AGENTS.md 第 7 節已規定：偏好預設全讀者、只影響劇本目錄排序、排序為符合偏好、全讀者、另一頻；讀者頻道為單選、既有劇本視為全讀者、屬版本內容。
- 劇本內容的流向：`script_spec.validate_script` 產生標準化內容與 `content_hash` → `ScriptRepository._content_columns` 攤平成 `script_versions` 的欄位（角色、地點、里程碑為 JSON）→ `content_of(version)` 由欄位還原。`content_hash` 是規範化 JSON 的 SHA-256，**任何新增進標準化內容的鍵都會改變所有既有內容的雜湊**。
- 雜湊被誰比對：`update_draft(expected_hash)` 與審查結果（`review_hash` 必須等於 `content_hash` 才算數）比對的是**儲存的**雜湊；內建劇本種子（`ScriptSeedService.run`）用「重新驗證資料檔得到的雜湊」比對上架版本儲存的雜湊，判斷未變更或更新。
- 使用者偏好的既有樣板：`users.content_tier_preference`（NOT NULL，模型 `default` 加 `server_default`）、`UserRepository.set_content_preference`（條件式 UPDATE）、`ContentModeService`、`/api/account/content-mode`。`/account` 的初始狀態來自 `AuthService.profile` → `CurrentUser.public_dict`。
- 目前沒有任何玩家端點呼叫 `ScriptRepository.list_published`／`list_published_by_tag`（RC-3a 才有）。

## Goals / Non-Goals

**Goals:**
- 偏好與頻道的值域、預設與排序規則各自只定義一次（純函式模組），Repository、Service 與測試共用。
- 部署對既有資料零擾動：既有內容的雜湊不變、不回填、不使內建劇本被重複發佈、舊版程式在新結構上照常運作。
- 排序在 SQL 層完成（分頁之前），不在記憶體重排一頁資料。

**Non-Goals:**
- 可玩劇本清單的端點與畫面（RC-3a）、依頻道篩選或隱藏、`/age` 精靈的偏好步驟（`onboarding-polish`）、頻道的審查或統計。
- 偏好與頻道不進模型、不進日誌欄位白名單、不影響額度。

## Decisions

### D1：`audience` 為全讀者時不進標準化內容（雜湊相容）

標準化內容只在 `audience` 為 `male`／`female` 時帶有該鍵；缺值、空值與 `all` 一律不寫入。`content_of` 對應：欄位為 `male`／`female` 才還原該鍵，空值與 `all` 都不還原。

- **為什麼**：「缺值視為全讀者」直接就是這個表示法。這樣所有既有內容（草稿、待審、已發佈、內建劇本）的 `content_hash` 與升級前**逐位元相同**：待審版本的審查結果不失效、`update_draft` 的衝突偵測不會誤報、`flask seed-scripts` 重跑仍判定為未變更（否則升級後第一次重跑會憑空發佈一個內容完全相同的第 2 版，並讓所有進行中的存檔來源版本變成 `superseded`）。
- **替代方案**：①標準化內容永遠帶 `audience`（結構規整，但上述三項都被擾動，要靠回填或特例處理，且違反「已發佈版本不可變」的精神）；②`audience` 不進內容雜湊、只當獨立欄位（改頻道不會改變雜湊，審查結果與「內容變了要重新送審」的機制就抓不到頻道的變更，而 AGENTS.md 要求頻道修改須重新送審）——否決。
- **規格版本維持 1**：新增選填欄位向下相容，且 `validate_script` 只接受等於目前版本的 `spec_version`，升版會使所有已儲存的內容無法再驗證。
- 內容形狀因此是「頻道欄位可有可無」；程式讀取一律用 `content.get("audience")` 並經 `parse_audience` 套預設，不假設鍵存在。

### D2：值域與排序集中在一個純函式模組 `story_preference`

新增 `app/services/story_preference.py`（純函式，沒有資料庫、設定與時間的依賴；值域常數自 `app.models.user` 匯入，所以與 `content_tier.py` 一樣不在 `PURE_MODULES` 清單內——那份清單禁止匯入模型）：常數 `AUDIENCE_ALL／MALE／FEMALE` 與 `AUDIENCES`、`parse_audience(value)`（非列舉值回 `None`）、`normalize_audience(value)`（空值回 `all`）、`rank(preference, audience)`（0 最前）。`script_spec`、Service 與測試都從這裡取值域，不各寫一份字串；**Repository 不得匯入 Service（分層守門），所以只用 `app.models.user` 的常數，排序的 `case` 規則與 `rank` 各寫一次，由 Repository 測試逐一比對兩者的結果以守住一致**。

- 模型層需要預設值常數（`users.story_preference` 的 `default` 與 `server_default`），而資料模型不得匯入 Service：沿用 `CONTENT_TIER_*` 的做法，把三個常數放在 `app/models/user.py`，純函式模組自模型匯入。
- `rank`：偏好 `all` → 一律 0（不重排）；偏好 `male`／`female` → 同頻 0、全讀者（含空值）1、另一頻 2。

### D3：排序用 ORM `case` 在 SQL 層完成

`list_published(max_tier, *, limit, offset, preference=None)` 與 `list_published_by_tag` 在 `order_by` 前面加一個依 `rank` 產生的 `case(...)` 表達式（`ScriptVersion.audience` 等於偏好 → 0、為空或 `all` → 1、其餘 → 2），其後仍是 `published_at desc, id desc`。`preference` 為 `None` 或 `all` 時**不加**這個排序鍵，產生的查詢與現在完全相同。

- **為什麼**：排序必須在 `LIMIT/OFFSET` 之前，否則每頁各自排序、跨頁順序不一致。`case` 是 ORM 表達式，符合「嚴禁 Raw SQL」，SQLite 與 SQL Server 行為一致（不依賴 NULL 排序方向的方言差異，因為 NULL 在 `case` 中被明確映射）。
- **為什麼放在 Repository 而不是 Service 重排**：Service 只拿得到一頁資料，無法做全域排序。
- 沒有玩家端點使用它，所以這個 change 的驗證靠 Repository 層測試（SQLite 與真實 SQL Server）；RC-3a 接上時只需傳入 `current.story_preference`。

### D4：資料庫（遷移 `0017`，expand）

- `users.story_preference`：`Unicode(10)`，NOT NULL，`server_default='all'`。模型同時設 `default`，所以舊版程式的 INSERT（不含此欄）在新結構上取得 `all`。與 `content_tier_preference` 相同模式。
- `script_versions.audience`：`Unicode(10)`，可為空，不設預設、不回填。新版本寫入 `male`／`female`／`all`（全讀者寫 `all` 而非空值，讓「明確選了全讀者」與「遷移前的舊資料」可區分，兩者行為相同）。
- 不建索引：目錄量體小，排序鍵是 `case` 運算式，篩選仍由既有的 `(status, content_tier)` 索引處理。
- 值域由 Service 驗證，不加 CHECK 約束（CHECK 在 SQLite batch migration 與 SQL Server 之間有方言差異，沿用專案慣例）。
- 回復（downgrade）：`recreate="never"` 直接刪欄位；`story_preference` 的預設值約束在 SQL Server 需先移除（`mssql_drop_default`），作法同 0016 的 `attempt`。遷移檔不匯入應用程式程式碼；不含原生 SQL。
- **部署順序**：備份 → 遷移 → 新版程式（AGENTS.md 第 4 節）。舊版程式在新結構上可運作（新欄位有預設值或可為空）；新版程式**不**要求在舊結構上運作（遷移先於程式上線）。本機 SQL Server `project` 與 Azure 目前都在 0016，上線前必須各自升級；本機的升級依慣例由使用者備份後執行。

### D5：偏好的更新路徑

- `UserRepository.set_story_preference(user, value)`：條件式 `UPDATE`，值不在三個列舉值內拋 `ValueError`（程式錯誤）；沿用 `_run_update` 以刷新已載入的物件。不要求已有生日（不同於內容模式：偏好沒有年齡風險）。
- 新增小型 `StoryPreferenceService`（`get`／`set`），Controller 不寫驗證。輸入驗證：必須是字串且完全等於三個列舉值之一（不做大小寫容忍，與內容模式一致），否則 `ValidationError(fields={"story_preference": ...})`（400）。
- 端點掛在既有的 `account_api_bp`：`GET`／`PUT /api/account/story-preference`，使用 `require_login()`（受限登入狀態預設被拒，全站掃描測試 `test_restricted_session` 會自動涵蓋）。不過年齡閘門 `require_chat_access`：它檢查的是「能不能呼叫模型」，偏好不涉及模型；玩家未完成年齡確認時本來就被 `require_login` 擋下。
- 成功時記一個事件 `account.story_preference_changed`（只含事件名稱與 `user_id`，不含值；值是低敏感的列舉，但沿用內容模式「事件不含輸入」的作法）。事件名稱要加進 `security_events` 與其守門測試。
- `CurrentUser` 新增 `story_preference`（預設 `all`），`public_dict` 回傳它：`/account` 的初始狀態就有目前值，不需要多一次請求。這會讓 `/api/auth/me` 多一個鍵，既有的鍵集合測試要刻意更新。

### D6：作者工作坊與帳號頁

- `script-draft.js`：`toDraft` 讀 `c.audience`（只接受 `male`／`female`，其餘為 `all`）；`toContent` 永遠送出 `audience`（`all`／`male`／`female`）給後端，由後端的標準化決定要不要保存該鍵。`DEFAULT` 草稿為 `all`。前端字數統計不受影響。`FIELD_LABELS` 加 `audience: '讀者頻道'` 讓錯誤訊息有欄位名稱。
- `workshop.html`：「結局」區塊之後、「全部文字約…」之前新增「讀者頻道」區塊（風格同結局的單選 fieldset，`:disabled="!canEdit"`），一句說明：只影響在劇本清單中的排序，不限制誰能玩。位置依 `docs/RPG.md` 7.10「表單最下方」。
- `account.html`／`account.js`：在「暱稱與劇本創作」區塊之後新增「故事偏好」區塊（`v-if="ageConfirmed"`，與暱稱區塊同條件），沿用暱稱表單的 `Auth.useForm`／`run` 模式（忙碌時禁用、`finally` 恢復、Toast 回報），儲存用 `axios.put`。不使用 Bootstrap JS，Toast 由 Vue 狀態接管。
- 兩處都以純文字渲染標籤（「全讀者」「男頻」「女頻」由前端常數對應，不來自伺服器文字）。

### D7：匯出與內建種子

- `PersonalExportService._account` 與 `ACCOUNT_COLUMNS` 加 `story_preference`；`ScriptRepository.export_scripts` 的版本字典加 `audience`（空值轉 `all`），`SCRIPT_COLUMNS` 與 JSON／CSV 的對應列加 `audience`。
- 內建劇本：資料檔 `xianxia-qingyun.json` **不改**（缺值＝全讀者）。D1 保證重跑種子為「未變更」。

### D8：不新增 LLM 與日誌欄位

偏好與頻道不進任何提示詞、`script_review_prompt._script_data` 與規則層文字欄位（它們逐欄位挑選，不遍歷鍵，所以新鍵不會洩漏進去，仍以測試證明）。事件只用既有欄位。

## Risks / Trade-offs

- [內容形狀有「可有可無」的鍵，未來讀取者可能假設它存在] → 全部經 `normalize_audience(content.get("audience"))`；測試覆蓋缺值、`all`、`male`、`female`；`content_of` 的契約寫進模型與 Repository 註解。
- [`case` 排序在 SQL Server 與 SQLite 的行為差異] → 兩個資料庫都跑 Repository 測試（專案本就對 `project_test` 跑雙軌）；`case` 只比較字串相等並映射成整數，沒有方言差異。
- [新增 `public_dict` 的鍵影響 `/api/auth/me` 的消費者] → 純新增；前端只讀已知欄位；既有的鍵集合測試刻意更新。
- [同一玩家在 RC-3a 之前沒有任何地方感受到偏好的效果] → 預期中的：驗收靠 Repository 測試與 `/account` 的儲存與回讀；RC-3a 的清單才是端到端。
- [`audience` 寫入 `all` 與空值並存] → 兩者行為完全相同，由 `normalize_audience` 統一；匯出與還原時一併轉成 `all`。
- [升級窗口：遷移之後、新程式上線之前，舊程式在新結構上運作] → 新欄位對舊程式不可見，舊程式的 INSERT 取得預設值；不需要特例。

## Migration Plan

1. 備份（`COPY_ONLY`、`CHECKSUM`、`RESTORE VERIFYONLY`；本機 `project` 與 Azure 各一次）。
2. `alembic upgrade head`（0016 → 0017）；`alembic check` 無差異。
3. 部署新版程式；執行 `flask seed-scripts --dry-run` 確認內建劇本為「未變更」。
4. 回復：新程式可直接退回舊版（欄位有預設值或可為空）；需要退回結構時 `alembic downgrade`（只在尚無真實偏好資料時使用，有資料時以備份還原）。
