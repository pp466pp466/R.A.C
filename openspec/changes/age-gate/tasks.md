# Tasks

## 1. 前置：批准規範修訂

- [x] 1.1 向使用者呈現 design.md 末段「建議的 AGENTS.md 第 10 節新增條文」，取得**明確批准或修改意見**；未批准前不進行第 2 組以後的實作。驗證：對話中有使用者的明確批准，且條文最終版已記下。（2026-10-28：使用者指示修訂 AGENTS.md 並於事後確認；最終條文即 AGENTS.md 第 10 節的「年齡確認」。）
- [x] 1.2 依批准的條文修訂 AGENTS.md 第 10 節，並同步 `.claude/skills/python-flask-skill/references/auth-security.md` 與 `SKILL.md`（兩者是 AGENTS.md 的摘要副本；`business-encoding.md` 對應第 7、8 節，沒有年齡條文）。驗證：以 Grep 在 AGENTS.md、`auth-security.md`、`SKILL.md` 都找得到「年齡確認」或 `age_confirmation_required`，且文字與批准版一致。（已完成，commit 5162824；`tests/test_agents_skill_sync.py` 也守住這條。）

## 2. 資料層

- [x] 2.1 `app/models/user.py` 的 `User` 新增 `age_confirmed_at`（UTC 時間）與 `age_policy_version`（`Unicode(40)`），皆可為空，並更新檔頭說明。驗證：`tests/test_user_models.py` 的「可為空欄位」集合加入這兩欄，新建的 `User` 兩欄預設為 None，測試通過。
- [x] 2.2 新增 migration `migrations/versions/20261028_0003_users_age_confirmation.py`（revision `0003_users_age_confirmation`，接在 `0002`）：以 `batch_alter_table` 新增兩欄，不匯入應用程式、不含原生 SQL；`downgrade` 刪除兩欄。驗證：`tests/test_migrations.py` 新增案例——升級後欄位存在且可為空、降級後消失、`alembic heads` 只有一個 head（`0003`）；在 SQLite 與 SQL Server（`project_test`）都通過。
- [x] 2.3 `UserRepository.confirm_adult_if_unset(user, confirmed_at, policy_version) -> bool`：沿用 `_run_update` 的條件式 UPDATE（`WHERE id = ? AND age_confirmed_at IS NULL`，ORM 表達式），回傳是否真的寫入。驗證：`tests/test_user_repositories.py` 新增案例——第一次回傳 True 並寫入、第二次回傳 False 且原時間與版本不變；以 `concurrent_engine` 讓兩個連線同時確認，只有一個回傳 True；SQLite 與 SQL Server 都通過。
- [x] 2.4 更新 `docs/spec_report_v2.2.0.md` 的 users 資料表說明（新增兩欄與其用途）。驗證：以 Grep 在該節找到 `age_confirmed_at` 與 `age_policy_version`。

## 3. Service 與權限政策

- [x] 3.1 `app/services/legal.py` 新增 `CURRENT_AGE_POLICY_VERSION`（`2026-10-28-self-declared`），並把 `CURRENT_TERMS_VERSION` 更新為 `2026-10-28-draft`（條款內容在 5.4 修改）。驗證：既有引用這兩個常數的測試（`test_auth_registration`、`test_google_sign_in_service`、`test_auth_pages`）通過，並新增斷言兩個常數都是非空字串。
- [x] 3.2 `CurrentUser` 新增 `age_confirmed`（`from_model` 以 `age_confirmed_at` 是否為空計算），`public_dict()` 回傳 `age_confirmed`，**不含**時間與版本。驗證：新增單元測試——已確認為 True、未確認為 False；`public_dict` 含 `age_confirmed` 布林，不含 `age_confirmed_at`、`age_policy_version`。
- [x] 3.3 `app/services/errors.py` 新增 `AgeConfirmationRequired(Forbidden)`（`code="age_confirmation_required"`）；`AccessPolicy.can_use_chat` 加入已確認條件；新增 `AccessPolicy.require_chat_access(user)`（依序檢查狀態、信箱驗證、年齡；年齡不足時記錄 `auth.age_gate_denied` 並拋出該錯誤）。驗證：`tests/test_access_policy.py` 擴充為狀態、信箱驗證、年齡、角色（admin 與 user）、會員等級（free 與 vip）的矩陣：只有條件齊全才回 True；管理員與 vip 不豁免；錯誤代碼正確；檢查順序為狀態與信箱驗證先於年齡。
- [x] 3.4 `app/observability/security_events.py` 新增 `auth.age_confirmed.register`、`auth.age_confirmed.google`、`auth.age_confirmed.later`、`auth.age_gate_denied`、`auth.google_rejected.age`。驗證：`tests/test_security_events.py` 確認五個名稱都已登記在 `EVENT_NAMES`，`auth.age_gate_denied` 為資訊層級、`auth.google_rejected.age` 為警告層級。
- [x] 3.5 `AuthService.register`（與 `_validate_new_account`）接受 `confirm_adult`：不是 `True` 時回報欄位錯誤 `confirm_adult`，且與 `accept_terms` 各自獨立回報；成功建立時寫入 `age_confirmed_at` 與 `age_policy_version`、記錄 `auth.age_confirmed.register`。同步更新 `tests/support` 的註冊輔助與既有測試（`test_auth_registration`、`test_auth_api`、`test_email_flows`、`test_email_flow_api`、`test_log_scan_full_flow`）。驗證：新增案例——缺少、為 False、為非布林值都被拒絕；只缺其中一項時錯誤指向缺少的那一項；成功時兩欄被記錄；信箱已存在時回應與成功完全相同且既有帳號不被改動；**驗證發生在查詢帳號是否存在之前**（輸入不合法時，已存在與不存在的信箱得到相同的 400）；更新後的既有測試全數通過。
- [x] 3.6 `AuthService.confirm_adult(current, confirm)`：`confirm` 不是 `True` 時拋出 `ValidationError`（欄位 `confirm_adult`）；否則呼叫 2.3 的方法，僅在實際寫入時記錄 `auth.age_confirmed.later`，回傳最新的帳號資料。驗證：新增案例——成功、不是 True 被拒絕且不寫入、重複確認成功且原時間不變、事件只在第一次記錄、日誌不含 email。
- [x] 3.7 `GoogleSignInService`：`begin` 接受 `confirm_adult` 並存入交易的 `a` 旗標；`complete` 與 `_resolve` 傳遞；建立新帳號時兩項都需要（沒同意條款 → `terms_required`，已同意但沒確認年齡 → `age_required` 並記錄 `auth.google_rejected.age`），成功時記錄確認與 `auth.age_confirmed.google`；既有帳號的登入路徑不變。同步更新 `tests/support/google.py` 與 `tests/test_google_sign_in_service.py`。驗證：新使用者的兩個旗標四種組合（只有兩項皆為真才建立帳號並記錄確認）；既有帳號（已綁定、以信箱合併）不受影響且確認欄位維持為空；沒有 `a` 的舊交易視為未確認。

## 4. HTTP 介面

- [x] 4.1 `auth_api_controller`：`/api/auth/register` 與 `/api/auth/google/start` 讀取 `confirm_adult`；新增 `POST /api/auth/confirm-adult`（需要登入，成功回應最新的帳號資料）；`/api/auth/me` 與登入回應含 `age_confirmed`。驗證：`tests/test_auth_api.py`、`tests/test_google_endpoints.py` 新增案例——狀態碼（400、401）、回應格式、`age_confirmed` 正確；`tests/test_csrf.py` 確認新端點沒有被豁免（缺少 CSRF 權杖得到 403，若該檔是逐路由列舉則自動涵蓋，否則補一案）。
- [x] 4.2 `auth_page_controller`：登入頁的固定訊息新增 `age_required`（「請先確認您已年滿 18 歲。」）。驗證：`tests/test_auth_pages.py` 新增案例——帶 `error=age_required` 時顯示該訊息；未知代碼仍不顯示；固定代碼集合的既有測試更新後通過。
- [x] 4.3 以**測試專用的端點**（只在測試應用程式中註冊，比照 IDOR 測試框架）驗證 `require_chat_access` 的 HTTP 行為。驗證：新增 `tests/test_age_gate_api.py`——尚未確認的使用者得到 403、JSON 代碼 `age_confirmation_required`；補確認後同一端點回 200；管理員與 `vip` 未確認時同樣被擋；被拒時日誌有 `auth.age_gate_denied` 與內部使用者編號、不含 email。
- [x] 4.4 擴充 `tests/test_log_scan_full_flow.py` 的完整流程（註冊、補確認、被拒、Google 註冊含確認），並擴充 `tests/test_cli_create_admin.py` 確認 `flask create-admin` 建立的帳號兩欄為空。驗證：完整流程後的日誌掃描找不到 email、密碼、權杖；管理員帳號的 `age_confirmed_at` 為空。
- [x] 4.5 更新 `README.md`：端點表新增 `POST /api/auth/confirm-adult`、註冊與 `google/start` 的請求欄位、`/api/auth/me` 的欄位、`age_confirmation_required` 代碼；「撰寫新端點：權限與 IDOR 測試」一節加入「AI 端點必須先呼叫 `require_chat_access`」；「建立首位管理員」一節說明管理員第一次登入後需補確認。驗證：`tests/test_readme.py` 的路由表一致性檢查通過，且三處說明可由 Grep 找到。

## 5. 前端

- [x] 5.1 `register.html` 與 `register.js`：在同意條款下新增獨立的「我已年滿 18 歲」勾選與錯誤顯示；送出時帶 `confirm_adult`；Google 按鈕在離開本站前檢查兩個勾選。驗證：`tests/test_auth_pages.py` 斷言頁面含該勾選與標籤；`tests/architecture/test_frontend_rules.py` 的前端規範掃描通過（無 `v-html`、無 Bootstrap JS、無 CDN）。
- [x] 5.2 `login.html` 與 `login.js`：Google 入口新增同樣的勾選，兩個勾選任一未完成就不離開本站並顯示對應提示。驗證：頁面測試斷言勾選存在；前端規範掃描通過。
- [x] 5.3 `account.html` 與 `account.js`（必要時含 `auth.css`）：清單新增「年齡確認」列；尚未確認時顯示說明、勾選與確認按鈕，沒勾選就按時不送出請求，成功後以 Toast 回報並更新狀態。驗證：頁面測試斷言兩種狀態的內容；前端規範掃描通過；若新增純函式邏輯則以 Node 測試涵蓋（`tests/js/`）。
- [x] 5.4 `terms.html`、`privacy.html`：載明僅限年滿 18 歲者使用、只記錄確認時間與政策版本而不蒐集生日年齡證件，更新資料保存表，仍標示為草稿。驗證：`tests/test_auth_pages.py` 斷言兩頁含上述說明與草稿標示，且頁面顯示的版本等於 `CURRENT_TERMS_VERSION`。
- [x] 5.5 以 Edge 無頭瀏覽器做人工驗收（比照 README 既有的「瀏覽器驗收紀錄」）：註冊頁兩個勾選與各自的錯誤位置、登入頁 Google 入口的兩個勾選、帳號設定頁補確認全流程、360px 與 1280px 無水平捲軸、鍵盤可操作。驗證：結果與項目數寫入 README 的瀏覽器驗收紀錄，全部通過。

## 6. 整合驗收

- [x] 6.1 完整測試：pytest（SQLite 與 SQL Server `project_test`）、Node 測試、`openspec validate age-gate --strict`。驗證：全數通過，數字記入 README 的驗收紀錄。
- [x] 6.2 更新 `scripts/verify_container.py`：會員流程驗收的註冊帶 `confirm_adult`，並新增補確認與未確認被擋的檢查；於 WSL 的 Docker 重新建置映像並執行。驗證：容器驗收全部通過，檢查項目數與映像大小記入 README。
- [ ] 6.3 本機開發資料庫 `project` 升級到 `0003`（**需使用者同意**；先備份）。驗證：`alembic current` 顯示 `0003_users_age_confirmation`，`/readyz` 回 200，既有帳號的兩欄為空。
- [x] 6.4 在 [RPG.md](../../../docs/RPG.md) 的 6.3 節 T-1 與 R-15.4 標註此 change 已完成與其範圍（自行聲明）。驗證：以 Grep 在 RPG.md 找到更新後的說明。

## Workflow follow-up

- 歸檔前請使用者確認 AGENTS.md 第 10 節的修訂已完成（任務 1.2）。
- C-3 的任務清單須納入「每個 AI 端點先呼叫 `AccessPolicy.require_chat_access`」，並在客服與 RPG 的端點測試中各驗證一次年齡閘門。
- 給 Google 的詢問信（`docs/google.pdf`）若得到更嚴格的確認要求，另開 change 提高 `CURRENT_AGE_POLICY_VERSION` 並實作重新確認。
- 歸檔本 change（`/openspec-archive-change age-gate`），並驗證歸檔結果。
