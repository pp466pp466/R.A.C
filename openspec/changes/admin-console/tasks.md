# Tasks

## 1. 稽核資料表與 Repository

- [ ] 1.1 新增 `app/models/admin_audit_log.py`（`AdminAuditLog` 與固定的動作列舉）與 migration `0007`（只新增 `admin_audit_logs` 與兩個索引；`admin_user_id` 外鍵 `SET NULL`、目標只存對外識別碼、不設外鍵），模型加入 `app/models/__init__.py`。驗證：`tests/test_migrations.py` 新增 0007 的升降級測試（只多一張表、舊資料不變）、「遷移後的結構與模型無差異」在 SQLite 與 SQL Server 皆通過，`tests/architecture/test_user_foreign_keys.py` 通過
- [ ] 1.2 新增 `app/repositories/admin_audit_repository.py`：`add`（動作不在列舉就拒絕、原因超過 200 字拒絕、細節只接受字串與整數）與 `list`（由新到舊、分頁、帶出管理員的顯示名稱而不帶 email）。驗證：`tests/test_admin_audit_repository.py` 涵蓋列舉外的動作、長度邊界、管理員被刪除後紀錄保留且管理員為空、以反射斷言公開方法只有新增與讀取、SQLite 與 SQL Server 皆通過

## 2. 管理員檢查、重新輸入密碼與事件

- [ ] 2.0 把「被擋次數與最近一次時間 到 客服對話暫停截止時間」抽成 `quota_policy` 的純函式，`UsageService.suspended_until` 改用它（行為不變）。驗證：`tests/test_quota_policy.py` 新增純函式的邊界測試（剛好 3 次、不足、期滿、期滿後再被擋），`tests/test_usage_service.py` 與 `tests/test_chat_usage_limits.py` 原有的暫停測試全數仍通過
- [ ] 2.1 `AccessPolicy.require_admin(user)`：不是 `admin` 且 `active` 就記錄 `admin.access_denied`（只含內部編號）並拋出 `NotFound`；新增 `app/observability/admin_events.py`（`admin.access_denied`、`admin.reauth_failed`、`admin.action`，原因為固定列舉、列舉外的值立刻出錯）。驗證：政策測試涵蓋管理員、一般使用者、vip 一般使用者、停權的管理員，事件測試涵蓋欄位只在白名單內、不含 email 與文字、列舉外的值被拒絕
- [ ] 2.2 `AuthService.confirm_password(current, password)`：沒有密碼雜湊回 `no_password`、帳號被鎖定或雜湊不符時計入失敗並拋出指出密碼欄位的 `ValidationError`、成功時不重設失敗計數、不洩漏該帳號是否被鎖定。驗證：`tests/test_auth_confirm_password.py` 涵蓋缺少、錯誤、連續 5 次後鎖定 15 分鐘（含鎖定中輸入正確的密碼）、成功不重設計數、只靠 Google 的管理員

## 3. 後台專用的 Repository

- [ ] 3.1 `app/repositories/admin_repository.py` 的使用者部分：以信箱（已正規化的規範形式）與對外識別碼取得使用者、條件式停權（同時 `token_version` 加 1）、解除、調整等級，全部用單一條件式 UPDATE 並回傳是否真的寫入。驗證：`tests/test_admin_repository_users.py` 涵蓋狀態條件不符時不寫入、兩個連線並行停權只有一個成功（`concurrent_engine`）、`token_version` 確實加 1、SQLite 與 SQL Server 皆通過
- [ ] 3.2 `AdminRepository` 的彙總部分：各狀態與各等級的使用者人數、某個台灣日的（token 總數、呼叫次數、計入額度輪數、被擋次數）、今日每位被擋使用者的（被擋次數、最近一次時間）。驗證：`tests/test_admin_repository_stats.py` 涵蓋空資料、空值 token 以 0 計、日界前後一秒、摘要被擋與其他使用者的列的歸屬、被擋使用者的分組（只含今日、只含對話呼叫）、SQLite 與 SQL Server 皆通過
- [ ] 3.3 `AdminRepository` 的內容部分：意見回饋清單、被標記清單、「是否可讀」的兩個 `EXISTS` 查詢、讀取訊息（最多 200 並標示是否還有）、刪除對話（先清帳本的對話參照再刪、訊息連帶刪除）、14 天內有原文的驗證失敗列。驗證：`tests/test_admin_repository_content.py` 涵蓋算 `complaint_feedback` 與 `human_handoff`、不算只有 `unknown` 的對話、摘要被擋不算、對話被刪後離開被標記清單、不符資格的對話讀不到、第 14 天邊界、分頁、清單不含內容欄位、SQLite 與 SQL Server 皆通過

## 4. AdminService

- [ ] 4.1 `app/services/admin_service.py` 的骨架、總覽（含今日警告三個計數）與稽核檢視；新增錯誤 `AdminTargetForbidden`（403 `admin_target_forbidden`），並新增兩個架構測試：每個公開方法第一個陳述式是 `require_admin`；後台 Repository 只被 `admin_service.py` 與 `registry.py` 匯入。驗證：`tests/architecture/test_admin_boundaries.py` 通過，且自我驗證（餵它一段故意違規的原始碼必須失敗並指出方法或檔案）；總覽測試涵蓋比例計算、近 7 日恰 7 筆、警告計數（兩位被擋、一位暫停）、沒有個別使用者的資料與年齡區間
- [ ] 4.2 使用者管理：查詢（寫 `user.lookup`，細節註明途徑為信箱或識別碼）、最小資料（不含生日、年齡區間、年齡群組、雜湊）、停權、解除（依信箱驗證設定回到 `active` 或 `pending_verification`）、調整等級，四個危險操作先重新輸入密碼、失敗時寫 `reauth.failed` 並以 `persist_on_error` 保存、只能操作一般使用者。驗證：`tests/test_admin_service_users.py` 涵蓋每個前置條件與每個拒絕（管理員目標、自己、`deletion_pending`、已停權、同等級、不合法等級、缺少或過長的原因、密碼錯誤與缺少）、稽核內容不含 email 與原因以外的文字、動作失敗時稽核一併不存在
- [ ] 4.3 內容檢視：兩類清單（被標記清單每筆帶該使用者今日被擋次數與暫停截止時間）、讀取全文（先寫 `conversation.read` 再回傳）、刪除對話（寫 `conversation.delete` 含原因）、失敗原文（寫 `failures.read`）。驗證：`tests/test_admin_service_content.py` 涵蓋警告輕重的欄位（被擋 3 次者顯示暫停截止時間）、不符資格的對話得到與不存在相同的 404 且沒有稽核、稽核寫入失敗時不回傳內容、刪除後帳本筆數不變、稽核沒有任何訊息文字
- [ ] 4.4 `registry.py` 組裝（`get_admin_service`，注入政策、`AuthService.confirm_password`、`keep_changes_on_error`、年齡區間人數彙總（沿用 `AgeStatsService`）、設定的用量上限與信箱驗證設定、時鐘）。驗證：`pytest tests/architecture` 通過

## 5. API 與頁面

- [ ] 5.1 `app/controllers/admin_api_controller.py`（`/api/admin`：overview、users/lookup、users/<id>、suspend、unsuspend、tier、feedback、flagged、conversations/<id>、conversations/<id>/delete、failures、audit）、`request_helpers` 的分頁解析、`/api/admin/` 加入 no-store 前綴、`app/__init__.py` 註冊。驗證：`tests/test_admin_api.py` 對每個端點檢查未登入 401、一般使用者 404（與不存在的路徑逐字相同）、管理員成功、沒有 CSRF 權杖的寫入 403 且不改變資料、回應含 no-store、不存在 `PUT/PATCH/DELETE` 稽核端點與使用者清單端點
- [ ] 5.2 端到端整合：以假供應商走完「使用者被擋三次 → 出現在被標記清單 → 管理員讀取（有稽核）→ 停權（登入狀態立即失效、再登入得到帳號已停用）→ 解除 → 調整等級（額度立即改變）→ 刪除對話」，以及「管理員以錯誤的密碼嘗試 → 400 但 `reauth.failed` 仍保存 → 連續 5 次鎖定」。驗證：`tests/test_admin_flow.py` 通過，並以日誌掃描證明整段流程沒有 email、訊息文字與原因文字
- [ ] 5.3 `app/controllers/admin_page_controller.py` 與 `templates/admin/console.html` 的外殼與初始狀態（只有管理員的顯示名稱）。驗證：`tests/test_admin_page.py`：未登入導向登入並保留 `next=/admin`、一般使用者 404、管理員 200 且 no-store；`tests/architecture/test_frontend_rules.py` 掃描新範本與腳本通過（無 CDN、無 `v-html`、無 Bootstrap 的 JavaScript）

## 6. 前端

- [ ] 6.1 `app/static/js/admin/admin-format.js`（台灣時間的日期顯示、狀態與等級與動作的中文標籤、用量比例的百分比與警示等級、未知值的退路）。驗證：`tests/js/admin-format.test.js` 涵蓋每個列舉值都有標籤、日界、比例邊界（0、0.8、1、超過 1）、未知值不丟例外
- [ ] 6.2 `app/static/js/admin/admin.js` 與 `app/static/css/admin.css`：六個分頁（總覽、被標記對話、意見回饋、使用者、模型失敗、稽核；總覽最上面是可點擊的「今日警告」，被標記對話每列有「看對話」與「看使用者」，使用者分頁只有標明「供客服反應時使用」的信箱查詢區塊）、三個使用者操作、確認用的 Modal（需要時顯示原因欄位、密碼欄位送出或取消後立即清空、錯誤顯示在密碼欄位）、對話全文 Modal（純文字）、分頁載入、Toast；沿用 `v-if`／`v-show`、不使用 Bootstrap 的 JavaScript 與 `v-html`，色彩符合規範。驗證：`node --test tests/js` 通過、架構掃描通過
- [ ] 6.3 瀏覽器驗收（Edge，桌面與 360px）：以假供應商與預先塞好的資料啟動伺服器，走過每個分頁與每個操作（含「從總覽的今日警告進到被標記清單，再讀對話、看使用者、停權或刪除」的主要流程、密碼錯誤、成功、使用者被停權後無法再登入）、一般使用者開啟 `/admin` 得到 404、對話內容含 `<script>` 時仍是純文字，逐張檢視截圖。驗證：驗收腳本全數通過、截圖已檢視、缺陷已修正

## 7. 升級、驗收與文件

- [ ] 7.1 本機開發資料庫 `project`：備份（`COPY_ONLY, CHECKSUM`，`RESTORE VERIFYONLY` 通過）後 `alembic upgrade head` 到 `0007`，確認資料表與外鍵動作、`/readyz` 回 200、既有資料筆數不變。驗證：`alembic current` 為 `0007_admin_audit_logs`，外鍵 `admin_audit_logs.admin_user_id → users.id` 為 `SET NULL`
- [ ] 7.2 全部測試通過（`pytest`、`node --test tests/js`），README 更新：`/api/admin/*` 端點表、後台章節（可讀範圍、危險操作、稽核、已知限制）、驗收紀錄（測試數、瀏覽器驗收、資料庫升級）。驗證：`pytest tests/test_readme.py` 通過，端點表與實際註冊的路由完全一致
- [ ] 7.3 更新 docs/ 對應的設計文件（對照表見 docs/README.md）：`帳號與安全-auth-security.md`（管理員檢查、後台的可讀範圍、重新輸入密碼）、`資料模型-data-model.md`（`admin_audit_logs` 與 migration `0007`）、`系統架構-architecture.md`（後台專用 Repository 的分層規則）、`前端結構-frontend.md`（`/admin` 的模組）、`營運與部署-operations.md`（後台事件、稽核的保存期限狀態）、`客服對話-chat-service.md`（被標記與意見回饋如何被管理員看到）、`路線圖-roadmap.md`（C-4b 的狀態）；並讓 `tests/test_design_docs.py` 通過
