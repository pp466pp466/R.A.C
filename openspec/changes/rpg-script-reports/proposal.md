# Proposal

## Why

8a（`rpg-script-publishing`）讓玩家創作的劇本能「先審後發」上架，但上架之後平台只有兩條路能讓有問題的劇本下架：管理員自己巡視，或作者自己下架。**玩家看到問題劇本卻沒有任何管道回報**；AGENTS.md 第 7 節已定「登入且通過年齡確認的玩家可檢舉已上架的劇本」，只是還沒實作。

另一個缺口在帳號刪除：現在刪帳號或閒置 12 個月被清除時，作者的所有劇本一併消失。AGENTS.md 第 11 節已定帳號偏好「匿名保留我已發佈的劇本」（預設關閉、於刪除申請視窗再次確認），只有目前上架的版本保留，筆名改為「已註銷的作者」。

AGENTS.md 已於 `67ef67b`（8a 的規範）與 `5543cbe`（8b 探索補定：檢舉掛在版本、優先度規則、系統結案、內建劇本以種子鍵辨識）修訂完成。這個 change 實作它們。

## What Changes

**A. 劇本檢舉**
- **新資料表 `script_reports`**（遷移 `0022`，expand-only）：每筆檢舉掛在檢舉當下的**上架版本**，記錄檢舉人、固定列舉的理由（可多選）、自寫文字（上限 200 字）、建立時間與結案資訊。同一人對同一版本只能檢舉一次（唯一鍵 `(檢舉人, 版本)`）；新版本經核可上架後可再檢舉。
- **玩家端**：`POST /api/sessions/catalog/<版本識別碼>/report`；劇本詳情頁新增「檢舉」按鈕與自製 Modal（理由多選、選填文字、字數計數）；詳情回應新增 `report_state`（`available`／`reported`／`unavailable`），自己的劇本與內建劇本不可檢舉（按鈕不顯示）。登入且通過年齡確認才可檢舉；檢舉人不會收到任何通知。
- **每人每日上限**：新增營運限制 `script_report_daily`（初始 5、範圍 0 至 100，台灣時間 0 點刷新；0 表示暫停接受檢舉），存放於 `runtime_limits`，每次使用時現讀，以既有的 `flask set-script-limit` 修改並寫稽核，初始值由遷移寫入。
- **管理員端**：審核清單新增「被檢舉」檢視（`status=reported`）：只列「上架中且有未結案檢舉」的版本，**紅線理由優先、其次未結案人數、再其次最早檢舉時間**；所有清單的每一筆多出 `report_count` 與 `red_line_reported`。新增 `GET …/versions/<id>/reports`（檢舉的理由與自寫文字；**不含檢舉人身分**；每次讀取寫稽核 `report.read`）與 `POST …/versions/<id>/reports/dismiss`（結案為「不成立」，不需密碼，寫稽核 `report.dismiss`）。**結案為「下架」沿用既有的管理員下架**（須密碼），下架時該版本未結案的檢舉由系統自動結為「下架」。
- **系統自動結案**：版本因作者或管理員下架 → 結為「下架」；版本被新版本取代 → 結為系統專用的「被取代」。恢復上架不重開已結案的檢舉。
- **保存與刪除**：結案的檢舉保存 12 個月後由 `flask purge-expired-data` 清除（未結案者與所屬版本相同）；檢舉人刪帳號時其檢舉一併刪除（下架決定與稽核不受影響）；納入檢舉人的個人資料匯出（理由、文字、時間，**不含結案結果**，因為檢舉人不會被通知結果）；作者刪帳號時其版本上的檢舉隨版本刪除。
- 內建劇本不可檢舉；檢舉文字與理由不送入模型、不進日誌與稽核（日誌只有內部編號與整數長度）。

**B. 匿名保留我已發佈的劇本**
- **新欄位 `users.keep_published_scripts`**（NOT NULL、預設關閉，遷移 `0022`）；`GET／PUT /api/account/script-retention`；帳號頁新增開關；**刪除帳號視窗再次確認**：視窗內顯示同一個核取方塊（目前有上架劇本時才顯示），`POST /api/account/delete` 可帶 `keep_published_scripts` 一併寫入偏好。
- **抹除流程**：`AccountErasureService` 在「使用者申請刪除」與「連續 12 個月未登入」兩種原因下、於**執行刪除當下**讀取偏好；開啟時，在同一個交易、刪除使用者之前：刪除沒有上架版本的整份劇本與其餘版本（草稿、待審、被退回、已下架、舊版本），保留目前上架的版本，把劇本的作者關聯解除、筆名改為「已註銷的作者」、短代號清空、審核備註清除。未滿 18 歲抹除與逾時未確認年齡一律全刪，不看偏好。備份還原後依 `deletion_tombstones` 重新抹除時不區分偏好（已知限制，AGENTS.md 已載明）。
- **修正「沒有作者＝內建劇本」的假設**：匿名保留會讓玩家劇本也沒有作者，而現有程式以 `creator_id IS NULL` 判斷內建劇本（`AdminScriptRepository.find_ref` 的 `is_builtin`、`ScriptRepository.remove_by_admin`、後台前端 `admin-format.js` 的下架按鈕條件 `!!it.author_id`），不修正的話保留的劇本會變成**管理員無法下架**。改以種子鍵（`seed_key IS NOT NULL`）判斷；管理員清單與詳情新增 `is_builtin`。
- 暱稱保留字新增「已註銷的作者」（現在只擋「官方」）。
- 個人資料匯出的帳號資料新增 `keep_published_scripts`。

**C. 政策頁**
- `/privacy` 與 `/terms` 補充（AGENTS.md 第 11 節）：筆名會公開顯示、劇本內容經 AI 檢查、送審公開的劇本可能由管理員審閱、發佈即授權保存並複製進他人存檔、刪帳號時的保留選項、檢舉的內容會提供管理員檢視、檢舉保存期限；並**更正過時的敘述**（兩頁仍寫「公開分享尚未開放」，8a 之後已不正確）。提高 `CURRENT_TERMS_VERSION`，保存期限表與 `tests/test_legal_pages.py` 同步。

**其他**
- `AUDIT_ACTIONS` 新增 `report.read`、`report.dismiss`（21 → 23）；`script_events` 與 `lifecycle_events` 新增白名單事件；後台 `admin.js` 新增「被檢舉」檢視與檢舉讀取、結案。
- `tests/architecture`：新資料表登記到抹除覆蓋測試；新增「檢舉結案欄位只由 `ScriptReportRepository` 寫入」守門。
- 文件：`docs/project資料結構文件.md`（第 14 節異動紀錄）、`docs/資料模型-data-model.md`、`docs/系統架構-architecture.md`、`docs/前端結構-frontend.md`、`docs/帳號與安全-auth-security.md`、`docs/RPG.md`、`docs/路線圖-roadmap.md`；簡報衝突依 `deck-conflict-watch` 只記錄。
- **BREAKING**：無。`GET /api/admin/scripts` 的清單項目新增三個鍵（`report_count`、`red_line_reported`、`is_builtin`），沒有移除；`author_id` 為空的語意由「內建劇本」改為「沒有作者（內建或已註銷）」。

**不在本 change 內**：檢舉人通知與結果查詢（AGENTS.md：檢舉人不會收到通知）；顯示檢舉人身分給管理員（資料最小化，v1 不提供）；以檢舉自動下架；對檢舉濫用者的懲罰機制（只有每日上限與每版本一次）；被保留劇本的作者回訪（已無作者，無人可編輯）；金流與儲值。

## Capabilities

### New Capabilities

- `script-reports`：玩家檢舉上架劇本：端點與驗證、理由列舉與自寫文字、唯一性與每日上限、內建與自己的劇本不可檢舉、年齡閘門、系統自動結案、保存與刪除、匯出、詳情頁的檢舉入口與 Modal、檢舉內容不進模型與日誌。
- `admin-script-reports`：管理員處理檢舉：被檢舉清單與優先度、讀取檢舉（稽核、不含檢舉人身分）、結案為「不成立」、結案為「下架」的銜接、稽核動作、「被檢舉」檢視與檢舉面板。
- `script-anonymous-retention`：帳號偏好「匿名保留我已發佈的劇本」：偏好端點與帳號頁開關、刪除視窗再確認、抹除時保留上架版本的步驟與適用原因、筆名改為「已註銷的作者」、備份還原的已知限制、保留劇本仍可被檢舉與下架。

### Modified Capabilities

- `account-deletion`：「抹除的範圍」——劇本與標籤不再一律刪除，偏好開啟時目前上架的版本匿名保留。
- `script-takedown`：管理員下架的內建劇本判斷由「沒有作者」改為種子鍵；匿名保留的劇本可被下架。
- `admin-script-review`：審核清單新增 `reported` 狀態與排序、每筆新增三個鍵、內建劇本的判斷。
- `rpg-script-catalog`：詳情回應新增 `report_state`。
- `admin-audit-log`：動作列舉新增 `report.read`、`report.dismiss`。
- `script-store`：（ADDED）以種子鍵辨識內建劇本、匿名保留劇本的資料形狀與筆名。
- `runtime-limits`：（ADDED）`script_report_daily`。
- `data-retention`：（ADDED）結案檢舉保存 12 個月。
- `personal-data-export`：（ADDED）匯出檢舉與匿名保留偏好。
- `player-nickname`：（ADDED）暱稱不得冒用「已註銷的作者」。
- `service-observability`：（ADDED）檢舉與匿名保留事件只含白名單欄位。
- `auth-pages`：（ADDED）`/privacy` 與 `/terms` 對檢舉、匿名保留與公開分享的說明。

## Impact

- **程式（後端）**：新增 `app/models/script_report.py`、`app/repositories/script_report_repository.py`、`app/services/script_report_service.py`、遷移 `migrations/versions/…_0022_script_reports.py`；修改 `app/models/script.py`（`RETIRED_AUTHOR_NAME`）、`app/models/user.py`（`keep_published_scripts`）、`app/models/admin_audit_log.py`、`app/repositories/script_repository.py`（下架與核可時自動結案、`retain_published_anonymously`、內建判斷）、`admin_script_repository.py`（報告聚合、`is_builtin`）、`user_repository.py`、`retention_repository.py`、`app/services/admin_service.py`（`script_reports`、`dismiss_reports`）、`admin_script_views.py`、`game_entry_service.py`（`report_state`）、`account_erasure_service.py`、`account_deletion_service.py`、`data_retention_service.py`、`personal_export_service.py`、`runtime_limits.py`、`nickname.py`、`access_policy.py`（`require_can_report`）、`legal.py`；`observability/script_events.py`、`lifecycle_events.py`；控制器：`session_api_controller.py`、`account_api_controller.py`、`admin_api_controller.py`；`registry.py` 組裝。
- **程式（前端）**：`game/script_detail.html`、`script-detail.js`、新增 `script-report.js`（理由標籤、驗證、請求本文，純函式）、`game-labels.js`、`game-api.js`；`auth/account.html`、`account.js`、`account-lifecycle.js`；`admin/console.html`、`admin.js`、`admin-format.js`；`legal/privacy.html`、`legal/terms.html`；CSS。
- **API**：新增 `POST /api/sessions/catalog/<id>/report`、`GET／PUT /api/account/script-retention`、`GET /api/admin/scripts/versions/<id>/reports`、`POST /api/admin/scripts/versions/<id>/reports/dismiss`；修改 `POST /api/account/delete`（選填 `keep_published_scripts`）、`GET /api/admin/scripts`（`status=reported`）、`GET /api/sessions/catalog/<id>`（`report_state`）。
- **資料庫**：遷移 `0022`（expand-only：新資料表 `script_reports`、`users.keep_published_scripts`、`runtime_limits` 一列初始值）；須同步 `docs/project資料結構文件.md` 與第 14 節異動紀錄；SQLite 與 SQL Server 雙軌測試。**SQL Server 的串聯路徑**：`script_reports.version_id` 為無動作的外鍵（與 `sessions.source_version_id` 同一個理由，錯誤 1785），由抹除流程先刪除。
- **風險**：①`creator_id IS NULL` 的假設散落多處，漏改一處就會讓保留的劇本無法下架或顯示成內建，需盤點並以測試固定；②抹除流程的順序（清存檔的來源參照 → 刪檢舉 → 刪版本 → 解除作者關聯 → 刪使用者）在同一個交易內，任何一步漏掉都會讓刪除失敗（偏向刪除而非洩漏）；③每日上限以資料列計數，作者刪除劇本會使檢舉消失而讓上限鬆一點（可接受）；④兩個並行請求可能讓某人當日超過上限 1 筆（軟上限）。
