# Tasks

## 1. 年齡規則與資料結構

- [x] 1.1 新增 `app/services/age_policy.py`：不可變的 `AgePolicy`（最低註冊年齡 13、最低使用年齡 13、成年門檻 18、區間選項 `under_13`／`13_17`／`18_24`／`25_34`／`35_44`／`45_54`／`55_plus` 與繁體中文標籤、政策版本 `2026-10-28-birthdate-range`）、預設實例、`age_on(birth_date, today)`、`range_for_age`、`age_group`；`create_app` 接受 `age_policy` 參數並放進 `app.extensions`，`get_age_policy()` 供取用；`CURRENT_AGE_POLICY_VERSION` 改由它提供。驗證：新增 `tests/test_age_policy.py`——生日當天與前一天（12/13、17/18 歲邊界）、閏日（2 月 29 日出生）、各區間邊界、Asia/Taipei 與 UTC 日期不同的時刻、自訂政策（最低年齡 18）生效、`create_app(age_policy=…)` 注入。
- [x] 1.2 `app/models/user.py` 新增 `birth_date`（`Date`）、`age_range`（`Unicode(20)`）、`is_adult_at_confirmation`（`Boolean`），皆可為空；新增 `DeletionTombstone` 模型（`id`、`user_id` 整數且**不設外鍵**、`deleted_at`、`reason`）並註冊到 `app/models/__init__.py`；更新檔頭說明。驗證：`tests/test_user_models.py` 的可為空欄位集合與預設值更新，新增墓碑模型的欄位與「沒有外鍵」的測試。
- [x] 1.3 新增 migration `migrations/versions/20261028_0004_users_birthdate_and_tombstones.py`（revision `0004_users_birthdate`——不超過 32 個字元，因為 `alembic_version.version_num` 在 SQL Server 只有 `VARCHAR(32)`；接在 `0003`）：`batch_alter_table` 新增三欄、建立 `deletion_tombstones`；`downgrade` 以 `recreate="never"` 只刪這三欄並刪除該資料表；不匯入應用程式、不含原生 SQL。驗證：`tests/test_migrations.py` 新增案例——只新增這三欄與一張表、欄位可為空、降級後只剩 `0003` 的結構且 `user_identities`、`auth_tokens` 的資料列都在、`alembic heads` 只有一個、與模型比對無差異；SQLite 與 SQL Server（`project_test`）都通過。
- [x] 1.4 `UserRepository` 以 `complete_age_verification_if_unset(user, birth_date, age_range, is_adult, confirmed_at, policy_version) -> bool` 取代 `confirm_adult_if_unset`（條件式 UPDATE，`WHERE id = ? AND birth_date IS NULL`，ORM 表達式）；新增 `count_by_age_range() -> dict[str, int]`（`GROUP BY`，只回計數，含未完成者）。驗證：`tests/test_user_repositories.py`——第一次寫入回 True、第二次 False 且原值不變；8 個連線同時補填只有 1 個成功；`count_by_age_range` 的計數正確且回傳內容不含任何生日；SQLite 與 SQL Server 都通過。
- [x] 1.5 更新 `docs/spec_report_v2.2.0.md` 的 users 資料表（新增三欄、`age_confirmed_at` 的新意義）並新增 `deletion_tombstones` 一節。驗證：以 Grep 找到 `birth_date`、`age_range`、`is_adult_at_confirmation` 與 `deletion_tombstones`。

## 2. 驗證、權限與註冊流程（Service 層）

- [x] 2.1 在 `age_policy.py` 實作 `validate_age_input(raw_birth_date, raw_range, today, policy)`：依序檢查缺少或格式不合法（`birth_date`、`age_range` 各自的欄位錯誤）、未滿 13 歲或區間為 `under_13`（`ValidationError`，代碼 `age_not_eligible`，欄位 `birth_date`）、年齡不在區間（`ValidationError`，代碼 `age_mismatch`，無欄位，訊息固定為「資料有誤，請重新註冊」）；通過回傳已驗證的日期與區間代碼。驗證：`tests/test_age_policy.py` 擴充——缺少、格式不合法（`2026-02-30`、未來、早於 1900、非字串）、未滿 13 歲（含區間填成年）、不一致、一致；失敗時例外的訊息與屬性不含輸入的生日。
- [x] 2.2 `app/services/errors.py` 新增 `AgeRestricted(Forbidden)`（`age_restricted`）；`app/observability/security_events.py` 新增 `auth.age_rejected.underage`、`auth.age_rejected.mismatch`（警告層級）與 `account.inactive_purged`（資訊層級）；`GOOGLE_REJECTED_AGE` 沿用。驗證：`tests/test_security_events.py` 確認新名稱已登記且層級正確、`AgeRestricted` 是 `Forbidden` 的子類別且代碼正確。
- [x] 2.3 `CurrentUser` 新增內部欄位 `birth_date`；`age_confirmed` 由它推得；`public_dict` 新增 `age_group`（`minor`／`adult`／空值，由呼叫端傳入）、仍**不含**生日、區間、時間與版本。`AccessPolicy` 注入 `AgePolicy` 與時鐘，`can_use_chat`／`require_chat_access` 依 D4 的順序判斷並拋出 `AgeConfirmationRequired` 或 `AgeRestricted`，新增 `content_ceiling(user)`；`registry.get_access_policy()` 帶入政策與時鐘。驗證：`tests/test_access_policy.py` 的矩陣改為狀態、信箱驗證、完成與否、年齡（12、13、17、18、40 歲）、角色、會員等級，只有條件齊全才回 True，管理員與 `vip` 不豁免；檢查順序（停權先於年齡）、最低使用年齡改為 18 時 15 歲回 `age_restricted`、`content_ceiling`（未成年 0／安全模式、成年 2、未完成保守值、與存檔旗標無關）；`tests/test_current_user.py` 更新。
- [x] 2.4 `AuthService.register` 以 `birth_date`、`age_range` 取代 `confirm_adult`：驗證發生在查詢信箱之前，成功時寫入生日、區間、`is_adult_at_confirmation`、確認時間、政策版本並記錄 `auth.age_confirmed.register`，失敗時只記錄 `auth.age_rejected.*`；`create_admin` 仍不寫入年齡欄位。並同步 `tests/support/age.py`（以時鐘的 Taipei 日期產生 N 歲的生日與對應區間、預設通過的 `age_fields()`）、`tests/support/auth.py` 的 `add_user(adult=…)`，以及使用舊欄位的既有測試（`test_auth_registration`、`test_auth_api`、`test_email_flows`、`test_email_flow_api`、`test_log_scan_full_flow`、`test_google_sign_in_service`）。驗證：新增案例——缺少、不一致、未滿 13 歲、各自獨立的條款錯誤、成功時全部欄位被記錄、信箱已存在時回應與成功完全相同且既有帳號的年齡欄位不被改動、**不一致時已存在與不存在的信箱得到相同的 400**、**不一致失敗後立即以同一信箱與密碼註冊成功（沒有封鎖、計數或殘留資料）**、未滿 13 歲時資料庫沒有任何新資料列、日誌沒有生日；更新後的既有測試全數通過。
- [x] 2.5 `AuthService.verify_age(current, birth_date, age_range)` 取代 `confirm_adult`：同一個驗證、`complete_age_verification_if_unset`、僅在實際寫入時記錄 `auth.age_confirmed.later`，回傳最新快照；`AuthService.profile` 以時鐘與政策算出 `age_group`。驗證：`tests/test_auth_session_service.py`——成功、各種不通過都不寫入、已完成者再次提交（即使內容不同）不改動且事件只記一次、日誌不含 email 與生日、17 歲在生日之後的 `age_group` 變為 `adult`（時鐘前進）、`is_adult_at_confirmation` 被改成矛盾的值時 `can_use_chat` 與 `content_ceiling` 不受影響。
- [x] 2.6 `GoogleSignInService`：`begin(accept_terms, birth_date, age_range)` 先驗證（失敗直接拋出，不產生授權網址）、交易鍵改為 `b`、`r`（移除 `a`）；`complete`／`_resolve` 在建立新帳號前再驗證一次，缺少或失敗走 `age_required` 並記錄 `auth.google_rejected.age`；成功時寫入全部年齡欄位並記錄 `auth.age_confirmed.google`；既有帳號登入不讀不寫。同步 `tests/test_google_sign_in_service.py`。驗證：新使用者的條款與年齡四種組合、未滿 13 歲與不一致在 `begin` 就被拒絕且沒有授權網址與任何資料、缺少 `b`／`r` 的舊交易視為未提供、既有帳號（已綁定、以信箱合併）不受影響且年齡欄位維持為空、回呼時邊界日期改變仍被重新驗證。

## 3. HTTP 介面與頁面狀態

- [x] 3.1 `auth_api_controller`：`/api/auth/register` 與 `/api/auth/google/start` 讀取 `birth_date`、`age_range`（移除 `confirm_adult`）；以 `POST /api/auth/verify-age` 取代 `/api/auth/confirm-adult`（需要登入，成功回最新帳號資料）；`/api/auth/me` 與登入回應含 `age_group`；錯誤代碼 `age_not_eligible`、`age_mismatch`、`age_restricted` 由既有的錯誤對應轉成 JSON。驗證：`tests/test_auth_api.py`、`tests/test_google_endpoints.py` 新增案例——狀態碼（400、401、403）、回應格式、`age_group` 正確、**所有 `/api/auth/*` 回應都不含生日與年齡區間**；`tests/test_csrf.py` 的「沒有被豁免」路由掃描自動涵蓋新端點。
- [x] 3.2 `auth_page_controller`：頁面初始狀態新增 `ageRules`（區間選項與標籤、最低註冊年齡、成年門檻、生日欄位的 `min`／`max`），由 `AgePolicy` 與時鐘產生；`age_required` 的提示改為「請先完成年齡確認（生日與年齡區間）」；條款與隱私權政策範本的年齡數字改由政策提供。驗證：`tests/test_auth_pages.py`——`ageRules` 的內容與政策一致、自訂政策（最低年齡 18）時頁面的選項與條款文字跟著變、帳號頁的初始狀態不含生日與區間、固定代碼集合的既有測試更新後通過。
- [x] 3.3 以測試專用端點（`tests/support/age_gate.py`）驗證閘門的 HTTP 行為。驗證：更新 `tests/test_age_gate_api.py`——尚未完成者 403 `age_confirmation_required`、補填後同一端點 200、13 至 17 歲者 200、最低使用年齡改為 18 時 15 歲者 403 `age_restricted` 而成年者不受影響、管理員與 `vip` 同樣被擋、被拒時日誌有 `auth.age_gate_denied` 與內部使用者編號且不含 email 與生日、停權者得到的是帳號狀態的拒絕。

## 4. 前端與條款

- [x] 4.1 `register.html` 與 `register.js`：以生日欄位與年齡區間下拉選單取代年齡勾選（選項來自 `ageRules`），送出帶 `birth_date`、`age_range`；Google 入口在離開本站前檢查條款、生日、區間，缺哪項就提示哪項；收到 `age_mismatch` 顯示提醒並回到註冊頁初始狀態（清空生日、區間與密碼，保留電子郵件與顯示名稱，可立即重新送出），收到 `age_not_eligible` 顯示說明並清空生日。驗證：`tests/test_auth_pages.py` 斷言頁面有生日與區間欄位、沒有年齡勾選、腳本送出新欄位並處理兩個錯誤代碼；`tests/architecture/test_frontend_rules.py` 的前端規範掃描通過（無 `v-html`、無 Bootstrap JS、無 CDN）。
- [x] 4.2 `login.html` 與 `login.js`：Google 入口以生日與區間取代年齡勾選，錯誤各自顯示在欄位旁。驗證：頁面測試斷言欄位存在與腳本行為；前端規範掃描通過。
- [x] 4.3 `account.html`、`account.js`、`auth.css`：「年齡確認」列、補填表單（沒填就按不送出請求、成功後 Toast 並更新狀態）、13 至 17 歲的安全模式提示、頁面不顯示生日與區間。驗證：頁面測試斷言各狀態的內容與「不含生日」；若有純函式邏輯則以 Node 測試涵蓋（`tests/js/`）；前端規範掃描通過。
- [x] 4.4 `terms.html`、`privacy.html`：條款載明 13 歲以上、13 至 17 歲僅限安全模式、未滿 13 歲不得註冊；隱私權政策載明蒐集生日與年齡區間、使用目的（年齡確認、內容分級、以去識別的統計方式了解用戶群）、保存期限（12 個月未登入即刪除）、不傳送至 Gemini、未滿 13 歲者的輸入不保存，並更新資料保存表；年齡數字來自 `AgePolicy`，仍標示草稿；`CURRENT_TERMS_VERSION` 提高。驗證：`tests/test_auth_pages.py`——兩頁含上述說明與草稿標示、版本等於 `CURRENT_TERMS_VERSION`、舊的「不蒐集生日、年齡或證件」已不存在、章節編號連續、範本原始檔沒有寫死的年齡數字（以自訂政策渲染後文字改變來證明）。

## 5. 閒置帳號清除與統計

- [x] 5.1 `DeletionTombstoneRepository.add` 與 `UserRepository` 的 `find_inactive_ids(cutoff)`、`delete_inactive(user_id, cutoff)`（ORM 的 `func.coalesce(last_login_at, created_at)`、排除 admin、**條件式 DELETE** 回報是否真的刪除）。驗證：`tests/test_user_repositories.py`——364 與 365 天的邊界、從未登入以建立時間計、管理員不納入、刪除時條件已變（剛登入）則不刪且回報 False、並發兩個清除只有一個刪成功；`user_identities`、`auth_tokens` 隨之刪除（SQLite 與 SQL Server）。
- [x] 5.2 新增 `AccountRetentionService` 與 `flask purge-inactive-accounts [--dry-run]`、`flask age-stats`（`app/cli.py`，沿用 `create-admin` 的 Session 建構方式）：每個被刪帳號寫入刪除紀錄並記錄 `account.inactive_purged`（只含內部編號）、輸出只有筆數；預覽模式不刪除不記事件；`age-stats` 輸出各區間與未完成人數。驗證：新增 `tests/test_cli_account_retention.py`——預覽、實際刪除、重複執行為 0、墓碑只含內部編號與時間與原因、輸出與日誌不含 email、名稱、生日（掃描）、`age-stats` 只有計數；`tests/test_log_scan_full_flow.py` 加入清除流程後的日誌掃描。
- [x] 5.3 新增架構測試 `tests/architecture/test_user_foreign_keys.py`：所有指向 `users.id` 的外鍵都必須是 `ON DELETE CASCADE` 或 `SET NULL`，否則失敗並指出資料表與欄位，提醒 C-3、C-4 新增資料表時要處理清除。驗證：目前通過；以一個暫時的、沒有串聯刪除的模型驗證測試確實會失敗（反向驗證，不留在程式裡）。

## 6. 整合驗收與文件

- [x] 6.1 更新 README：端點表（`/api/auth/verify-age` 取代 `confirm-adult`、新的請求欄位與錯誤代碼、`age_group`）、「撰寫新端點」一節的 `require_chat_access` 說明（含 `age_restricted` 與內容上限）、新指令（`purge-inactive-accounts`、`age-stats`）、管理員第一次登入須補填、生日與保存期限的說明，以及「已知落差」那一行改為已解決。驗證：`tests/test_readme.py` 的路由表一致性通過，且上述各處可由 Grep 找到。
- [x] 6.2 更新 `scripts/verify_container.py`：註冊改帶生日與區間，並新增補填、未滿 13 歲被拒絕、不一致被拒絕、`age-stats` 與 `purge-inactive-accounts --dry-run` 的檢查；於 WSL 的 Docker 重新建置並執行。驗證：容器驗收全部通過，檢查項目數與映像大小記入 README。
- [x] 6.3 Edge 無頭瀏覽器驗收（比照 `age-gate` 的做法，**逐張檢視截圖**）：註冊頁的生日與區間欄位與各自的錯誤位置、不一致後欄位被清空、未滿 13 歲的說明、登入頁 Google 入口、帳號頁補填與 13 至 17 歲的安全模式提示、360px 與 1280px 無水平捲軸、鍵盤可操作。驗證：結果與項目數寫入 README 的瀏覽器驗收紀錄，全部通過。
- [x] 6.4 完整測試：pytest（SQLite 與 SQL Server `project_test`）、Node 測試、`openspec validate age-verification-birthdate --strict`。驗證：全數通過，數字記入 README 的驗收紀錄。
- [x] 6.5 本機開發資料庫 `project` 升級到 `0004`（**需使用者同意**；先備份並以 `RESTORE VERIFYONLY` 驗證）。驗證：`alembic current` 顯示 `0004_users_birthdate`、`/readyz` 回 200、`age-stats` 可執行。
- [x] 6.6 在 [RPG.md](../../../docs/RPG.md) 的 T-10 與 R-15.4 標註此 change 已完成及其範圍（含「與 Gemini 條款不符、僅限個人測試」仍然成立）。驗證：以 Grep 在 RPG.md 找到更新後的說明。

## Workflow follow-up

- 歸檔本 change（`/openspec-archive-change age-verification-birthdate`），並驗證歸檔結果：歸檔時 `google-sign-in` 的 RENAMED 需求要在主規格中改名，主規格的「首次以 Google 註冊須確認年滿 18 歲」不應殘留。
- C-3 的任務清單須納入：每個 AI 端點先呼叫 `AccessPolicy.require_chat_access`、模型呼叫只在 `LLMProvider` 之後、`llm_call_logs` 記錄供應商名稱，並在客服與 RPG 的端點測試中各驗證一次年齡閘門。
- RPG change 須實作 `content_ceiling` 的實際套用（伺服器端的 system_instruction 與安全設定）與「玩家輸入曖昧內容而分級較低時提醒切換、不得自動調高」。
- C-4 須把對話、訊息等新資料納入帳號刪除與閒置清除（5.3 的架構測試會提醒），並提供管理員後台的年齡區間彙總統計（呼叫 `UserRepository.count_by_age_range`）。
- 對外開放前須以實際使用的供應商條款重新確認年齡規則（RPG.md T-10）；若要把最低年齡改為 18，只需修改 `AgePolicy` 並提高政策版本。
