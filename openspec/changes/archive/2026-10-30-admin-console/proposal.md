# Proposal

## Why

C-4a 之後，客服有了額度與暫停，但**沒有任何人能處理結果**：使用者因反覆觸發安全過濾被暫停、意見回饋對話堆在資料庫裡沒人看得到、需要停權或調整會員等級（目前沒有付款機制，等級只能由人手動調整）時只能直接改資料庫。AGENTS.md 第 10 節已經規定了管理員後台——獨立的 `/admin` 與 `/api/admin/*`、只有管理員可存取、功能含停權、查看用量與刪除違規內容、預設只能看統計資料與兩類對話、高風險操作須重新輸入密碼、每個操作寫入 `admin_audit_logs`——但程式裡只有「首位管理員」的建立指令。

這是原 C-4（usage-safety-ops）拆成三個 change 之後的第二個：C-4a `usage-limits`（已封存）、**C-4b `admin-console`（本 change）**、C-4c `privacy-lifecycle`（刪除帳號、匯出、保留期限清理、告警）。後台是高權限攻擊面，所以重點不是功能多，而是**可讀範圍小、每個操作可追溯、危險操作要重新驗證**。

## What Changes

- **後台頁面與 API**：新增 `/admin`（頁面外殼，Vue 3 全域變數模式、自製 Modal 與 Toast）與 `/api/admin/*`（一律回 JSON）。只有角色為 `admin` 且帳號啟用的使用者可存取；**非管理員一律得到 404**（與資源不存在相同，不洩漏後台存在），未登入 API 回 401、頁面導向登入。回應一律 `Cache-Control: no-store`。
- **總覽與用量**：使用者數（依狀態與會員等級）、今日與近 7 日的 token、呼叫次數、對話輪數、被擋次數，全站用量相對於上限的比例（沿用 C-4a 的 `USAGE_DAILY_*`）、各年齡區間人數（只有彙總）。另有**今日警告**的三個計數（今日被安全過濾擋下的呼叫數、被擋的使用者數、目前客服對話暫停中的使用者數）。**總覽不含任何個別使用者的資料**。
- **找使用者的方式（警告優先）**：管理員平常**只透過違反條例的警告找使用者**——總覽的「今日警告」連到「被標記對話」清單，每一筆帶有該使用者今日被擋的次數與是否暫停中，由此進入該對話與該使用者，決定要不要刪除內容或停權。**以信箱精確查詢是次要入口，只在客服反應時使用**（例如使用者來信要求調整等級），介面上標明用途，且每次查詢都寫稽核（註明是以信箱還是以識別碼）；沒有瀏覽全部使用者的清單。
- **單一使用者的詳情**：對外識別碼、顯示名稱、email、等級、狀態、建立與最後登入時間，以及今日用量。**管理員看不到任何個別使用者的年齡資料**（生日、年齡區間，以及由它們推得的年齡群組都不給），與 AGENTS.md 第 10 節一致；年齡只有總覽的各區間人數彙總（現在 `flask age-stats` 的做法）。可**停權**（`suspended` 並使 `token_version` 加 1，既有登入立即失效）、**解除停權**（回到 `active`，信箱驗證開啟而尚未驗證者回 `pending_verification`）、**調整會員等級**。管理員帳號不能被後台操作。
- **重新輸入密碼**：停權、解除停權、調整等級、刪除對話都必須在請求中帶管理員自己的密碼；錯誤時沿用既有的失敗計數與 15 分鐘鎖定，並寫稽核（`reauth.failed`）。沒有設定密碼的管理員不能執行這些操作。
- **內容檢視**：只開放兩類對話——**意見回饋**（至少有一則助理回覆的意圖是 `complaint_feedback` 或 `human_handoff`，也就是客服告訴使用者「管理員會看到」的兩種情況；只有 `unknown` 的對話不算）與**被內容安全過濾標記**（帳本中有該對話被擋的紀錄）。清單只有中繼資料（對外識別碼、次數、時間），**不含訊息內容**；點進去讀對話全文才寫稽核（`conversation.read`）。不屬於這兩類的對話一律 404。另可查看**模型失敗的原文**（`error_detail`，AGENTS.md 第 5 節「僅管理員可讀」），讀取寫稽核（`failures.read`）。
- **刪除違規內容**：刪除整個對話（單則刪除會破壞序號與摘要），並寫稽核（含原因）。
- **稽核**：新增 `admin_audit_logs`（migration `0007`，只新增資料表）。固定列舉的動作；記錄管理員、目標的對外識別碼、管理員填寫的原因與列舉型的細節（例如調整前後的等級）；**不記錄 email、訊息內容或密碼**；寫入與動作在同一個交易；Repository 沒有修改或刪除的方法。管理員可在後台查看稽核紀錄。
- **分層與測試守則**：後台專用的 Repository 只能被 `AdminService` 使用，`AdminService` 的每個公開方法第一件事是 `AccessPolicy.require_admin`，兩者都以架構測試（AST）守住。
- **不在範圍**：待審的性格原型草案（RPG 的 RC-5）；`error_detail` 14 天清除與稽核 24 個月清除、刪除帳號、匯出、告警（C-4c）；解除「客服對話暫停」（它由帳本推得、1 小時自然到期）；調整額度數值、角色變更、來源 IP 限制（AGENTS.md 第 10 節明定 v1 不限制）、雙因素驗證；寄信通知被停權者。

## Capabilities

### New Capabilities
- `admin-console`: `/admin` 與 `/api/admin/*` 的存取規則（只有管理員、非管理員 404、no-store）、總覽與用量統計（只有彙總）、頁面本身。
- `admin-user-management`: 以信箱查詢使用者、停權與解除、調整會員等級、可操作的目標、重新輸入密碼與稽核的搭配。
- `admin-content-review`: 意見回饋與被標記對話的清單與全文檢視、刪除違規對話、模型失敗原文的檢視；可讀範圍與稽核。
- `admin-audit-log`: `admin_audit_logs` 的內容、固定的動作列舉、不可變、與動作同一交易、後台檢視。

### Modified Capabilities
- `access-control`: 新增需求——管理員判斷的集中入口 `require_admin`（非管理員 404），管理員存取他人資料只能走專用路徑並寫稽核。
- `application-layering`: 新增需求——後台專用的 Repository 只能由 `AdminService` 使用，且 `AdminService` 的每個公開方法先檢查管理員。
- `service-observability`: 新增需求——後台事件（重新驗證失敗、停權等）只輸出固定列舉與內部編號，不含 email、訊息或原因文字。

## Impact

- **新增程式**：`app/models/admin_audit_log.py`、`app/repositories/admin_repository.py`、`admin_audit_repository.py`、`app/services/admin_service.py`、`app/observability/admin_events.py`、`app/controllers/admin_api_controller.py`、`admin_page_controller.py`、`app/templates/admin/console.html`、`app/static/js/admin/`、`app/static/css/admin.css`、migration `0007`。
- **修改程式**：`app/services/access_policy.py`（`require_admin`）、`auth_service.py`（公開的 `confirm_password`）、`registry.py`、`app/__init__.py`（註冊藍圖）、`app/security/cache_headers.py`（`/api/admin/` 加入 no-store）、`app/observability/json_logging.py`（若需要新欄位）、README 與 `docs/` 設計文件。
- **資料庫**：新增一張資料表 `admin_audit_logs`（`admin_user_id` 外鍵為 `SET NULL`，管理員被刪除時紀錄保留；目標只存對外識別碼、不設外鍵——兩條指向 `users` 的 `SET NULL` 在 SQL Server 會被拒絕，與 `llm_call_logs.conversation_id` 同一個理由）。符合 expand/contract。
- **相依套件**：不新增。
- **既有行為**：沒有改變；一般使用者與客服流程不受影響。唯一的共用變更是 `AuthService` 多一個公開的密碼確認方法（沿用既有的雜湊驗證與失敗鎖定）。
- **外部**：不涉及任何模型呼叫。
- **上線限制**：完成後仍缺 C-4c（刪除帳號、匯出、保留期限清理、告警）；對外開放前還須通過 C-5 的上線前關卡。
