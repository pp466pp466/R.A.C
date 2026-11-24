# Tasks

> 分批原則（`openspec-batch-workflow`）：後端有改動（模型常數、Repository、Service、端點），但範圍很小，所以中間批次（批 1、批 2）只跑相關測試加全域守門子集（`tests/architecture`、`test_service_errors`、`test_design_docs`、`test_readme`、`test_restricted_session`、`test_legal_pages`、`test_agents_skill_sync`）；批 3 是前端與政策頁，加跑 Node 全套與對應頁面測試；**全套 pytest 只放最後一批（批 4，驗收前）**。沒跑全套的批次，完成註記要如實寫「未跑全套」。若批 1 或批 2 的相關測試顯示改動波及寫死的數量（路由數、鍵數、事件欄位白名單）而守門子集抓不到，才提前補跑一次全套。
>
> 規範（AGENTS.md 第 10、11 節）已先單獨提交為 `99022ff`，本 change 不再改 AGENTS.md。**沒有資料庫結構變更、沒有遷移。**
>
> 範圍外（部署之後的維運操作，另行向使用者確認、先做 RDS 快照）：清理展示環境 rac001 的非管理員帳號。

## 1. 資料與抹除出口（批 1）

- [x] 1.1 `app/models/admin_audit_log.py` 新增 `ACTION_USER_DELETE = "user.delete"` 並加入 `AUDIT_ACTIONS`（23→24）；`app/models/deletion_tombstone.py` 新增 `REASON_ADMIN_DELETED = "admin_deleted"` 並加入 `DELETION_REASONS`；`app/observability/admin_events.py` 的 `DANGEROUS_ACTIONS` 加入 `ACTION_USER_DELETE`。同步更新寫死數量與列舉的既有測試（`tests/test_admin_audit_repository.py` 的集合與 `== 23`、`tests/test_admin_events.py`、`tests/support/db_schema.py` 的列舉對照）並確認改動是行為刻意改變。**同一項一併同步文件**（守門測試會要求）：`README.md` 管理員後台一節的稽核動作清單加入 `user.delete`（`tests/test_readme.py` 逐一檢查 `AUDIT_ACTIONS`），`docs/project資料結構文件.md` 內 `admin_audit_logs.action` 與 `deletion_tombstones.reason` 的列舉值加入新值，並在檔案最下方第 14 節追加異動紀錄（只有列舉值變動、沒有結構變動）。驗證：`tests/test_admin_audit_repository.py tests/test_admin_events.py tests/test_project_db_schema_doc.py tests/test_readme.py`。
- [x] 1.2 `UserRepository.delete_player_by_admin(user_id)`：以 ORM 表達式的條件式 `DELETE`（`id` 相符且 `role != admin`，不限制狀態、不要求生日），`synchronize_session=False`，回傳是否刪到一列（與 `delete_unconfirmed_player` 同型）。新增 Repository 測試：一般玩家的各種狀態（`active`、`suspended`、鎖定、`deletion_pending`）都刪得掉；管理員刪不掉且資料不變；不存在回傳 `False`。驗證：該測試檔與 SQL Server `project_test` 軌（`-m sqlserver`，同時只能一個 pytest 行程）。
- [x] 1.3 `AccountErasureService.erase_by_admin(user_id, *, now)`：呼叫 `erase(..., delete=users.delete_player_by_admin, reason=REASON_ADMIN_DELETED, retain_scripts=False)`，與 `erase_underage` 並列，更新模組說明。新增測試：寫入原因為 `admin_deleted` 的刪除紀錄；偏好開啟的帳號其上架劇本也全刪；別人的存檔保留且來源參照為空；帳本匿名化（使用者與對話編號為空）；目標是管理員時回傳 `False`、整個交易回復、沒有刪除紀錄；`tests/architecture/test_account_erasure_coverage.py` 仍通過。驗證：抹除相關測試檔（含 SQLite 與 SQL Server 兩軌）加全域守門子集。
- [x] 1.4 批 1 收尾：跑相關測試與全域守門子集，更新 tasks 完成註記與 HANDOFF（**未跑全套**，如實寫明）。

### 批 1 完成註記（2026-11-24；只跑相關測試加全域守門子集，**未跑全套 pytest 與全套 Node**）

- **1.1**：`ACTION_USER_DELETE = "user.delete"`（`AUDIT_ACTIONS` 23→24）、`REASON_ADMIN_DELETED = "admin_deleted"`（`DELETION_REASONS`）、`DANGEROUS_ACTIONS` 加入 `user.delete`（9→10）。同步更新寫死的既有測試（`test_admin_audit_repository.py`：24 個動作；`test_admin_events.py`：十個危險操作；`tests/js/admin-format.test.js`：動作清單）。文件：`README.md` 稽核動作清單與原因欄位說明、`docs/project資料結構文件.md`（標頭日期與對應 change、兩個欄位說明、列舉對照表、稽核動作與刪除原因字典、第 14 節的異動紀錄：只有列舉值變動，沒有結構變更、沒有遷移、不需要升級 `project`）。
- **與原 tasks 的出入（一處）**：`tests/test_admin_format_labels.py` 要求前端標籤涵蓋後端全部動作值，所以 `admin-format.js` 的動作標籤 `'user.delete': '刪除帳號'` 在本批就要加（原本在 3.1）；3.1 只剩稽核細節的「原狀態」格式。
- **1.2**：`UserRepository.delete_player_by_admin`：`id` 相符且 `role != admin` 的條件式 ORM `DELETE`，不限制狀態與生日。新增 `tests/test_user_repository_admin_delete.py`（SQLite 與 SQL Server `project_test` 兩軌各 9 項：使用中、停權、鎖定、待刪除、沒有生日都刪得掉且只刪指定的；管理員刪不掉；不存在；刪除當下再確認條件；重複刪除只成功一次）。
- **1.3**：`AccountErasureService.erase_by_admin(user_id, *, now)` = `erase(..., delete=delete_player_by_admin, reason=REASON_ADMIN_DELETED)`（`retain_scripts` 不傳）。新增 `tests/test_account_erasure_admin.py`（兩軌各 11 項）：三種狀態立即抹除、刪除紀錄只有內部編號與時間與原因、偏好開啟或關閉劇本都全刪、他人存檔保留且來源參照為空值、帳本匿名化與對話刪除、管理員不被刪、**條件不成立時先寫入同一 Session 的稽核列隨交易一起回復**（含劇本清理）、成功時稽核列保留且不帶外鍵。
- **驗證**：新增 40 項（兩軌）全數通過；抹除與架構相關 772 通過、2 略過（既有）；全域守門子集加相關測試 1329 通過、2 略過；Node `admin-format.test.js` 47 通過；`openspec validate admin-delete-account --strict` 通過。
- **簡報衝突**：檢查過，`docs/2.0簡報.md`、圖解產生器與 `build_pptx_40.py` 都沒有寫稽核動作的數量，沒有衝突。
- **未驗證**：全套 pytest 與 Node（批 4）；服務層與端點（批 2）。

## 2. 服務層與端點（批 2）

- [x] 2.1 `AdminService` 注入抹除服務（`registry.py` 的後台組裝處，沿用 `AccountErasureService(users, scripts, tombstones, commit=session.commit, rollback=session.rollback)`），新增 `delete_user(admin, public_id, *, reason, password)`：`require_admin` → `_reason` → `_find_user`（404）→ 角色不是 `user` 則 `AdminTargetForbidden` → `_confirm`（動作 `user.delete`）→ `_record`（原因與 `{"from_status": …}`，`changed=False`）→ `erase_by_admin`；回傳 `False` 時拋 `Conflict(code="state_changed")`；成功後才呼叫 `admin_events.log_action`。**不使用 `_manageable_target`**（它會擋 `deletion_pending`）。
- [x] 2.2 `app/controllers/admin_api_controller.py` 新增 `POST /users/<public_id>/delete`：`_admin()`、`read_json_object()`、呼叫服務、回 `jsonify(ok=True)`；Controller 不寫權限判斷。**同一項一併更新 `README.md` 的端點表與路由數**（`tests/test_readme.py` 比對 README 與實際路由，寫死的數量 27→28 要隨之更新並確認是刻意改變）。
- [x] 2.3 Service 測試（新檔 `tests/test_admin_delete_user.py`）：成功路徑（帳號、對話、存檔、錢包、劇本、身分都不存在；刪除紀錄與 `user.delete` 稽核；稽核目標是對外識別碼、細節只有原狀態、沒有 email 或暱稱）；`deletion_pending` 可刪；管理員與自己 `admin_target_forbidden` 且無稽核；不存在 404；缺少或過長的原因 400 欄位 `reason`；密碼錯誤 400 欄位 `password` 並保存 `reauth.failed`（含 `user.delete`）、連續失敗鎖定；沒有密碼 `no_password`；**原子性**：注入失敗的 `erase`（或目標變成管理員）時沒有稽核列、沒有刪除紀錄、帳號仍在；成功後日誌只有管理員編號與動作名稱。
- [x] 2.4 API 測試（沿用既有後台 API 測試的做法）：非管理員與未登入一律 404（含格式錯誤的本文）、缺 CSRF 被擋、回應形狀 `{"ok": true}`、刪除後原 session cookie 視為未登入、同一 Google 信箱可重新建立新帳號；驗證：相關測試加全域守門子集。
- [x] 2.5 批 2 收尾：更新 tasks 完成註記與 HANDOFF（**未跑全套**，如實寫明）。

### 批 2 完成註記（2026-11-24；只跑相關測試加全域守門子集，**未跑全套 pytest 與全套 Node**；批 1 已提交為 `704ff80`）

- **2.1**：`AdminService.delete_user(admin, public_id, *, reason, password)`，順序：`require_admin` → `_reason` → `_find_user`（404）→ 角色不是 `user` 則 `AdminTargetForbidden` → `_confirm`（動作 `user.delete`）→ `_record`（原因與 `{"from_status": …}`）→ `erase_by_admin`；回傳 `False` 拋 `Conflict(code="state_changed")`；成功後才 `admin_events.log_action`。**沒有用 `_manageable_target`**（`deletion_pending` 可刪）。`AdminService` 新增必填的 `erasure` 參數：`registry.get_admin_service` 以與其他抹除路徑相同的組裝（`UserRepository`、`ScriptRepository`、`DeletionTombstoneRepository`、`session.commit`／`rollback`）注入；`tests/support/admin.py` 的 `AdminKit` 同步。
- **2.2**：`POST /api/admin/users/<public_id>/delete`（`_admin()` → `read_json_object()` → 服務 → `jsonify(ok=True)`）。**README 端點表**新增一列，`tests/test_readme.py` 的後台路由數 27→28；`tests/test_admin_api.py` 的路由集合測試（26→27 條 `/api/admin`）與身分矩陣、CSRF、畸形本文參數加入 `delete_user`。
- **與原 tasks 的出入（一處）**：`tests/test_design_docs.py` 要求每條 API 路由出現在對應的設計文件，所以 `docs/帳號與安全-auth-security.md` 在本批就要同步（端點表一列、稽核 23→24、操作對象的例外、新增「管理員刪除帳號」一段）；批 4 的 4.5 只剩其他設計文件的核對。
- **2.3**：新增 `tests/test_admin_delete_user.py`（SQLite 與 SQL Server 兩軌各 30 項）：成功路徑（帳號不存在、一筆稽核與一筆刪除紀錄、日誌只有管理員編號與動作名稱、不含理由與 email）、稽核沒有 email 或暱稱且在帳號刪除之後仍在、停權／鎖定／待刪除都可刪、對話與劇本全刪（偏好開啟也一樣）、他人不受影響、同一 email 可重建、舊 session 失效、管理員與自己 `admin_target_forbidden`、不存在 404、非管理員 404、原因缺漏或過長 400 欄位 `reason`（200 字可）、密碼錯誤或缺漏 400 欄位 `password` 並保存 `reauth.failed`（含 `user.delete`）、`no_password`、連續 5 次鎖定、**原子性兩條**（目標在檢查後變成管理員 → 409 且稽核列與刪除紀錄都沒有；抹除拋例外 → 回復後稽核列不存在、沒有成功日誌）。
- **2.4**：`tests/test_admin_api.py` 新增 6 組 API 測試：回 `{"ok": true}`、帳號消失、一筆 `user.delete` 稽核與一筆 `admin_deleted` 刪除紀錄、重複刪除 404、被刪玩家的瀏覽器視為未登入、刪管理員 403 `admin_target_forbidden`、缺原因或缺密碼或密碼錯 400 且帳號仍在、不存在 404、沒有 GET 與批次端點。
- **驗證**：`test_admin_api.py` 與 `test_admin_delete_user.py` 共 241 通過；全域守門子集加後台與抹除相關測試 1511 通過、2 略過（既有）；`openspec validate admin-delete-account --strict` 通過。
- **簡報衝突**：檢查過，`docs/2.0簡報.md` 只有停權的狀態圖（第 453 行），沒有後台端點或功能清單的數字，沒有衝突。
- **未驗證**：全套 pytest 與 Node（批 4）；前端與條款（批 3）；真實瀏覽器操作（批 3 的 3.5）。

## 3. 前端與政策頁（批 3）

- [x] 3.1 `app/static/js/admin/admin-format.js`：動作標籤加 `'user.delete': '刪除帳號'`、稽核細節顯示「原狀態：…」（沿用 `user.suspend` 的格式）；更新 `tests/test_admin_format_labels.py` 與對應的 Node 測試。
- [x] 3.2 `app/static/js/admin/admin.js` 與 `app/templates/admin/console.html`：`userActions.delete`（角色為 `user` 才有，含 `suspended`、鎖定與 `deletion_pending`）、`CONFIRMS.delete`（標題、不可復原與連帶刪除資料的警告、`needsReason`、「確認永久刪除」、`admin-btn--danger`）、`askDelete`、`submitConfirm` 新增 `kind === 'delete'` 分支（清空 `user`、從 `locked`／`suspended`／`appeals` 移除該帳號的列、`markStale('flagged','feedback','overview','audit')`，**不可落入預設分支的 `user.value = data.user`**）、模板按鈕；文字全為繁體中文、純文字渲染、沒有 `v-html` 與 `data-bs-*`。
- [x] 3.3 後台前端測試：以既有後台頁面測試的方式（vm 沙盒或字串斷言）涵蓋：管理員目標沒有按鈕、`deletion_pending` 與 `suspended` 有按鈕、確認視窗文字、成功後面板清空與清單移除、錯誤顯示在密碼或原因欄位、密碼送出當下清空。驗證：Node 全套與 `tests/test_admin_page.py`。
- [x] 3.4 `app/templates/legal/terms.html` 第 4 節加一條（管理員得因違反內容規範或條款刪除帳號與其全部資料，無法復原）；依既有慣例遞增 `CURRENT_TERMS_VERSION`（`draft-10` → `draft-11`，先以 `git log -- app/services/legal.py` 確認慣例）；更新 `tests/test_legal_pages.py`、`tests/test_auth_pages.py` 等寫死版本或條款文字的測試。驗證：這幾個測試檔加全域守門子集。
- [x] 3.5 無頭 Chrome（自有實例：全新 SQLite、假模型，`127.0.0.1` 非 5000 埠）實機走一次：以管理員登入，查詢一位玩家 → 按「刪除帳號」→ 錯誤密碼 → 缺原因 → 正確操作 → 面板清空、稽核分頁出現「刪除帳號」、該玩家的 cookie 再請求被視為未登入、管理員查詢另一位管理員沒有按鈕；1280 與 360 寬度各目視確認 Modal 不溢出。
- [x] 3.6 批 3 收尾：跑 Node 全套與全域守門子集，更新完成註記與 HANDOFF（**未跑全套 pytest**，如實寫明）。

### 批 3 完成註記（2026-11-24；前端與政策頁：**跑 Node 全套與相關頁面測試加全域守門子集，未跑全套 pytest**；批 2 已提交為 `88ce109`）

- **3.1**：`admin-format.js` 的稽核細節 `user.delete` 與 `user.suspend` 同為「原狀態：…」（動作標籤已在批 1 加入）；`tests/js/admin-format.test.js` 新增細節、動作標籤與 `reauth.failed` 的「嘗試：刪除帳號」。
- **3.2**：`admin.js`：`userActions.delete`（角色為 `user` 即可，含停權、鎖定與 `deletion_pending`）、`CONFIRMS.delete`（標題「永久刪除這個帳號？」、狀態碼 `ADMIN_DELETE_USER`、`needsReason`、「確認永久刪除」，警告寫明不可復原、沒有寬限期與連帶刪除的資料、其他玩家已開出的存檔不受影響、同一 Google 帳號可重新註冊）、`askDelete`（管理員或沒有目標時不開窗）、`removeDeletedFromLists`、`submitConfirm` 的 `kind === 'delete'` 分支（`user` 設為 `null`、清單移除、`markStale('flagged','feedback','overview','audit')`，不落入預設的 `user.value = data.user`）；`console.html` 新增 `admin-btn--danger` 的「刪除帳號」按鈕。`userNote` 對 `deletion_pending` 的說明由「無法操作」改為「無法停權或調整，但仍可立即永久刪除」（否則與新按鈕矛盾）。純文字渲染，沒有 `v-html` 與 `data-bs-*`。
- **與原 tasks 的出入（兩處）**：①design D6 寫「確認鈕使用 `admin-btn--danger`」，但確認鈕是共用元件 `ui-modal` 的主要鈕（琥珀橙，與 `admin-btn--danger` 同色），元件沒有樣式參數，為此改共用元件會波及所有視窗；所以只有面板上的「刪除帳號」鈕用 `admin-btn--danger`（與「停權」一致），確認鈕維持共用樣式。②`userNote` 的說明文字（上一項）。
- **3.3**：新增 `tests/js/admin-delete-user.test.js`（19 項）：按鈕條件（任何狀態的玩家有、管理員與無目標沒有）、待刪除的說明、確認視窗文字與規格、缺原因或缺密碼不送出、請求本文只有密碼與原因、成功後面板為 `null`、三份清單的列移除且保留他人、其餘清單與總覽標為過期、密碼在送出當下清空（成功與失敗）、重複按只送一次、錯誤顯示在密碼欄與原因欄且視窗保持開啟、`no_password`、409 與 403 的視窗內訊息、關閉視窗清空、401 導向登入、樣板按鈕與純文字渲染。**測試輔助抽出**：`admin.test.js` 內的 `setupAdmin` 沙盒移到 `tests/js/support/admin-sandbox.js`，兩個測試檔共用（`admin.test.js` 的 20 項照舊通過）。快速變異：把成功分支的 `kind === 'delete'` 改名，3 項測試失敗，已還原並比對確認。
- **3.4**：`terms.html` 第 4 節加一條（管理員得因違反內容規範或本條款刪除帳號與其全部資料，無法復原；立即執行、不經 7 天寬限期、不適用第 5 節的匿名保留選項、劇本一併刪除）；`CURRENT_TERMS_VERSION` `2026-11-22-draft-10` → `2026-11-24-draft-11`（慣例：每次政策頁文字變動就遞增 draft 序號並換成當天日期，`git log -- app/services/legal.py` 確認）；既有使用者不被要求重新同意。`tests/test_legal_pages.py`：版本斷言與「不是先前版本」清單、新增 2 項（條款一句與細節、不承諾事先通知或復原）。隱私權政策沒改（刪除範圍已說明）。
- **3.5 實機（無頭 Chrome，自有實例：全新 SQLite、127.0.0.1:5013、假模型位址，沒有碰 5000 埠與使用者的驗收環境；腳本與截圖在 scratchpad，不進專案）**：1280 寬完整走過——查詢玩家、按「刪除帳號」、確認視窗（標題、警告、對象、狀態碼）、什麼都沒填（兩欄各有提示、不送出）、只缺原因（不送出）、錯誤密碼（送出一次、錯誤顯示在視窗內、密碼欄清空、面板仍在）、正確操作（出現「已刪除帳號。」、視窗淡出後移除、面板清空）、稽核分頁出現「刪除帳號」「原狀態：使用中」與「嘗試：刪除帳號」且沒有信箱、被刪玩家原 cookie 的 `/api/auth/me` 由 200 變 401、查另一位管理員沒有按鈕且顯示說明、待刪除玩家有按鈕（沒有停權與調整）並實際刪除成功、查已刪除信箱沒有面板；360 寬確認視窗不橫向溢出、確認鈕在可視範圍內（視窗只需捲 16px）、Esc 取消後面板仍在。資料庫直接檢查：兩個被刪帳號不存在、各有 `admin_deleted` 刪除紀錄與 `user.delete` 稽核（只有對外識別碼、原因與原狀態；沒有信箱、暱稱）。過程中一次檢查失敗是我在淡出動畫途中判斷視窗是否關閉（時序，非缺陷），改為等視窗移除後重驗通過。
- **3.6 驗證**：Node 全套 1872 項通過（含新增 19 項與 `admin-format` 的新斷言）；pytest：`test_legal_pages`、`test_auth_pages`、`test_admin_page`、`test_admin_format_labels` 加全域守門子集共 1235 通過、2 略過（既有）；另跑 `test_assets_manifest`、`test_admin_flow`、`test_admin_two_step_login` 48 通過；`openspec validate admin-delete-account --strict` 通過。**未跑全套 pytest**（只放批 4）；本批沒有改 `app/` 的 Python 程式（只有 `legal.py` 的版本字串）。
- **簡報衝突（只通知，沒有改）**：①`docs/2.0簡報.md` 第 981 行與 `docs/diagrams/build_pptx_40.py` 第 530 行（進而 `docs/4.0簡報.pptx`）寫條款版本 `2026-11-22-draft-10`，現為 `2026-11-24-draft-11`；②`docs/diagrams/build_diagrams_system.py` 第 498 行「停權、刪除內容、調整等級與遊戲幣等高風險操作須重新輸入密碼」少列「刪除帳號」（不是錯誤，是不完整；批 1 起就如此）；③簡報的前端測試數（1,610）早已落後（現為 1,872），本批只是又增加 19 項。
- **未驗證**：全套 pytest（批 4）；使用者本人在真機上對 Modal 警告文字與條款那一句的驗收（Workflow follow-up）；設計文件與 README 中寫「目前條款版本 `2026-11-22-draft-10`」的幾處（`docs/帳號與安全-auth-security.md`、`docs/營運與部署-operations.md`，README 的歷史段落不改）留給批 4 的 4.1 與 4.5。

## 4. 文件、驗證與收尾（批 4，最後一批）

- [x] 4.1 README 的「驗收紀錄」與條款版本說明收尾（端點表與稽核動作已在批 1、2 同步）；核對 `docs/project資料結構文件.md` 的異動紀錄與實際一致；`tests/test_readme.py tests/test_project_db_schema_doc.py` 通過。
- [x] 4.2 依 `deck-conflict-watch` 對照 `docs/2.0簡報.md`、六張圖解與 `docs/4.0簡報.pptx`（後台功能清單、稽核動作數、抹除路徑、測試數字）：**只通知，有衝突列出請使用者決定，不自行修改簡報**；沒有衝突也要說一句已檢查。
- [x] 4.3 變異驗證（在 scratchpad 的專案副本上，每個變異有行程逾時；存活者先做差分推理再分類為缺口或等價）：`delete_player_by_admin` 的管理員保護與狀態不限制、`delete_user` 的角色判斷與 `deletion_pending` 放行、稽核先於抹除的順序與失敗回復、`retain_scripts=False`、`CONFIRMS.delete` 的 `needsReason`、成功分支不落入預設、按鈕條件、稽核標籤。補上抓不到的測試。
- [x] 4.4 全套驗證：pytest 全套（含 SQL Server `project_test` 軌，同時只能一個行程，輸出存檔不接 tail）與 Node 全套；`openspec validate admin-delete-account --strict`、`openspec validate --specs`；`alembic check`（確認沒有模型漂移）；把筆數寫進完成註記。
- [x] 4.5 更新 `docs/` 對應的設計文件（對照表見 `docs/README.md`：後台與稽核、抹除與保存期限、帳號與安全、前端結構），並讓 `tests/test_design_docs.py` 通過。

### 批 4 完成註記（2026-11-24；**跑了 pytest 全套與 Node 全套**；批 3 已提交為 `e5907f8`）

- **4.1／4.5 文件**：README 新增「管理員刪除帳號（admin-delete-account）」一節與「驗證與驗收紀錄（批 4）」，後台一節的危險操作清單加入「刪除帳號」並註明 `deletion_pending` 例外，抹除出口一條與政策頁版本說明補上 draft-11；設計文件六份同步：`系統架構-architecture.md`（第五條抹除路徑）、`資料模型-data-model.md`（保存與刪除的流程圖與刪除紀錄原因）、`project資料結構文件.md`（第 9 節生命週期圖與表、第 14 節追加一列文件異動，沒有結構變更）、`營運與部署-operations.md`（條款版本、展示環境清理改由後台逐一刪除）、`帳號與安全-auth-security.md`（條款版本說明）、`前端結構-frontend.md`（刪除帳號的前端行為）；`路線圖-roadmap.md` 狀態改為「已實作，等驗收」。`test_design_docs`、`test_readme`、`test_project_db_schema_doc` 等守門測試通過。
- **4.2 簡報對照**：已依 `deck-conflict-watch` 對照，衝突清單交使用者決定（簡報、圖解、PDF、pptx 一律沒有修改）；**未執行 `build_pptx_40.py --check`**（本機沒有 python-pptx，且本批沒有改 2.0 簡報，`FACTS` 來源沒變）。
- **4.3 變異驗證**：在 scratchpad 的專案副本上跑 39 個變異（後端 23、前端 16），**全數被測試抓到，沒有存活者、不需要補測試**；每個變異有行程逾時並於結束後逐檔還原比對，專案工作樹沒有被碰。第一輪腳本在印出前端結果時因主控台編碼（cp950）當掉，`finally` 已還原，確認副本與專案逐檔一致後改用 UTF-8 輸出、只重跑未完成的前端變異（J2–J16）；只跑 SQLite 軌，SQL Server 軌由全套涵蓋。
- **4.4 全套**：pytest **15158 通過、5 略過、0 失敗**（29 分 9 秒，含 SQL Server `project_test` 兩軌；5 個略過都是既有環境因素）；Node 全套 **1872 通過**；`openspec validate admin-delete-account --strict` 通過、`openspec validate --specs` 99 通過；`alembic upgrade head`（SQLite，到 `0023`）後 `alembic check` 沒有差異。**第一次全套抓到一個我造成的測試相撞**（批 2 在 `World` 夾具新增 6 號玩家，`tests/test_admin_lockout_api.py` 在同一夾具再加 6 至 8 號玩家而信箱重複；批 2 的守門子集不含該檔所以當時沒發現），已把該測試新增的玩家改為 7 至 9 號，修正後重跑完整全套通過。
- **未驗證**：使用者本人在真機上對確認視窗警告文字與條款那一句的驗收；展示環境（尚未部署）；真實 SQL Server `project`（沒有結構變更，不需要升級）；`build_pptx_40.py --check`。

## Workflow follow-up

- 使用者驗收（含 Modal 的警告文字與條款那一句）通過後才封存；封存後更新 `docs/路線圖-roadmap.md`。
- 部署：先要使用者重新 `aws login`，以提交短碼部署（沒有遷移）；之後才在展示環境用後台逐一刪除非管理員帳號——**每次刪除前先唯讀盤點、向使用者確認一次、先做 RDS 快照**（`aws_demo.py deploy` 會自動做快照；單獨清理時另行建立）。
- 後續（不在這個 change）：使用者清單與批次刪除（需要放寬管理員可見範圍，須先改規範）、刪除後寄通知信（email 已抹除，需要另外保存）、復原機制。
