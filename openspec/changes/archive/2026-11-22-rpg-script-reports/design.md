# Design

## Context

動機與範圍見 `proposal.md`，行為見 `specs/`。現況（已讀程式確認）：

- 版本狀態只在 `ScriptRepository._transition`（條件式 UPDATE）寫入；`approve_version` 在 SAVEPOINT 內把同劇本的 `published`、`removed` 版本改為 `superseded`；`_remove` 把 `published` 改為 `removed`。這兩處正是檢舉要自動結案的時間點。
- **「沒有作者＝內建劇本」散落三處**：`AdminScriptRepository.find_ref` 的 `is_builtin = creator_id is None`（被 `AdminService.remove_script` 用）、`ScriptRepository.remove_by_admin` 的 `creator_id is None`、前端 `admin-format.js` 的 `remove: it.status === 'published' && !!it.author_id`。另外 `GameEntryService._card` 已經依種子鍵（`script.seed_key is not None`）顯示「官方」，是對的。匿名保留會讓玩家劇本也沒有作者，所以這三處必須改。
- 抹除流程 `AccountErasureService.erase` 在同一個交易內：①`clear_script_version_refs`（清別人存檔對這位作者版本的來源參照，因為 `sessions.source_version_id` 是無動作的外鍵，SQL Server 不允許多重串聯路徑，錯誤 1785）→ ②清帳本對話參照 → ③條件式刪除使用者（`scripts.creator_id` 以 `ON DELETE CASCADE` 帶走劇本、版本、標籤）→ ④刪除紀錄 → 提交；③沒刪到就整個回復。呼叫者為 `erase_inactive`、`erase_underage`、`erase_unconfirmed`、`erase_pending_due`，以及 `deletion_replay_service`。
- 營運限制放在 `runtime_limits`（鍵、範圍與保底預設在 `runtime_limits.py` 的 `LIMIT_SPECS`；遷移以 `bulk_insert` 寫入初始值，重複執行不蓋掉）；台灣日界有 `quota_policy.taipei_day_bounds`，送審的每日上限就用它。
- 管理員讀取他人資料的規則：後台專用 Repository 只有 `AdminService` 與 `registry` 能匯入、每個公開方法第一行 `require_admin`、讀全文先寫稽核；稽核明細只能放列舉值、整數與對外識別碼（`AdminAuditRepository` 有欄位白名單，違規拋 `ValueError`）。
- `tests/architecture/test_account_erasure_coverage.py` 要求每個指向 `users.id` 的外鍵與每個串聯父表都登記。

## Goals / Non-Goals

**Goals:**
- 檢舉從玩家送出到管理員結案走通，且「結案」在所有版本離開上架的路徑上都自動發生，不留下孤兒的未結案檢舉。
- 匿名保留在抹除的同一個交易內完成，並維持「失敗偏向刪除」。
- 修正「沒有作者」的語意，讓保留的玩家劇本仍可被檢舉與下架。
- 檢舉與抹除在 SQLite 與 SQL Server 行為一致（含 SQL Server 的串聯路徑限制）。

**Non-Goals:**
- 檢舉的相關度排序、檢舉人信譽分數、檢舉濫用的自動懲罰（只有每日上限與每版本一次）。
- 顯示檢舉人身分給管理員、通知檢舉人（AGENTS.md 已定不通知）。
- 被保留劇本的「認領」或轉移擁有者。
- 模型層審查檢舉內容（檢舉文字不送入模型）。

## Decisions

### D1：新資料表 `script_reports`，版本外鍵是無動作、檢舉人外鍵是串聯

欄位：`id`、`version_id`（`script_versions.id`，**無 `ondelete`**）、`reporter_id`（`users.id`，`ON DELETE CASCADE`）、`reasons_json`（JSON，固定列舉的陣列）、`red_line`（布林，建立時由 Service 依理由算出，之後不變）、`note`（`UnicodeText`，可為空，自寫文字）、`created_at`／`updated_at`、`closed_at`（`UTCDateTime`，可為空）、`closed_result`（`Unicode(12)`，可為空：`removed`／`dismissed`／`superseded`）。約束與索引：`UNIQUE (reporter_id, version_id)`、`(version_id, closed_at)`（佇列與自動結案）、`(reporter_id, created_at)`（每日上限）、`(closed_at)`（期限清除）。沒有 `public_id`（檢舉不被個別定址：管理員結案是對版本整批做）。

- **為何版本外鍵不串聯**：使用者刪除有兩條路徑會到達這張表——`users → script_reports`（檢舉人）與 `users → scripts → script_versions → script_reports`（被檢舉版本的作者）。SQL Server 不允許多重串聯路徑（錯誤 1785）。與 `sessions.source_version_id` 同一個做法：檢舉人那一條用 `CASCADE`（玩家文字視為個資，漏處理時偏向刪除），版本那一條是無動作外鍵，由抹除流程在刪除版本之前明確刪除；漏掉時刪除會直接失敗，不會留下懸空的編號。`tests/architecture/test_account_erasure_coverage.py` 登記 `script_reports.reporter_id` 為 `CASCADED_FROM_USERS`、`script_reports.version_id` 為 `NO_ACTION_REFERENCES`，並新增「抹除流程在刪除使用者之前先清除檢舉」的原始碼順序斷言。
- **為何 `red_line` 另存**：優先度要依「是否有人勾選紅線理由」排序，而 AGENTS.md 第 4 節不允許依 JSON 欄位內容篩選；`reasons_json` 只用來顯示，排序與聚合只用 `red_line`。它在建立時由理由算出且不可變，所以不會與 `reasons_json` 分歧（測試固定）。
- 備選：把理由拆成子表 `script_report_reasons`。否決：多一張要登記抹除與匯出的表，只為了一個可由建立時算出的布林。備選：`UNIQUE (reporter_id, script_id)`。否決：使用者已決定新版本可再檢舉（唯一性按版本）。

### D2：自動結案在 `ScriptRepository` 的轉移 SAVEPOINT 內呼叫 `ScriptReportRepository`

- `_remove`（作者與管理員下架共用）在轉移成功之後、同一個交易內呼叫 `ScriptReportRepository.close_open(version_id, "removed", now)`；`approve_version` 在把舊版本改為 `superseded` 的同一個 `begin_nested()` 內，對被取代的 `published` 版本呼叫 `close_open(..., "superseded", now)`（`removed` 的舊版本在它們被下架時已結案）。轉移丟出 `InvalidVersionTransitionError` 時 SAVEPOINT 回復，結案一併回復。
- 結案欄位（`closed_at`、`closed_result`）只能由 `ScriptReportRepository` 的方法寫入：新增 `tests/architecture/test_script_report_writes.py`，仿 `test_script_state_writes.py` 以 AST 擋「以關鍵字寫入」與「對屬性賦值」。`ScriptRepository` 只呼叫該 Repository 的方法，不自己寫結案欄位，所以「版本狀態只在 `script_repository.py` 寫入」與「結案只在 `script_report_repository.py` 寫入」兩條守門互不衝突。
- 為什麼不放在 Service：版本離開上架有四個入口（`remove_own`、`remove_by_admin`、`approve_version`，以及種子的 `publish_builtin`——內建劇本不可被檢舉所以不會有檢舉，但邏輯仍涵蓋）。放在 Service 每個入口都要記得呼叫，漏一個就留下孤兒的未結案檢舉；放在轉移內部則不可能漏。`AdminScriptRepository` 已有 Repository 組合 Repository 的先例（它建立 `ScriptRepository` 取內容）。
- **恢復上架不重開**：`_restore` 不碰檢舉。結案是歷史事實（下架時就處理過了），重開會讓管理員的「不成立」與下架決定被重新審視。恢復之後同一位玩家因 `UNIQUE (reporter_id, version_id)` 不能再檢舉同一版本，其他玩家可以。
- 備選：以資料庫觸發器。否決：方言專屬、會破壞雙軌。備選：未結案的檢舉隨狀態查詢動態推導（`closed_at` 為空且版本 `published` 才算未結案）。否決：版本被取代又恢復的狀態沒有「恢復」路徑會讓舊檢舉復活，語意不清；明確結案才有可保存 12 個月的結案時間。

### D3：檢舉的建立、驗證與每日上限放在 `ScriptReportService`

- 檢查順序（對應規格）：登入與受限狀態（既有 `require_login`）→ `AccessPolicy.require_can_report`（帳號可用且已完成年齡確認，沿用 `chat_gate`，不論角色與會員等級）→ 以 `get_published_card` 取版本（非上架、不存在、格式不對、種子鍵非空 → `NotFound`）→ 自己的劇本（`script.creator_id == user.id` → `Forbidden(code="report_own_script")`）→ 驗證本文（`reasons`、`text`，一次回報全部不合法欄位）→ 重複（先查再以 SAVEPOINT 內的 `INSERT` 為準，`IntegrityError` → `Conflict(code="already_reported")`）→ 每日上限（`count_between(reporter, start, end)` ≥ 現讀的 `script_report_daily` → `TooManyRequests(code="report_limit", retry_after)`）→ 寫入 → 記錄事件。404 與 403 早於本文驗證，使檢舉頁面不會因為無效識別碼而洩漏欄位規則；重複早於上限，所以重複的請求不消耗額度。
- **每日上限是軟上限**：兩個並行請求都在計數時看到 4 筆就都會寫入，當日可能是 6 筆。不加鎖：檢舉是低頻操作，鎖會把玩家的請求串起來；每版本一次與每日 5 次兩層仍使洗版面積有限。以資料列計數，作者刪除劇本會使檢舉消失而讓上限略鬆（可接受，記在風險）。
- 錯誤類別沿用 `services/errors.py` 的模式新增 `ReportOwnScript`（403，`report_own_script`）、`AlreadyReported`（409，`already_reported`）、`ReportLimit`（429，`report_limit`，`retry_after`），並登記到 `tests/test_service_errors.py` 的狀態表（8a 的教訓：新的 `ServiceError` 子類不登記，全域守門測試會失敗）。
- 文字清理沿用 `input_cleaning`（NFC、去控制字元、單行或多行規則依既有函式）；`reasons` 以 `REPORT_REASONS` 常數驗證（`models/script_report.py`），紅線集合 `RED_LINE_REASONS = {minor_sexual, sexual_violence, real_person_sexual}` 在同一處，Service 算 `red_line`，管理員聚合只讀 `red_line`。

### D4：管理員清單的聚合以一個子查詢取得，三個鍵出現在所有清單

- `AdminScriptRepository` 新增 `open_report_summary` 子查詢：`select(version_id, count(id) as n, max(case(red_line, 1, 0)) as red, min(created_at) as earliest).where(closed_at.is_(None)).group_by(version_id)`，對所有狀態的清單做外連接，`report_count = coalesce(n, 0)`、`red_line_reported = coalesce(red, 0) = 1`。`status=reported` 加條件 `status = published and n > 0`，排序為 `red desc, n desc, earliest asc, version.id asc`（全部為 ORM 表達式；布林一律以 `case` 取 0／1，SQL Server 不接受把比較式直接當值回傳，與現有 `has_note` 同一個做法）。
- `LIST_KEYS` 新增 `report_count`、`red_line_reported`、`is_builtin`；`tests/test_admin_script_views.py` 的鍵集合測試同步。`is_builtin` 由 `seed_key is not None` 算出（`ScriptVersionRow` 新增一個欄位，位置參數順序同步）；`ScriptVersionRef.is_builtin` 同改。
- 讀取檢舉：`AdminScriptRepository.list_reports(version_public_id)`（唯讀、只回資料類別，**沒有**檢舉人欄位——查詢根本不選取 `reporter_id`，所以不是「記得不要放」而是「拿不到」）；`AdminService.script_reports(admin, public_id)` 先 `get_reviewable`（非草稿，否則 `NotFound`）再**先寫 `report.read` 稽核**再回傳。
- 結案為不成立：`AdminService.dismiss_script_reports(admin, public_id)`：`require_admin` → `find_ref`（不存在或草稿 → 404）→ 呼叫 `ScriptReportRepository.close_open(..., "dismissed")`（回傳結案筆數；0 筆 → `Conflict(code="no_open_reports")`）→ `_record(ACTION_REPORT_DISMISS, ...)`，與結案同一個交易；不要求密碼（AGENTS.md 第 10 節：高風險清單不含檢舉結案，與核可、退回同級）。
- 結案為「下架」不新增端點（見規格）：`remove_script` 本來就經 `_remove` 結案。刻意不提供「一鍵下架並結案」的免密碼捷徑。

### D5：把「沒有作者」改回「沒有種子鍵」

三處改為以種子鍵判斷：①`AdminScriptRepository.find_ref` 與 `ScriptVersionRow`／`ScriptVersionRef` 新增 `is_builtin`（`Script.seed_key.is_not(None)`）；②`ScriptRepository.remove_by_admin` 的守門改為 `script.seed_key is not None`；③`admin-format.js` 的 `scriptActions`：`remove` 條件改為 `it.status === 'published' && !it.is_builtin`。`AdminService.remove_script` 的 `ref.is_builtin` 不變（來源改了）。稽核目標使用者：`ref.author_public_id` 為空時 `target_user_public_id=None`（`_record` 本來就支援，內建劇本現在就是這樣）。另新增回歸測試：「匿名保留的上架版本可被管理員下架」與「內建劇本仍 409」。**盤點**：實作時以 `grep creator_id`、`author_public_id`、`author_id` 全部再過一遍，後台與玩家端凡是以「作者為空」推論「內建」的分支都要改（tasks 1.5）。

### D6：匿名保留放在 `ScriptRepository.retain_published_anonymously`，由 `AccountErasureService` 依偏好呼叫

- `AccountErasureService.erase(..., retain_scripts: bool = False)`：只有 `erase_inactive` 與 `erase_pending_due` 傳入 `True`；`erase_underage`、`erase_unconfirmed` 與 `deletion_replay_service` 維持 `False`（預設值使「新增呼叫者時漏傳」偏向全刪）。為 `True` 時在 `try` 區塊內、`clear_script_version_refs` 之前：`if self._users.keep_published_scripts_of(user_id): self._scripts.retain_published_anonymously(user_id)`，**偏好在這個交易內讀取**（規格：刪除當下讀取）。條件式 `delete(user_id)` 沒刪到時既有的 `rollback()` 把這些一併復原（測試固定）。
- `retain_published_anonymously(user_id) -> RetainResult(kept, deleted)` 的步驟（同一個交易，全部 ORM 表達式，無原生 SQL）：
  1. 取作者的劇本 `S`；保留集合 `K` = `S` 之中 `status = published` 的版本（`live_script_id` 非空）；刪除集合 `D` = 作者所有版本 − `K`（含沒有上架版本的整份劇本的全部版本）。
  2. 對 `D` 的版本：`UPDATE sessions SET source_version_id = NULL`（無動作外鍵）；`DELETE FROM script_reports WHERE version_id IN D`（無動作外鍵）。
  3. 刪除 `S` 中沒有 `K` 版本的劇本（`DELETE scripts`，版本與標籤靠串聯）；對有 `K` 的劇本，刪除其 `D` 版本（`DELETE script_versions`，標籤靠串聯）。
  4. 保留的劇本：`creator_id = NULL`、`author_name = RETIRED_AUTHOR_NAME`（「已註銷的作者」，新增於 `models/script.py`）、`author_code = NULL`；`K` 版本的 `decision_note` 清為空（上架版本本來就沒有，防禦用）。`author_locked_at` 不變。
  5. 之後既有的 `clear_script_version_refs(user_id)` 找不到任何以此人為作者的劇本（已解除關聯），不會誤清對 `K` 的來源參照；對 `K` 的存檔來源參照保留。
- **為什麼順序是「清參照、刪檢舉 → 刪版本 → 解除關聯 → 刪使用者」**：任何一步反過來都會被無動作外鍵擋下（`sessions.source_version_id`、`script_reports.version_id`），而不是留下懸空資料；刪除使用者前作者關聯已是 NULL，所以 `ON DELETE CASCADE` 不會誤刪保留的劇本。
- **預設偏向刪除**：`scripts.creator_id` 仍是 `ON DELETE CASCADE`（AGENTS.md 第 11 節：作者關聯採串聯刪除，使漏掉處理時偏向刪除）；只有這個函式明確把它改成 NULL 的劇本才會留下。
- 備份還原後重新抹除（`deletion_replay_service`）：不傳 `retain_scripts`，保留的劇本因 `creator_id` 已是 NULL 而不受影響，只有「還原後又掛回該帳號的劇本」會被刪（AGENTS.md 已載明的限制）。
- 備選：以「軟刪除／標記」保留劇本再由排程轉移。否決：多一個中間狀態與排程，違反「刪除就是刪除」的簡單性，且備份還原時更難推論。

### D7：偏好欄位與端點

`users.keep_published_scripts`（`Boolean`，NOT NULL，`server_default` 假值；遷移用 `batch_alter_table`，與 `story_preference` 同一個寫法，降級時 `mssql_drop_default=True`）。`UserRepository.keep_published_scripts_of(user_id)` 與條件式 `set_keep_published_scripts(user, keep)`；新增 `ScriptRetentionPreferenceService`（`get`、`set`，與 `StoryPreferenceService` 同型：只要求已登入、受限狀態拒絕、管理員 `Forbidden`）。`CurrentUser` 快照**不**新增欄位（避免牽動 `/api/auth/me` 與一堆回應形狀測試）；帳號頁以 `GET /api/account/script-retention` 取得 `keep` 與 `live_count`（`ScriptRepository.count_live_owned(user_id)`，只算 `live_script_id` 非空的版本）。`AccountDeletionService.request(..., keep_published_scripts=None)`：確認本人通過之後、在 `request_deletion` 的條件式 UPDATE 之前或同交易寫入偏好（非布林 → `ValidationError(fields={"keep_published_scripts": …})`）；申請失敗（並行搶先、狀態不符）時偏好 SHALL 不變，所以寫入放在 UPDATE 成功之後的同一個交易、失敗則整個回復。

### D8：期限清除、匯出與政策頁

- `DataRetentionService` 新增類別 `script_reports`（`lifecycle_events.CATEGORY_SCRIPT_REPORTS`、`RETENTION_LABELS`、`RetentionRepository.find_/count_/purge_script_reports`，條件 `closed_at IS NOT NULL AND closed_at < cutoff`），保存 365 天；未結案者不清（條件已排除）。排在 `script_submissions` 之後。`/privacy` 的保存期限表加一列，`tests/test_legal_pages.py` 比對。
- 匯出：`personal_export_service` 新增 `script_reports`（JSON 陣列、CSV `script_reports.csv`）；來源是 `ScriptReportRepository.export_for_reporter(user_id)`，只回 `version_public_id`、`reasons`、`note`、`created_at`，**不選取** `closed_*`。帳號資料欄位表新增 `keep_published_scripts`（`ACCOUNT_COLUMNS`）。
- 政策頁：`privacy.html`、`terms.html` 依規格補寫並更正兩頁過時的「公開分享尚未開放」與「現階段沒有公開分享」；`CURRENT_TERMS_VERSION` 提高（新使用者註冊時記錄的版本）；既有使用者的同意版本不被重置（沿用現行行為，不要求重新同意）。

### D9：前端

- 詳情頁：`script-report.js`（IIFE 全域模組 `Game.scriptReport`，純函式：理由清單與中文標籤、`validate(reasons, text)`、`requestBody`、`stateAfter(error)`；Node 可測）、`script_detail.html` 加按鈕與 `ui-modal`（沿用 `_mode_prompt.html` 的 Modal 寫法與 Esc／遮罩行為）、`script-detail.js` 掛載狀態（`reportState`、`reportOpen`、`reportForm`、`submitting`，`finally` 恢復）；`game-api.js` 新增 `reportScript(versionId, body)`；`normalizeDetail` 保留 `report_state`（只接受三個列舉，其他當 `unavailable`）。字數計數以 `Array.from(text).length` 計 Unicode 字元，與伺服器一致。
- 帳號頁：`account.html` 在暱稱區塊加開關（`account.js` 狀態與請求）、刪除 Modal 加條件式核取方塊（`account-lifecycle.js` 的 `deletionBody` 新增選填 `keep_published_scripts`，只在 `live_count > 0` 時帶上）。
- 後台：`admin.js` 的 `LISTS` 新增 `scripts_reported`、分頁檢視新增「被檢舉」；`openScript` 載入全文之後，若版本為上架中，再 `GET …/reports`（失敗只在面板顯示錯誤，不阻止全文）；面板列出各筆；`scriptActions` 新增 `dismiss`（有未結案檢舉時）；`admin-format.js` 新增理由中文標籤與面板格式化（純文字）；標籤表 `LABELS.action` 補 `report.read`、`report.dismiss`（既有測試要求稽核動作在前端有標籤）。
- 全部沿用 AGENTS.md 第 1、2 節：離線資源、`url_for`、Vue 全域變數、無 `v-html`、無 `data-bs-*`、Modal 與 Toast 由 Vue 接管、無純白文字。

### D10：事件與稽核

`script_events` 新增 `EVENT_REPORT_SUBMITTED`（欄位：`user_id`、`reason_count`、`text_length`）、`EVENT_REPORT_LIMITED`（`user_id`）、`EVENT_REPORTS_DISMISSED`（`user_id`＝管理員、`count`）；系統自動結案不另記事件（它是下架或核可的副作用，筆數已在那些事件的脈絡內；規格要求「自動結案」被記錄的部分以 `EVENT_REPORTS_AUTO_CLOSED`（`count`、`result`）一個事件滿足，只在 `count > 0` 時記）。`lifecycle_events` 新增 `log_scripts_retained(user_id, kept, deleted)`。函式簽名只接受整數與列舉，傳入其他型別立即出錯（沿用既有風格）。稽核：`ACTION_REPORT_READ = "report.read"`、`ACTION_REPORT_DISMISS = "report.dismiss"`，加入 `AUDIT_ACTIONS` 與前端標籤表；明細鍵白名單允許 `count`（整數）。

## Risks / Trade-offs

- [`creator_id IS NULL` 的語意改變，漏改一處就讓保留的劇本無法下架或被顯示成內建] → 三處已知，tasks 1.5 要求全專案盤點並以測試固定「匿名保留的版本可被下架、可被檢舉、顯示『已註銷的作者』」與「內建劇本仍不可下架、不可檢舉」；變異驗證破壞其中一處必須有測試抓到。
- [抹除交易變長，單一帳號有大量劇本時延遲增加] → 每位作者的劇本數有上限（依會員等級，預設最多 30），步驟都是以集合為單位的單一語句；仍在一個交易內以保證原子性。
- [抹除順序寫錯會讓刪除失敗而非洩漏] → 這是設計：無動作外鍵讓錯誤顯性化；`tests/architecture` 新增原始碼順序斷言，加上「SQL Server `project_test` 上真的抹除含檢舉的作者」的整合測試（SQLite 的串聯行為與 SQL Server 不同，不能只在 SQLite 驗證）。
- [每日上限為軟上限且以資料列計數] → 見 D3；風險是可忽略的當日多 1 筆或作者刪劇本後略鬆，不影響安全性，因為上限的目的是限制洗版而非計費。
- [管理員看不到檢舉人身分，無法辨識惡意檢舉者] → 資料最小化優先；每版本一次與每日 5 次限制面積；若日後需要，可新增「依檢舉人彙總」的後台檢視，那是另一個 change 並須修訂 AGENTS.md。
- [自動結案為「下架」的語意：作者自行下架也結為「下架」，管理員沒有下過決定] → 規格定義的是「版本已不在上架」；稽核仍能區分（管理員下架有 `script.remove`，作者下架沒有）；檢舉的 `closed_result` 不對任何人顯示（檢舉人不被通知、作者看不到）。
- [政策頁過時敘述被一併更正，造成使用者以為這是新規則] → 兩頁文字更新是補齊 8a 上線後的事實，版本字串提高；不要求既有使用者重新同意（沿用現行行為，記在 Open Questions）。
- [`removed` 版本的檢舉在 12 個月後被清除，期間管理員下架的決定仍在稽核（24 個月）] → 兩者期限不同是刻意的：稽核保存決定，檢舉的文字是玩家文字（個資），保存期限較短。

## Migration Plan

1. 備份資料庫（既有步驟）。
2. 執行遷移 `0022_script_reports`（`down_revision = "0021_script_publishing"`）：新增資料表 `script_reports`（含索引與唯一約束，外鍵名稱以專案的命名慣例）、`users.keep_published_scripts`（NOT NULL、`server_default` 假值）、`runtime_limits` 一列 `script_report_daily = 5`（只在缺少時插入）。純 expand：舊版程式不認識新表與新欄位也照常運作（舊版的抹除流程不會清 `script_reports`，但遷移後尚無任何檢舉資料，**回復到舊版程式之前須確認 `script_reports` 為空**，否則舊版抹除含檢舉的作者會被無動作外鍵擋下）。
3. 部署新版程式。不需要 `flask seed-scripts`。
4. 回復（downgrade）：刪除資料表與欄位、刪掉 `runtime_limits` 的那一列；`recreate="never"` 與 `mssql_drop_default=True` 的做法同 0017、0021（`batch_alter_table`，避免 SQLite 重建 `users` 時串聯清掉資料）。
5. 同步 `docs/project資料結構文件.md` 與第 14 節異動紀錄（有測試守門與列骨架工具）。

## Open Questions

- 既有使用者在條款版本提高後是否需要重新同意：沿用現行（不重置），若法務覆核要求再另立 change。
- 被保留的劇本若之後被管理員下架，是否保留審核備註給管理員：沿用 8a（備註隨版本保存與刪除），不另處理。
