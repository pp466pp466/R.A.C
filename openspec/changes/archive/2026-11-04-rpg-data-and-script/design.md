# Design

## Context

- 規範（AGENTS.md 第 5、7、10、11 節，commit `bff4109`）已定下劇本、存檔、內容模式與生命週期的規則；`docs/RPG.md` 第 2 章有舊版的資料表草案（`scripts`、`sessions`、`memory_logs`，缺口 G-1 至 G-14），其中分級與劇本版本的部分已被取代。動機與範圍見 proposal.md。
- 資料庫沒有任何 RPG 資料表。`users` 目前有年齡確認的欄位（`birth_date`、`is_adult_at_confirmation` 等），但沒有內容模式偏好。`AccessPolicy.content_ceiling` 回傳 `ContentCeiling(sexual_content_level, safe_mode)`，只有 `access_policy.py` 與 `registry.py` 使用，沒有 API 或前端消費它（`ceiling` 端點只存在於測試輔助）。
- 設定層（`ContentSettings.level3_enabled`、`SEXUAL_CONTENT_LEVEL3_ENABLED`／`_MODELS`）目前一律禁止 staging 與 production 開啟。
- 守門測試 `tests/architecture/test_user_foreign_keys.py` 與 `test_account_erasure_coverage.py` 會逐表檢查指向 `users` 的外鍵與抹除分類；`unique_when_not_null()` 是專案提供的「只涵蓋非 NULL 列」的部分唯一索引工具。
- 保存期限由 `DataRetentionService` 的一份「類別、期限、找出、計數、處理」清單驅動，由 `purge-expired-data` 執行；個人資料匯出由 `PersonalExportService` 串流產生 JSON 與 CSV zip。

## Goals / Non-Goals

**Goals:**
- 讓劇本、存檔、回合有穩固的資料基礎，使後續的 change 不必再改資料結構就能實作。
- 把「已發佈版本不可變」「每個劇本至多一個上架版本」「已完結的存檔不得再新增回合」這類不變量放進資料庫約束或單一的 Repository 入口，而不是散在各處的 Service。
- 把固定規格與內容模式變成可測試的純邏輯，並讓規範與程式重新一致。
- 失敗時偏向刪除與保守：外鍵串聯刪除、內容模式取最保守、舊的環境變數不會意外開啟任何東西。

**Non-Goals:**
- 不做任何創作、審核、發佈、檢舉的 API 或介面；不做回合引擎、SSE、劇情輸出的審查層；不做暱稱與初次設定頁；不做玩家的內容模式介面（只有 API）。
- 不處理性格原型庫的三張表（RC-5）；不處理代幣錢包與訂閱。
- 不做硬性檢查本身（RC-7）；這裡只提供「宣告分級不得高於作者上限」的驗證參數與開存檔時的分級檢查。

## Decisions

### D1. 資料表與命名：`scripts` 與 `script_versions` 分開

`scripts` 是劇本的身分（對外識別碼、作者、種子鍵、筆名快照），`script_versions` 是每個版本的內容與狀態，`script_version_tags` 是版本的標籤，`sessions` 與 `memory_logs` 沿用 AGENTS.md 與 RPG.md 的名稱。

- **為什麼拆兩張**：規範要求「已發佈版本不可變、修改產生新版本、核可前舊版持續有效」，這需要同一個劇本同時存在多個內容不同的列；把內容放在單一的 `scripts` 列只能靠覆蓋，做不到不可變。
- **代價**：AGENTS.md 與技能鏡像寫的是「`scripts.milestones_json`」，實際位置變成 `script_versions.milestones_json`。本 change 的最後一組任務把這句改成「劇本版本的 `milestones_json`」。
- **備選**：每個版本一列、以 `lineage_id` 分組（保留 `scripts.milestones_json` 的字面）。作者、筆名、種子鍵會在每一列重複，「這個劇本」的查詢要靠分組，所以不採用。
- 欄位對照（細節在任務與模型的註解）：`scripts` 有 `creator_id`（可為空，`ON DELETE CASCADE`）、`seed_key`（`unique_when_not_null`）、`author_name`、`author_code`、`author_locked_at`；`script_versions` 的目錄欄位（標題、簡介、世界觀名稱、宣告的內容模式、狀態、版本編號）是真正的欄位，背景、初始對話、規則、結局、曆法也是純欄位，角色、地點、里程碑是 JSON（不依其內容篩選）。

### D2. 版本狀態機與資料庫約束

狀態只有 `draft`、`pending_review`、`published`、`rejected`、`superseded`，轉移集合固定（見 `script-store` 規格），所有轉移只經過 Repository 的少數方法，內容欄位只有 `draft` 可寫。

- **每個劇本至多一個上架版本與一個進行中的版本，由資料庫保證**：`script_versions` 多兩個輔助欄位 `live_script_id`、`open_script_id`，分別在狀態為 `published`、`draft／pending_review` 時等於 `script_id`，其餘為空；各自以 `unique_when_not_null()` 建立部分唯一索引。這樣不需要新的複合部分索引工具，SQLite 與 SQL Server 的行為一致（SQL Server 的 UNIQUE 約束只容許一個 NULL，所以不能直接對 `(script_id, 狀態)` 設唯一）。
- 輔助欄位只由狀態轉移的方法寫入；測試走遍每一種轉移，斷言「輔助欄位等於 `script_id` 若且唯若狀態屬於該集合」。
- 上架新版本與取代舊版在同一個交易內完成；筆名在劇本的第一次上架時鎖定（`author_locked_at`），鎖定後更新筆名的方法回應衝突。
- 內建劇本由種子建立，版本直接為 `published`（不經 `pending_review`），這是唯一繞過審核的路徑，只有種子服務呼叫。
- **備選**：只存狀態、靠 Service 檢查唯一。並行請求下兩個請求都能通過檢查，所以不採用。

### D3. 存檔複製劇本內容

`sessions.script_snapshot_json` 存開存檔當時該版本標準化後的全部內容（含 `spec_version`），`source_version_id` 是可為空的外鍵（**無動作**，不是 `SET NULL`：SQL Server 不允許刪除使用者時有多重串聯路徑到達 `sessions`，錯誤 1785；所以刪除版本與抹除帳號的流程要先把參照清為空值）。存檔之後不讀取劇本。

- **為什麼複製**：作者可以改版、下架或刪帳號（連同所有版本一起刪），存檔不能因此壞掉或變得無法刪除；也讓「作者刪除帳號時，別人的存檔保留」成立。
- **代價**：每個存檔多約 9 KB（欄位上限加總約 9,300 字，總量上限 8,000 字），JSON 欄位在 SQL Server 是 `NVARCHAR(MAX)`，不被查詢也不建索引。
- 回合引擎之後把劇本內容以資料區塊放進使用者內容區（AGENTS.md 第 10 節），不從複本以外的地方讀。
- `source_version_id` 指向 `script_versions`，不指向 `users`，所以不觸發抹除守門測試的「指向使用者」分類，守門測試另列「無動作的外鍵」一類；「來源被刪除時存檔仍在、參照為空值」要有測試。選無動作的外鍵而不是完全沒有外鍵，是因為有外鍵才不會出現指向已刪除（甚至被重用）編號的懸空參照，忘了清空時刪除會直接失敗而不是悄悄留下資料。

### D4. 回合（`memory_logs`）的唯一性、輪數與冪等

- `(session_id, client_msg_id)` 唯一、`(session_id, seq)` 唯一；`seq` 由 Repository 在同一個交易內以「目前最大值加 1」指派，撞到唯一約束就重試一次。
- `turn_status` 為 `streaming`、`complete`、`interrupted`、`blocked`；`turn_number` 可為空，**只在回合完成時**才指派（目前已完成的回合數加 1），所以中斷的回合不計輪、不佔輪數。`turn_number` 的唯一性不設資料庫約束（可為空的複合唯一在 SQL Server 會出問題），靠「同一存檔同時只處理一則訊息」（AGENTS.md 第 10 節）與測試保證。
- 重送：`complete` 直接回傳；`interrupted`／`blocked` 重設為 `streaming` 並覆蓋該筆；`streaming` 回應衝突。
- 已完結的存檔：`SessionRepository` 開始回合的方法在存檔已完結時拋出 `SessionEnded`（新的 `Conflict` 子類別，代碼 `session_ended`），這個檢查與開始回合在同一個條件式更新裡，避免並行下漏網。
- 世界狀態以 `sessions.version` 做條件式更新（成功加 1，不一致拋出既有的 `VersionConflict`）；記事簿的欄位（`notebook_text`、`notebook_version`、`notebook_upto_turn`）這個 change 只建立、給預設值，行為屬 RC-2。

### D5. 固定規格驗證器

單一純函式模組（`app/services/script_spec.py`），常數集中在模組開頭的一張表（欄位上限、數量上限、總量上限 8,000 字、`SPEC_VERSION = 1`）。

- 輸入原始內容、`max_tier`（作者上限）、`forbidden_markers`（呼叫端提供）；輸出 `ValidatedScript`（標準化內容、`content_hash`、`total_chars`）或拋出 `ValidationError`，其 `fields` 以路徑為鍵（`characters[2].age`）。
- 標準化：NFC、去控制字元；單行欄位收斂空白，多行欄位保留換行。`content_hash` 為標準化後內容的規範化 JSON（排序鍵、`ensure_ascii=False`、UTF-8）的 SHA-256。
- 里程碑 `id` 在未提供時依順序產生（`m1`、`m2`…），同一輸入結果固定；沿用既有版本的 `id` 由創作流程（RC-7）負責。
- 錯誤訊息只描述規則，不回顯輸入。圖片欄位只接受空值。
- **備選**：JSON Schema 函式庫。需要新增依賴，且「標準化後計長度」「跨欄位的規則（曆法、結局、總量）」仍要自己寫，所以不採用。

### D6. 內容模式（取代等級 0 至 3）

- `app/services/content_tier.py`（純邏輯）：三種模式與順序、`most_conservative()`、`parse_tier()`（無法判定取 `safe`）、`ADULT_RULES_VERSION`（安全規範的版本字串，改版時所有人須重新確認）。
- `users` 新增四欄：`content_tier_preference`（`NOT NULL`，`server_default='safe'`，使舊版程式的 INSERT 仍有效且保守）、`adult_confirmed_at`、`adult_rules_version`、`adult_confirm_method`（皆可為空）。
- `AccessPolicy` 的 `content_ceiling` 改為 `content_access(user)`，回傳 `ContentAccess(preference, age_cap, effective, adult_selectable, adult_confirmed, reason)`；另有 `effective_tier(user, script_tier=…, save_tier=…)`。建構參數 `adult_ceiling` 改為 `adult_mode_enabled: bool = False`。年齡上限：未成年或尚未確認 → `safe`；成年 → `standard`，成人模式「啟用中」（部署開關開、已在目前規範版本確認）時為 `adult`。生效模式 = `most_conservative(偏好, 年齡上限, 劇本宣告, 存檔)`。
- `CurrentUser` 新增 `content_tier_preference`、`adult_confirmed_at`、`adult_rules_version`（有預設值，既有測試輔助不用改）；**不進** `public_dict`，也不進 `/api/auth/me`。
- 預設與回填：年齡確認成功時（`complete_age_verification_if_unset` 與補填路徑）依確認當下的年齡寫入偏好；遷移對既有列依 `is_adult_at_confirmation`（真 → `standard`，其餘 `safe`）回填，所以滿 18 歲不會自動調高。
- **成年確認的記錄放在 `users`，不另開表**：只有「目前這一次確認」有意義；規範版本改變時覆寫即可，歷史不需要保存。
- **備選**：每次請求都由生日算出預設（不存偏好）。這會讓「未成年時註冊、之後滿 18 歲」的人自動變成 `standard`，違反「不自動調高」，所以不採用。

### D7. 內容模式 API 與事件

`ContentModeService` 提供 `get_state` 與 `set_mode`，掛在既有的 `account_api_bp`：`GET`／`PUT /api/account/content-mode`，沿用 CSRF、登入與受限登入狀態的規則；先經 `AccessPolicy` 的年齡閘門（未確認年齡 → 403 `age_confirmation_required`）。錯誤代碼：`content_mode_restricted`（未成年要 `standard` 或 `adult`）、`adult_mode_unavailable`（部署關閉）、400 逐欄位（`confirm_adult`、`accept_rules`）。事件以名稱表達新的模式（`account.content_mode.safe`／`standard`／`adult`），`log_event` 不需要擴充欄位，日誌自然不含任何輸入內容。

### D8. 設定：`ADULT_MODE_*` 與已淘汰的舊名稱

- `ContentSettings` 改為 `adult_mode_enabled`、`adult_models`；`_resolve_content` 移除「staging／production 一律禁止」，保留「RPG 已設定、清單非空且涵蓋主要與備援模型」與格式檢查；值不合法啟動失敗，訊息只含變數名稱。
- 舊的 `SEXUAL_CONTENT_LEVEL3_*` 沿用 `EMAIL_VERIFICATION_REQUIRED` 的處理方式：忽略、啟動時只警告一次（只含變數名稱），`scripts/launch_plan.py` 的 `OBSOLETE_VARS` 加入它們。**忽略舊變數 = 成人模式關閉**，這是失敗時偏向保守的方向。
- `.env.example`、README 的環境變數表、營運文件的變數表同步（既有測試會比對）。

### D9. 生命週期：抹除、匯出、保存期限

- **抹除**：`scripts.creator_id`、`sessions.user_id` 都是 `ON DELETE CASCADE`；`script_versions.script_id`、`script_version_tags.version_id`、`memory_logs.session_id` 也串聯；`sessions.source_version_id` 是無動作的外鍵，抹除與刪除版本的流程先清空它（見 D3）。「匿名保留已發佈劇本」的偏好與抹除分支屬 RC-8；這裡的串聯讓「沒做到」時偏向刪除。
- **守門測試擴充**：`test_account_erasure_coverage.py` 的分類從「只認 `users` 與 `chat_conversations`」改為記錄任意父表的串聯與無動作的外鍵，新增 `CASCADED_FROM_SESSIONS`、`CASCADED_FROM_SCRIPTS`、`CASCADED_FROM_VERSIONS`、`NO_ACTION_REFERENCES`；並新增行為測試（抹除作者後別人的存檔仍在）。
- **匯出**：JSON 在 `account` 與 `conversations` 之外新增 `scripts` 與 `sessions` 兩個陣列，以產生器逐筆串流；CSV zip 新增 `scripts.csv`、`sessions.csv`、`turns.csv`，沿用公式字元中和與 UTF-8-sig。存檔內的劇本複本不匯出，世界狀態只在 JSON。
- **保存期限**：`DataRetentionService` 的清單新增兩個類別 `sessions`（最後活動 365 天）與 `turn_content`（中斷或被擋的回合超過 14 天，把 `llm_raw_response` 清為空字串）；`RetentionRepository` 新增對應的找出、計數與處理方法；沿用預覽與分批。

### D10. 種子

- 資料檔放在 `app/data/scripts/*.json`（UTF-8，讀取一律 `utf-8-sig`，隨映像封裝），每份有 `seed_key` 與規格內容。`flask seed-scripts [--dry-run]` 全部驗證通過後才寫入（全有或全無），以 `seed_key` 與 `content_hash` 判斷「建立」「更新（新增版本）」「沒有變動」。
- 指令加入 `MANUAL_COMMANDS`（部署後人工或部署流程執行，不排程，不在啟動時執行）。輸出只含筆數與種子鍵。
- **為什麼不放進 Alembic 遷移**：劇本內容會隨版本演進，而遷移是一次性的結構與資料變更；內容更新應該能重複執行並產生新版本。
- 首發劇本（古風仙俠）是一份精簡但完整的內容（至少三位角色、三個地點、年齡與年份兩種里程碑、曆法「乾歷朝」），宣告 `safe`，使用者日後可以直接改資料檔，重新執行就會新增一個版本。

### D11. Repository 的簽名與讀取範圍

- 劇本：擁有者範圍的方法（`get_owned_*`、`list_owned`、建立與修改草稿、轉移）都接收 `user_id`；公開讀取（`get_published`、`list_published(max_tier, …)`）只回傳 `published` 的版本；沒有「只憑識別碼取得任意版本」的方法。
- 存檔與回合：全部方法接收 `user_id`。開存檔的 Service 先過 `AccessPolicy` 的年齡閘門，再檢查「版本屬於可用範圍」（`published`，或自己的版本）與「劇本宣告 ≤ 生效模式」。
- 管理員讀取待審內容走 RC-8 的專用路徑，不在這裡。

### D12. 遷移 `0012_rpg_data_and_script`

只新增：`users` 四欄（`content_tier_preference` 有伺服器預設值）、五張新表與索引；一次小型回填（`content_tier_preference` 依 `is_adult_at_confirmation`，用 `sa.table`／`sa.update`，不匯入應用程式程式碼，也沒有方言專屬的原生 SQL）。舊版程式在新結構上可運作（新欄位有預設值、新表沒人用），所以部署順序仍是「備份 → migration → 部署 → `seed-scripts`」。降級會移除新表與新欄位（`recreate="never"` 的理由同 `0010`），只在還沒有任何真實資料時使用。部分唯一索引的 `sqlite_where` 與 `mssql_where` 與模型一致。

## Risks / Trade-offs

- **範圍大**：資料、驗證器、內容模式、生命週期、種子都在同一個 change。以任務分組、每組可獨立驗證來控制；各組之間的相依只有「模型與遷移 → 其他」。
- **`live_script_id`／`open_script_id` 重複存了 `script_id`**：若有人在轉移方法之外直接改狀態，輔助欄位會漂移。以「只有轉移方法寫狀態」的單一入口與走遍所有轉移的不變量測試降低風險。
- **staging 與 production 現在可以明確開啟成人模式**：這是規範的決定，不是疏漏。預設仍然關閉、需要列出許可涵蓋的模型，開啟前的上線前關卡（書面許可、法務意見）保持在營運文件。沒有任何入口會送出露骨內容，因為回合引擎還不存在。
- **回填依賴 `is_adult_at_confirmation`**：勾選版時代的舊帳號此欄為空，會得到 `safe`，是保守的方向；影響的只有測試帳號。
- **舊環境變數改名**：忽略舊名稱代表成人模式關閉，不會因為改名而意外開啟；代價是想用的人要改名，警告會提醒。
- **`memory_logs` 與 `sessions` 的名稱**：沿用 AGENTS.md 與 RPG.md，`memory_logs` 在摘要被取消後其實是回合表；不改名是為了與規範文字一致。
- **匯出變大**：存檔含記事簿與全部回合，用產生器串流，記憶體不隨存檔數成長；JSON 與 CSV 的測試要涵蓋大量回合。
- **種子內容的品質**：首發劇本由我撰寫一份精簡版，只保證通過規格；故事品質請之後以資料檔改寫。

## Migration Plan

1. 備份資料庫。
2. 執行 `alembic upgrade head`（`0012`）。新舊版程式都能在新結構上運作。
3. 部署新版程式。
4. 執行 `flask seed-scripts`（先 `--dry-run` 看筆數）。
5. 回復：只在沒有真實資料時 `alembic downgrade` 一版；有資料時以備份還原。環境變數 `ADULT_MODE_*` 預設關閉，不需要為回復做任何事。

## Open Questions

無。需要使用者之後處理的項目（供應商書面許可須涵蓋新模型、法務意見與勾選確認是否足夠、安全規範頁面的內容）都不影響這個 change 的實作，已記錄在 report.md 的 N-12、N-13 與路線圖。
