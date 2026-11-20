# Design

## Context

動機與範圍見 `proposal.md`。現況（已讀程式確認）：

- `ScriptRepository` 已有 `approve_version`（同一 SAVEPOINT 內把舊上架版本改 `superseded`、鎖筆名）與 `reject_version`，但沒有 Service、端點與畫面呼叫；沒有任何程式寫 `published` 以外的上架路徑，除了種子（`publish_builtin`）。
- 版本狀態與兩個輔助欄位（`live_script_id`、`open_script_id`）只在 `script_repository.py` 寫入，`tests/architecture/test_script_state_writes.py` 以 AST 守門；「每個劇本至多一個上架版本、至多一個進行中版本」由部分唯一索引保證。轉移表 `_ALLOWED_SOURCES` 以「目標狀態 → 允許的來源」表達，`approve` 與「新增的恢復」若都以 `published` 為目標就會共用來源，所以要改成明確指定來源。
- `AdminService` 的每個公開方法第一行必須是 `require_admin`（`tests/architecture/test_admin_boundaries.py`），後台專用 Repository 檔名以 `admin_` 開頭、只在 `AdminService` 與 `registry.py` 匯入；高風險操作用 `_confirm`（重新輸入密碼，失敗寫 `reauth.failed`）與 `_record`（稽核，與動作同交易）。
- 目錄是 RC-3a 的最小版：`GameEntryService.catalog` 以 `list_published_cards`（偏好分層、`published_at` 由新到舊、`LIMIT/OFFSET`）取得卡片，標籤另以 `tags_of_versions` 一次取回。
- `startable.StartableVersions` 以 `get_startable`（上架中，或呼叫者自己的任何版本）決定能否開存檔。
- 稽核表 `admin_audit_logs` 的目標欄位只有使用者與對話的對外識別碼，細節是 JSON（不得依內容篩選）。

## Goals / Non-Goals

**Goals:**
- 讓「先審後發」真正可運作：管理員能核可與退回，作者看得到結果。
- 下架與恢復的權限邊界清楚（誰下架誰恢復），且全部由資料庫約束與條件式 UPDATE 保證並行下仍正確。
- 目錄在劇本變多時可用（搜尋、篩選、穩定的游標分頁、詳情），且在 SQLite 與 SQL Server 行為一致。
- 審核備註、劇本文字不進稽核、日誌、匯出與作者的回應。

**Non-Goals:**
- 檢舉、檢舉的優先度、匿名保留偏好（8b）。
- 全文搜尋、相關度排序、搜尋索引（v1 只做 `LIKE` 包含比對）。
- 內建劇本的下架（由種子與部署流程管理）。
- 寄信或站內通知（狀態只顯示在 `/scripts`）。
- 核可時再呼叫模型（送審時已審查，內容在 `pending_review` 已凍結）。

## Decisions

### D1：新增版本狀態 `removed`，轉移表改為「(來源 → 目標)」明確配對

`VERSION_STATUSES` 新增 `removed`（`Unicode(16)` 放得下；不加 CHECK 約束，沿用既有慣例）。轉移集合固定為：`draft→pending_review`、`pending_review→draft|published|rejected`、`published→superseded|removed`、`removed→published|superseded`。`_transition(version, to, *, sources, **extra)` 改成呼叫端明確傳入允許的來源（核可只能來自 `pending_review`、恢復只能來自 `removed`），並對照一張 `(來源, 目標)` 白名單，白名單外的組合在程式層就拋 `InvalidVersionTransitionError`，而不是只靠 `WHERE`。輔助欄位規則不變：`removed` 兩個輔助欄位皆為空，所以下架同時讓出「上架」位置，作者可以立刻建立新草稿。

- **恢復**是 `removed→published` 的條件式 UPDATE，`WHERE status='removed' AND removed_by=<恢復者>`，在 SAVEPOINT 內執行；`live_script_id` 的部分唯一索引衝突（同劇本已有另一個上架版本）轉成 `LiveVersionExistsError`（→ 409 `live_exists`），版本保持 `removed`。
- **核可**的取代範圍擴大為 `published` 與 `removed`（`removed→superseded`）：下架後出了新版本並上架，舊的下架版就此不可再恢復。這避免「兩個可恢復的版本」與恢復時的衝突成為常態。
- 備選：把下架做成獨立旗標（`is_removed`）而不新增狀態。否決：旗標與狀態可能不一致，且 `get_published`、目錄、`live_script_id` 都要多一個條件；狀態值讓「所有查詢只看 `published`」的既有保證自動涵蓋下架。

### D2：新增五個可為空的中繼資料欄位，不是內容

`script_versions` 新增（遷移 `0021`，expand-only）：`submitted_at`（`UTCDateTime`）、`removed_by`（`Unicode(8)`：`author`／`admin`）、`decision_category`（`Unicode(24)`）、`decision_note`（`UnicodeText`）、`decided_at`（`UTCDateTime`），另建索引 `(status, published_at)`（目錄的游標查詢與上架清單）。全部可為空、不回填；`submitted_at` 為空的待審版本以 `created_at` 代替（`coalesce`）。這些欄位像審查結果欄位一樣是**中繼資料**，不進 `content_hash`，也不受「非草稿內容不可變」限制，但只能由 `ScriptRepository` 的轉移方法寫入（`tests/architecture/test_script_state_writes.py` 的 `REVIEW_COLUMNS` 旁新增一組 `DECISION_COLUMNS`，同樣擋「以關鍵字寫入」與「對屬性賦值」）。

- `decision_category`：退回與管理員下架共用，值域 = `REVIEW_CATEGORIES` 加 `other_policy`（新增常數 `CATEGORY_OTHER_POLICY` 與 `DECISION_CATEGORIES`，Repository 與 Service 都驗證，列舉之外拋 `ValueError`／400）。作者自行下架不填類別。8b 若需要更多類別（抄襲、洩漏個資…），在這個列舉上擴充。
- `decision_note`：至多 500 字，NFC、去控制字元（沿用 `input_cleaning`）。**只**在管理員讀取版本時回傳；作者的摘要、匯出、稽核、日誌、事件函式簽名都沒有它的位置（事件函式沒有接收備註的參數，所以不是「記得不要放」而是「放不進來」）。
- 恢復上架時四個決定欄位（`removed_by`、`decision_category`、`decision_note`、`decided_at`）清為空；稽核仍保留「誰在何時下架與恢復」。
- 備選：把決定記在新的 `script_decisions` 表（保留歷史）。否決：8a 不需要歷史查詢，稽核已有時間軸；新表要多一套刪除、匯出與串聯規則，而且備註「隨所屬版本保存與刪除」在同一列最簡單（AGENTS.md 第 11 節）。

### D3：Repository 分工——寫入集中在 `ScriptRepository`，管理員讀取走 `AdminScriptRepository`

- `ScriptRepository`（唯一寫入者）新增／修改：`approve_version`（改指定來源、取代範圍 `published`＋`removed`）、`reject_version(public_id, category, note, now)`、`remove_own(user_id, public_id, now)`、`restore_own(user_id, public_id)`、`remove_by_admin(public_id, category, note, now)`、`restore_by_admin(public_id)`、`_transition` 設定 `submitted_at`（送審設、撤回清）；`latest_closed_version` 納入 `removed`；`get_startable` 排除 `removed`（任何人）；`export_scripts` 加四個欄位（不含備註）。所有方法回傳 `None`（找不到／不是擁有者）或拋型別化的例外（`InvalidVersionTransitionError`、`RemovalLockedError`、`LiveVersionExistsError`），Service 轉成 404／409／403。
- 新增 `AdminScriptRepository`（檔名以 `admin_` 開頭，符合既有架構守門）：`list_by_status(status, limit, offset)`（含佇列排序）、`get_reviewable(public_id)`（`status != draft`）、取得作者的對外識別碼。**只讀**，所以管理員的讀取路徑與玩家的讀取路徑不共用方法，符合「管理員存取他人資料須走專用 Repository 方法」。
- 備選：把管理員的寫入也放進 `AdminScriptRepository`。否決：那會讓狀態寫入散在兩個檔案，破壞「狀態只在 `script_repository.py` 寫入」的守門。

### D4：管理員功能放進既有的 `AdminService`，不另開 Service

新增方法（每個第一行 `require_admin`）：`script_queue`、`read_script`、`approve_script`、`reject_script`、`remove_script`、`restore_script`。`AdminService` 已持有 `_confirm`、`_record`、`persist_on_error`、時鐘與稽核，且 `test_admin_boundaries` 的規則（第一行 `require_admin`）直接涵蓋新方法；另開一個 Service 就要複製 `_confirm` 或抽出共用物件，並擴充守門測試的類別清單。代價是 `AdminService` 變長（約 480 → 650 行）；列表／檢視的組裝放在純函式模組 `admin_script_views.py`（欄位白名單在那裡，單獨測試），讓 `AdminService` 只剩流程。

- 流程：`approve`＝`require_admin` → Repository 核可（例外 → 409 `not_pending`）→ `_record(script.approve)`（同交易）→ 事件。`reject`＝驗證類別與備註 → 轉移 → 稽核（細節只有類別與識別碼）。`remove`／`restore`＝驗證 → `_confirm`（密碼，失敗寫 `reauth.failed`）→ 轉移 → 稽核。`read`＝取得（`status != draft`，否則與不存在相同的 `NotFound`）→ **先寫** `script.read` 稽核再回傳（稽核寫入失敗就不回傳，與 `conversation.read` 同一原則）。
- 稽核欄位：`target_user_public_id` 放作者的對外識別碼（內建劇本為空）；版本與劇本的對外識別碼放 `detail_json`（UUID 不是個資；稽核模型的註解「細節只放列舉值與整數」改為「列舉值、整數與對外識別碼」）。**不新增稽核欄位**，避免多一次遷移與 SQL Server 對同表多個外鍵的限制（稽核表檔頭已記載）。
- 內建劇本（`creator_id` 為空）：下架回 409 `builtin_not_removable`；核可與退回本來就只對 `pending_review`，內建版本不會有這個狀態。
- 作者的下架與恢復放在既有的 `ScriptAuthoringService`（`remove`、`restore`；共用 `_require_user`、版本摘要與事件），端點 `POST /api/scripts/versions/<id>/remove|restore`。

### D5：核可與退回不再呼叫模型，也不重新驗證內容

送審時已完成規則層與模型層審查（`review_hash` 對應內容雜湊）、內容在 `pending_review` 凍結。核可只做狀態轉移；管理員畫面顯示審查結果（僅當 `review_hash == content_hash`，沿用 `script-review` 的「結果與內容雜湊對應」），讓人工審核看得到 `flag` 類別。備選：核可時再跑規則層。否決：結果會與送審時相同（內容不變、詞表版本可能不同但不影響凍結中的版本），增加延遲與失敗模式；人工審核的價值在於判斷模型沒擋下的灰色地帶。

### D6：目錄查詢——游標（鍵集分頁）、搜尋、篩選全部以 ORM 表達式

- **純函式模組 `catalog_query.py`**：解析與驗證 `q`（NFC、去控制字元、`casefold`、至多 5 詞、整體至多 50 字）、`tag`（長度上限 = 標籤上限 12）、`audience`（`all|male|female`）、`limit`、`cursor`，一次回報所有不合法欄位（沿用 `ValidationError(fields=…)`），並負責游標的編碼與解碼。
- **游標內容**：`[偏好層, 發佈時間, 版本編號]`，以 URL-safe base64 編成 JSON，**不簽章**（它只表示「從哪個位置之後」，不含祕密也不授權任何東西）；解碼時嚴格驗證型別與範圍（偏好層 0 至 2、時間為可解析的 ISO、編號為正整數），任何不合就 400 `cursor`，所以被竄改只會得到錯誤或一個合法的起點。
- **鍵集條件**：`(rank > r) OR (rank = r AND published_at < p) OR (rank = r AND published_at = p AND id < i)`，`rank` 是與 `_audience_order` 同一個 `case` 表達式（偏好為全讀者時 `rank` 為常數 0）。排序鍵與條件鍵完全相同，所以並行的上架、下架、恢復不會讓已讀過之後的項目重複或遺漏（`published_at` 在恢復時不變，這是 D1 的前提）。`published_at` 以 `UTCDateTime` 參數傳入，兩種資料庫都保留微秒。
- **搜尋**：每個詞 `or_(lower(title).contains(t), lower(summary).contains(t), lower(world_name).contains(t), exists(標籤 contains t))`，`contains(..., autoescape=True)` 讓 `%`、`_` 變成字面字元；多詞 AND；`tag` 為 `exists(lower(tag) == casefold)`；`audience`：`male|female` 為相等，`all` 為 `is null or == 'all'`。標題等欄位只比對一般欄位，不碰 JSON（AGENTS.md 第 4 節）。SQLite 的 `lower()` 只處理 ASCII、SQL Server 預設不分大小寫，中文與 ASCII 在兩者的結果一致（測試固定）。
- **`has_more` 與 `next_cursor`**：取 `limit + 1` 筆，多的一筆只決定 `has_more`，`next_cursor` 由當頁最後一筆的 `(rank, published_at, id)` 組成；`rank` 由查詢一併回傳（select 出該 `case` 欄位），不在 Python 重算，兩處規則不會分歧。
- **常用標籤**：`group by lower(tag)` 於上架版本，`order by count desc, tag asc limit 30`；它不依使用者，因此不做偏好排序。
- **詳情**：`get_published`（非 `published` 一律 `None` → 404）→ 白名單組裝（純函式，單獨測試）：只有 D6 列出的欄位，角色只取姓名與職業；**隱藏特質、個性、里程碑、結局說明、規則、初始對話不出現**（這些是劇情機關與劇本的內部設定，詳情頁只給「決定要不要玩」的資訊；玩家開存檔時本來就會取得複本，但那是玩的過程，不是預覽）。
- 備選一：沿用 `offset` 加搜尋。否決：AGENTS.md 第 7 節已定游標，且 `offset` 在上架與下架並行時會重複或遺漏。備選二：`published_at` 單鍵游標。否決：偏好分層需要第一個鍵，同時間發佈（種子）需要編號作第三個鍵。
- `offset` 不再被讀取但不報錯：快取中的舊前端第一頁照常可用（只是「載入更多」會重複第一頁；前端與後端同一個映像部署，舊 JS 只在瀏覽器快取期間出現）。`OFFSET_MAX` 與入口目錄的 `_page` 只保留給「我的存檔」。

### D7：前端——詳情頁用獨立頁面，安全模式詢問 Modal 抽成共用

詳情頁是獨立的 MVC 頁面 `/game/script/<版本識別碼>`（可分享連結、返回鍵自然、與 `/game/start/<id>` 同一類路由；段數與 `/game/<存檔>` 不衝突，UUID 也不會等於 `script`）。頁面外殼不使用版本識別碼，資料由前端取得（與精靈頁同一原則）。詢問安全模式的 Modal 目前內嵌在 `game/index.html` 與 `entry.js`，抽成共用樣板片段與 `Game` 命名空間下的小模組（狀態機＋文案，Node 可測），入口頁與詳情頁共用，行為與規格 `rpg-entry-pages` 一致。搜尋框、標籤晶片、頻道選單的狀態放在入口頁的 Vue 響應式狀態；每次條件改變遞增請求序號，只套用序號相符的回應（避免過時回應覆蓋）；不寫任何瀏覽器儲存（沿用 `game-store.js` 以外不使用儲存的規則）。管理員「劇本審核」分頁沿用 `admin.js` 的分頁與 Modal 模式（`tabs` 表與 `{path, key, paged}` 載入表），全文以純文字顯示。

### D8：`/scripts` 的狀態顯示由後端給固定值，文案在前端

版本摘要新增 `decision_category`、`decided_at`、`removed_by`（見 `script-takedown`）；前端 `statusLabels` 加 `removed`，類別文案與入口頁其他類別標籤放同一處（`game-labels.js` 已有安全模式文案；作者端類別說明放 `script-workshop.js` 或共用的 `script-labels.js`，沿用既有 `script_events` 類別列舉的繁體中文說明）。按鈕規則由後端欄位決定：`published` 顯示「下架」；`removed` 且 `removed_by == 'author'` 顯示「恢復上架」；`removed_by == 'admin'` 顯示說明而不顯示按鈕。伺服器仍是唯一的判斷者（前端不顯示的按鈕若被呼叫，伺服器回 403 `removal_locked`）。

### D9：不寄信、不新增設定表項目

審核結果只顯示在 `/scripts`（AGENTS.md 第 7 節）。8a 沒有新的每日上限或門檻，所以**不新增** `runtime_settings` 項目與 flask 指令；8b 的檢舉上限才需要。

## Risks / Trade-offs

- [`LIKE '%…%'` 搜尋不走索引，劇本數很大時變慢] → v1 規模（數百至數千上架版本）可接受；詞數與長度有上限；只掃 `published` 的版本（`ix_script_versions_status_published_at` 縮小範圍）；若量測發現問題另立 change（全文索引屬方言專屬功能，會破壞雙軌）。
- [游標依賴 `(rank, published_at, id)` 三鍵與 `published_at` 不變] → 恢復不改 `published_at`、核可才設定；測試以「分頁期間上架、下架、恢復」固定行為；`published_at` 為空的上架版本不應存在（核可與種子都會設定），查詢以 `coalesce(published_at, created_at)` 也不增加成本，但規格不承諾，因此實作時**不**加 `coalesce`、而以測試證明兩條上架路徑都設定它。
- [核可競態：作者撤回或另一位管理員同時決定] → 全部是條件式 UPDATE（`WHERE status = 'pending_review'`），輸的一方得到 409 `not_pending`；稽核與動作同交易，輸的一方不留稽核。
- [審核備註外洩到稽核、日誌或匯出] → 事件與稽核函式沒有備註參數；`tests/` 以「帶備註的完整流程」收集日誌、稽核與匯出，找不到備註文字；`DECISION_COLUMNS` 守門。
- [`removed` 是新列舉值，漏改的分支會把它當成「非上架」或「已關閉」] → 盤點所有依狀態分支處：`latest_closed_version`、`get_startable`、`list_owned` 呈現、`export_scripts`、刪除版本、`account_erasure`（串聯，不依狀態）、`script_seed_service`（只看上架版本）；各加測試。種子重新上架：`publish_builtin` 取代的範圍不變（內建劇本不可下架，所以不會遇到 `removed`）。
- [管理員下架的版本，作者以新版本重新送審再次上架] → 這是設計：新版本仍須經管理員核可；若作者惡意重複，連續被自動拒絕會暫停發佈，或管理員可再次下架。8b 的檢舉流程提供另一個入口。
- [`AdminService` 變長] → 流程方法保持薄，欄位組裝在純函式模組；若超過可維護範圍，日後以不改外部行為的重構拆出。
- [同一個管理員在核可畫面讀全文會寫大量 `script.read` 稽核] → 每次讀取一筆是規範（AGENTS.md 第 10 節）；稽核保存 24 個月，量級與對話讀取相同。
- [詳情欄位白名單過窄或過寬的取捨] → 以規格固定並以測試鎖定鍵集合；日後要加欄位須先改規格。

## Migration Plan

1. 備份資料庫（部署流程既有步驟）。
2. 執行遷移 `0021_script_publishing`（Alembic `batch_alter_table`、`recreate="never"` 的 `drop_column` 回復；只新增五個可為空欄位與一個索引；不含原生 SQL）。舊版程式在新結構上可運作（忽略新欄位；`removed` 狀態只會由新版程式寫入）。
3. 部署新版程式。
4. 不需要 `seed-scripts`（沒有內建劇本內容變動）。既有 `pending_review` 的版本（若有）`submitted_at` 為空，佇列以 `created_at` 排序。
5. 回復：回到舊版程式前，須確認沒有 `removed` 版本（舊版程式不認識該狀態，會把它當成非上架且非草稿——不會出現在目錄也不能編輯，但舊版的 `latest_closed_version` 看不到它）；若有，先由管理員逐一恢復或讓作者建立新版本。`downgrade` 移除欄位時決定類別與備註一併消失。
6. 同步 `docs/project資料結構文件.md` 與其第 14 節異動紀錄（`script_versions` 新增五欄與索引、`status` 新值）。

## Open Questions

- 管理員審核備註是否納入作者的個人資料匯出：本設計預設**不納入**（那是管理員的文字，不是作者的資料；AGENTS.md 只說「隨所屬版本保存與刪除」）。若使用者希望納入，只需在 `export_scripts` 加一欄，不影響其他設計。
- 管理員下架後，是否讓作者以「申訴」之外的方式知道詳細原因：8a 只顯示固定類別，申訴沿用既有（被鎖定與被暫停發佈者才能進申訴頁）；若要擴充申訴入口屬另一個 change。
- **（已決定，2026-11-20，使用者）下架說明的導向**：申訴頁對沒有被鎖定、也沒有被暫停發佈的作者會導回預設頁面，申訴 API 也拒絕，所以「請至申訴頁」對一般作者是死路。管理員下架的說明（前端的版本列與詳細檢視、後端 `removal_locked` 的預設訊息）一律導向**客服（`/cs`）的意見回饋**：客服會引導留下紀錄，管理員在後台看得到（AGENTS.md 第 7 節）；不承諾處理結果，也不開放申訴入口。限制：意見回饋依賴模型對意圖的分類，作者的說明若被歸為只有 `unknown` 的對話，管理員讀不到（README 後台一節已列的既有限制）。
