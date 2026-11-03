# Design

## Context

動機與範圍見 proposal.md。規範（AGENTS.md 第 3、8、9、10、11 節與 report.md 的 U-12 至 U-19）已先修訂，本 change 讓程式追上。現況（已查證）：

- `app/security/session.py` 的 `AbsoluteExpirySessionInterface` 每次寫 cookie 時固定「現在加 7 天」，`SESSION_REFRESH_EACH_REQUEST` 為假，`PERMANENT_SESSION_LIFETIME` 為 7 天；`current_user()` 惰性讀取，所以 `/healthz` 與靜態檔不碰資料庫。
- 「受限登入狀態」目前只有「等待刪除」：`CurrentUser.awaiting_deletion`，`require_login(allow_pending_deletion=…)` 與 `login_required_page(allow_pending_deletion=…)` 預設拒絕，`tests/test_restricted_session.py` 掃描 `app.url_map` 證明。
- `GoogleSignInService.begin()` 接受條款旗標加生日與區間，通過 `validate_age_input` 後放進交易 cookie；新帳號建立時一併寫入年齡。沒有帶年齡的交易在回呼時，沒有帳號者被拒（`age_required`），已有帳號者照樣登入（後端其實已支援回訪者不帶任何欄位）。
- `login.js` 的 `signInWithGoogle` 一律要求條款、生日、區間三項才送出；`/login` 與 `/register` 已登入時導向 `/account`；`/`（`page_controller.home`）沒有導向；`DEFAULT_AFTER_LOGIN` 是 `/account`。
- 密碼鎖定（`users.failed_login_count`、`locked_until`，5 次、15 分鐘，到期自動解除）已存在，由 `AuthService` 管理；`alert_rules` 的「登入失敗激增」以 `count_locked(now)` 計。
- 「錯誤時仍保留變更」已有機制（`keep_changes_on_error`，`AuthService._persist_failures` 與 `AdminService` 的 `persist_on_error` 使用）；Google 回呼拒絕時回 302，交易照常提交。
- 稽核表 `admin_audit_logs.admin_user_id` 可為空（`ON DELETE SET NULL`），動作是固定列舉；`AdminService` 對高風險操作要求重新輸入密碼並在失敗時記錄 `reauth.failed`。
- 排程指令由 `app/cli.py` 的 `SCHEDULED_COMMANDS` 單一來源產生，`scripts/launch_plan.py` 與 `tests/test_scheduled_commands.py` 依它建立與檢查。
- 既有規格與規範的衝突：`age-confirmation` 規格寫「對照不一致不得累計失敗次數」，而 AGENTS.md 把補填時的不一致列為鎖定計次的來源。**使用者已決定計入（照 AGENTS.md）**，所以本 change 修改該規格句子，但註冊（密碼註冊，仍存在到 change ②）的不一致維持不留後果。

## Goals / Non-Goals

**Goals:**
- 登入畫面只剩 Google 與條款勾選；回訪者一鍵登入；年齡確認移到登入之後且有 24 小時上限。
- 登入有效期改為閒置 30 天續期，cookie 內容與撤銷機制不變。
- 受限登入狀態一般化為三種並維持「預設拒絕、掃描證明」。
- 帳號自動鎖定、申訴、管理員解除，全程可稽核且不擴大管理員可讀取的對話範圍。
- 管理員 Google 加密碼的二階段登入與忘記密碼。

**Non-Goals:**
- 移除玩家端的密碼註冊／登入／忘記密碼／信箱驗證與 `EMAIL_VERIFICATION_REQUIRED`（change ②）。本 change 結束時 `/api/auth/register`、`/api/auth/login` 仍可呼叫，只是沒有頁面使用；`/api/auth/login` 對管理員一律失敗。
- 入口層 IP 限流（使用者先不做）。對沒有帳號的 Google 身分與無法辨識身分的失敗，只記日誌，不鎖定。
- RPG 的選擇劇本首頁（預設頁面的常數留好切換點）。
- 絕對上限的登入期限（使用者明確不要）。
- 重新徵求既有使用者對新版條款的同意。

## Decisions

### D1：滑動 session 用「有碰過才重簽」的 session 介面

以 `SlidingSessionInterface` 取代 `AbsoluteExpirySessionInterface`：`PERMANENT_SESSION_LIFETIME` 改為 30 天（Flask 讀取 cookie 時用它當簽章 `max_age`，所以簽章時間戳就是「最後一次使用」）；`should_set_cookie` 在「session 非空且本次請求碰過它（`session.accessed`）」時回傳真，因此每個辨識出登入者的回應都以新時間戳重簽並設定新的到期時間，而 `/healthz`、靜態檔、匿名頁面（session 為空）不重發。`get_expiration_time` 仍不依賴 `permanent` 旗標，cookie 內容仍只有 `uid`、`tv`（不加簽發時間，也就不需要改 AGENTS.md 的內容規定）。取捨：每個登入者的請求都多一個 `Set-Cookie`，換來沒有絕對上限的續期；被竊取的 cookie 持續使用就持續有效，撤銷靠 `token_version`（已接受的風險）。

### D2：受限登入狀態用單一列舉，不再是布林

`CurrentUser` 新增 `restriction`（`None`｜`locked`｜`age_pending`｜`deletion_pending`），優先序為被鎖定 > 待確認年齡 > 等待刪除；`awaiting_deletion` 保留給既有程式。`require_login(allow=…)`／`login_required_page(allow=…)` 接受允許的受限狀態集合（舊的 `allow_pending_deletion=True` 等同 `allow={deletion_pending}`，保留一個版本的相容寫法再移除）。拒絕時 API 拋出該狀態對應的錯誤（`AccountDeletionPending`、`AgeConfirmationRequired`、新的 `AccountLocked`，皆為 `Forbidden` 子類別、各有固定代碼），頁面導向該狀態的頁面（`/account`、`/age`、`/appeal`）。待確認年齡的判斷是「角色為 `user` 且沒有生日與區間」，管理員不適用。`tests/test_restricted_session.py` 擴充：對三種狀態各建立登入者，掃描全部需要登入的端點，只有各自的允許清單通過。

### D3：資料模型（一個 migration，expand 階段）

- `users.age_deadline_at`（可為空，索引）：首次 Google 登入建立玩家帳號時設為建立時間加 24 小時；補填成功時清空。既有帳號為空值 → 被新閘門擋住但**不會被逾時清除**（避免部署當下把所有沒有生日的舊帳號刪光）。
- `users.locked_at`（可為空，索引）與 `users.lock_reason`（可為空，固定列舉：`identity_rejected`｜`rate_limited`）：玩家帳號鎖定狀態。與既有的密碼鎖定（`locked_until`）分開：兩者語意不同（到期自動解除 vs 只有管理員解除），告警把兩者合計。
- `lock_strikes`：`id`、`user_id`（FK，`ON DELETE CASCADE`）、`kind`（`identity_rejected`｜`rate_limited`）、`created_at`；索引 `(user_id, created_at)`。只存編號、類別與時間。
- `lock_appeals`：`id`、`public_id`（UUID）、`user_id`（FK，`CASCADE`）、`content`（`UnicodeText`）、`created_at`、`handled_at`（可為空）；索引 `(user_id, created_at)`。
- 新程式相容舊結構、舊程式相容新結構（只新增可為空的欄位與新表）；`tests/architecture/test_user_foreign_keys.py` 與 `test_account_erasure_coverage.py` 納入兩張新表。SQLite 與 SQL Server 雙軌，全部走 ORM。

### D4：計次與鎖定放在 `AccountLockService`，錯誤時仍保留

`AccountLockService.register_strike(user_id, kind, now)`：寫一筆 `lock_strikes`、計算視窗內的筆數（身分被拒類別 15 分鐘 5 次，限流類別 15 分鐘 10 次）、達門檻時以條件式 UPDATE（`WHERE locked_at IS NULL`）鎖定，`rowcount == 1` 才記錄 `account.lock` 稽核（管理員欄位為空）與事件，所以並行只鎖一次。已鎖定的帳號不再計次。呼叫點：①Google 回呼的拒絕（只有 `user_id` 已知者：帳號停用、已綁定另一個身分的衝突）——回應是 302，交易照常提交；②補填年齡的 `age_mismatch`（400）——呼叫 `keep_changes_on_error()`；③`UsageService._check_rate` 在拋出 `RateLimited` 之前——同樣 `keep_changes_on_error()`。找不到帳號（無法辨識身分、沒有帳號的 Google 身分）不呼叫。期限清除把 7 天前的計次納入既有的 `purge-expired-data`。

### D5：年齡移到登入之後

`GoogleSignInService.begin()` 只收條款旗標（請求帶的生日與區間忽略、不放進交易）；`complete()` 忽略交易裡舊版留下的 `b`、`r`。`_create_account` 不寫年齡，設定 `age_deadline_at`，結果仍是 `registered`；Controller 依 `restriction` 決定導向（待確認年齡 → `/age`，並保留登入前的 `next` 到補填完成後使用）。`AuthService.verify_age` 擴充：未滿 13 歲的玩家 → 以 `AccountErasureService` 抹除（條件式刪除：角色非管理員），清除 session，回 400 `age_not_eligible`；管理員提交未滿 13 歲 → 只拒絕、不抹除；`age_mismatch` → 回通用訊息並登記鎖定計次；成功 → 寫入年齡資料並清空 `age_deadline_at`。年齡規則、對照與政策版本沿用 `age_policy`（不內嵌任何數字）。

### D6：逾時清除是新的排程指令

`purge-unconfirmed-accounts`（`SCHEDULED_COMMANDS` 新增，cron `0 * * * *`，時區 Asia/Taipei）：Repository 的條件式查詢 `role='user' AND birth_date IS NULL AND age_deadline_at IS NOT NULL AND age_deadline_at <= now`，逐一以 `AccountErasureService` 抹除（刪除當下再確認一次條件）、分批、`--dry-run` 只計數；輸出只有筆數。沿用 `AccountRetentionService` 的批次與事件樣式。因為排程由單一來源產生，`launch_plan` 的步驟、`tests/test_scheduled_commands.py` 與營運文件的排程表自動納入。

### D7：管理員二階段登入

Google 回呼成功且帳號角色為 `admin` 時，不呼叫 `login_user`，改核發簽章的短效 cookie（`SignedCookies`，獨立 salt、10 分鐘，內容 `uid` 與 `tv`，沿用 OAuth 交易 cookie 的寫法）並導向 `/login/password`。`POST /api/auth/admin-password` 讀取該 cookie、以既有的 `AuthService` 密碼驗證與失敗鎖定（5 次、15 分鐘）驗證，成功才 `login_user` 並清掉中間 cookie；`POST /api/auth/admin-password/forgot` 只接受持有中間 cookie 的請求，呼叫既有的重設密碼寄信流程（改為不受 `EMAIL_VERIFICATION_REQUIRED` 控制，只需要寄信設定；重設後 `token_version` 加 1，中間 cookie 與既有 session 同時失效）。`POST /api/auth/login`（密碼）對管理員一律走與密碼錯誤相同的路徑（燒掉一次雜湊、通用訊息、不計失敗）。中間狀態 cookie 不被 `current_user()` 辨識，所以所有需要登入的端點都是 401。影響：Google 未設定時管理員無法登入（本機開發需要 OAuth 用戶端，Google 允許 `http://localhost` 的重新導向 URI；測試用假的 Google 供應商）。

### D8：申訴與管理員檢視

申訴 API（`POST /api/appeals`、`GET /api/appeals/mine`）需要登入、只對被鎖定的玩家有效（其他人 404，與不存在相同），輸入清理沿用 `input_cleaning`（NFC、控制字元、1000 字），頻率用 `lock_appeals` 的時間計（每小時 3 則、未處理 1 則）。管理員端：`GET /api/admin/locked`（被鎖定帳號：對外識別碼、顯示名稱、鎖定時間、原因類別）、`GET /api/admin/appeals`（只回傳「目前被鎖定的帳號」的申訴，寫 `appeal.read`）、`POST /api/admin/users/<id>/unlock`（重新輸入密碼，成功時清 `locked_at`、`lock_reason`、該帳號的計次、將待處理申訴標為已處理，寫 `account.unlock`；失敗寫 `reauth.failed`）。`DANGEROUS_ACTIONS` 納入 `account.unlock`。後台介面新增「被鎖定的帳號」與「申訴」兩個區塊。申訴不進任何日誌或稽核內容（只有字數）。

### D9：預設頁面與首頁導向

`DEFAULT_AFTER_LOGIN` 改為 `/cs`；新增一個函式 `landing_for(user)` 回傳登入者該去的位置（受限狀態各自的頁面，否則預設頁面），`/`、`/login` 與 Google 回呼共用。`/register` 改為 302 導向 `/login`。`tests/support/client.py` 的 `csrf_token()` 目前對 `/` 發 GET 取得 CSRF cookie——已登入的測試 client 會被導向而沒有 cookie，所以改取一個永遠公開的頁面（`/terms`）。

### D10：告警只改名稱與計數來源

告警代碼 `login_lockouts` 不變（`alert_states` 的鍵，避免資料遷移），顯示名稱與信件文案改為「帳號鎖定激增」；`UserRepository.count_locked(now)` 改為計「`locked_until > now` 或 `locked_at` 不為空」的帳號（同一帳號只算一個）。日誌／稽核事件：`account.lock`、`account.unlock`、`appeal.submit`、`appeal.read`（稽核）與對應的結構化事件，欄位限於事件名稱、內部編號與固定列舉。

### D11：法律頁面

隱私權政策與服務條款頁面加上：先蒐集 Google 提供的信箱與名稱、年齡確認 24 小時內未完成或未滿 13 歲時立即刪除、帳號鎖定與申訴的資料處理；`CURRENT_TERMS_VERSION` 提升。不重新徵求既有使用者的同意（已知限制，列入法務覆核關卡）。

## Risks / Trade-offs

- **受限狀態漏擋** → 一般化為單一列舉與預設拒絕，三種狀態各由掃描測試證明；新增端點不需要記得檢查。
- **計次在錯誤回應下被回復而漏記** → D4 的三個呼叫點各有「回應 4xx 後資料庫仍有計次」的測試。
- **舊帳號被新閘門擋住** → 在 `age_deadline_at` 為空時只擋、不清除；部署前沒有真實會員（開發與預備資料），風險低；上線前關卡不變。
- **沒有絕對上限的登入** → 使用者已接受；撤銷靠 `token_version`、帳號停權與管理員的鎖定。
- **未滿 13 歲者可以立刻重新用 Google 登入再填假資料** → 自行聲明的既有限制，與先前相同；抹除只保證不保存其資料。
- **鎖定可能被用來誤傷**：攻擊者無法替別人的 Google 身分製造「身分被拒」計次（需要通過 Google 驗證才辨識得出帳號），但可疑的重複失敗 `account_disabled` 可能累積；被誤鎖者可申訴。洗版鎖定只由帳號本人的請求造成。
- **管理員 Google 加密碼讓本機開發更麻煩** → 測試用假供應商；本機手動測試需要 localhost 的 OAuth 用戶端（已在 README 說明）。
- **既有測試與驗收腳本大量使用密碼註冊／管理員密碼登入** → 測試輔助改成直接寫入登入 session 或假的 Google 供應商；冒煙腳本、Locust 情境與容器驗收腳本的建立帳號方式要隨 change ② 一起改（本 change 先讓它們在管理員以外維持可用）。

## Migration Plan

1. 新程式碼相容新舊結構；部署順序仍是：備份 → `alembic upgrade head`（新增欄位與兩張表）→ 部署 → 驗證。
2. 部署後第一次執行 `purge-unconfirmed-accounts --dry-run` 確認筆數為 0（舊帳號沒有期限）。
3. 回復：還原到前一個版本即可，新欄位與新表保留不會影響舊程式；若要移除，另開 contract 階段。

## Open Questions

- 無會改變建置內容的未決問題。已決定並寫入：年齡不一致計入鎖定（使用者確認）、申訴與鎖定的預設門檻（使用者確認「按照建議」）。待法務：新版條款是否需要重新徵求既有使用者同意（本 change 不做）。
