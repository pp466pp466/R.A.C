# Tasks

> 每一組做完都要跑該組新增或改動的測試，以及 `pytest` 中受影響的既有測試；整套測試在每一組結束時應為綠色（設計 D10）。刪除測試時，要能對應到被移除的需求，不順手刪掉仍有效的覆蓋。測試輔助：玩家的登入夾具一律用 `tests/support/auth.py` 的 `sign_in_as`／`add_user`，管理員用 `tests/support/google.py` 的 `admin_sign_in`。

## 1. 資料遷移與狀態值

- [ ] 1.1 新增 Alembic migration `0011_remove_player_passwords`（只動資料，D2）：非管理員的 `password_hash` 設為空、`failed_login_count` 設為 0、`locked_until` 設為空；`status` 與 `status_before_deletion` 為 `pending_verification` 者改為 `active`。以 `sa.table`／`sa.column` 加 `sa.update` 表達，不含原生 SQL、不匯入應用程式程式碼；可重複執行；`downgrade()` 不做事並以註解說明。驗證：`tests/test_migrations.py` 新增案例——種入密碼玩家、待驗證玩家、待刪除且申請前為待驗證的玩家與帶密碼的管理員，升級後依「玩家不持有密碼」需求的三個遷移情境逐項斷言，管理員完全不變，再升級一次結果相同；SQLite 升到最新並可降回；SQL Server 整合測試（`project_test`）升級成功。
- [ ] 1.2 `app/models/user.py` 移除 `STATUS_PENDING_VERIFICATION`、`TOKEN_VERIFY_EMAIL`，`USER_STATUSES` 與 `TOKEN_PURPOSES` 隨之調整，更新模組與欄位註解（`status` 不再「依信箱驗證設定指定」、`status_before_deletion` 只會是 `active`）。驗證：`grep -rn "pending_verification\|STATUS_PENDING_VERIFICATION\|TOKEN_VERIFY_EMAIL" app` 只剩 migration 與本 change 無關的文字；`tests/architecture` 全過。

## 2. 設定與寄信器

- [ ] 2.1 `app/config.py`：移除 `_resolve_email_verification` 與 `AppConfig.email_verification_required`；寄信設定改為 `ALERT_EMAIL_TO` 非空或任何一項 `MAIL_SMTP_HOST`／`MAIL_SMTP_PORT`／`MAIL_FROM` 非空時解析並要求完整（D6）；`BASE_URL` 只在 Google 啟用時必填；讀到 `EMAIL_VERIFICATION_REQUIRED` 時記一則只含變數名稱的警告且不影響啟動。驗證：`tests/test_config.py`、`tests/test_config_alerts.py` 改寫並新增「已淘汰的信箱驗證開關被忽略」三個情境（production 殘留 `false`、不合法的值 `maybe`、警告不含值）與「設定了寄信主機但缺少寄件者」「沒有任何寄信設定」「Google 需要 BASE_URL」；`tests/support/config_env.py` 移除該變數。
- [ ] 2.2 `.env.example` 移除 `EMAIL_VERIFICATION_REQUIRED` 並更新寄信設定的說明；`scripts/launch_plan.py` 自 `PLAIN_VARS` 移除它、刪除「production 必須開啟信箱驗證」與 `BASE_URL` 隨驗證開關的條件（`BASE_URL` 改為一律必填：部署目標靠 Google 登入，OAuth 回呼與管理員重設連結都需要它），計畫檔含舊變數時錯誤訊息指出「已淘汰，請移除」。驗證：`tests/test_env_example.py`、`tests/test_launch_plan.py` 更新；新增「計畫檔含 `EMAIL_VERIFICATION_REQUIRED` 被拒絕且訊息只含變數名稱」。
- [ ] 2.3 `app/__init__.py` 的寄信器建立條件、`app/runtime.py` 與 `app/services/registry.py` 依新的設定調整（`build_auth_service`、`get_auth_service`、`get_access_policy`、`build_email_flow_service`、`get_email_flow_service` 不再讀驗證旗標；`get_auth_service` 在有寄信器時才注入通知信元件）。驗證：`tests/test_app_factory.py` 新增「沒有寄信設定時沒有寄信器、管理員重設停用」「有 `MAIL_*` 時有寄信器」。

## 3. 服務層

- [ ] 3.1 `AccessPolicy`：移除 `email_verification_required` 參數，`_account_usable` 只看 `status == active`；更新模組說明。驗證：`tests/test_access_policy.py` 改寫「信箱驗證時間為空的啟用帳號可使用客服」「停權不可」「檢查順序」，刪除兩個依賴驗證開關的情境；`tests/test_age_gate_api.py` 依 `access-control` 的「帳號被停權」情境調整。
- [ ] 3.2 `AuthService`：移除 `register`、`login`、`_validate_new_account` 中的同意與年齡分支（`create_admin` 仍用其餘驗證）、`email_verification_required` 參數與 `AccountMailer.send_verification`；`change_password` 的通知信改為有 mailer 才寄；移除 `LOGIN_FAILED_UNKNOWN_USER`、`LOGIN_FAILED_ADMIN_PASSWORD_ONLY`、`REGISTER`、`REGISTER_EXISTING`、`AGE_CONFIRMED_REGISTER`、`VERIFICATION_*`、`EMAIL_VERIFIED` 等事件常數（`app/observability/security_events.py`，僅移除沒有任何呼叫者的）。驗證：`tests/test_auth_registration.py`（刪除）、`tests/test_auth_login.py`（只保留管理員密碼驗證與鎖定）、`tests/test_auth_session_service.py`、`tests/test_auth_confirm_password.py`、`tests/test_age_step_service.py`、`tests/test_cli_create_admin.py`、`tests/test_security_events.py` 改寫（管理員密碼驗證、鎖定、變更密碼、`verify_age`、`create_admin` 的既有覆蓋保留）；`grep -rn "def register\|def login" app/services/auth_service.py` 無結果。
- [ ] 3.3 `EmailFlowService` 縮成管理員的重設流程（D4）：移除驗證、重寄、`request_password_reset(email)`、`send_deletion_notice`、`_mark_verified`；`reset_password` 新增「權杖所屬帳號必須是管理員」；`app/services/mail_messages.py` 刪除 `verification_mail`、`verification_link`、`deletion_notice_mail`。驗證：`tests/test_email_flows.py` 改寫為管理員重設（重設成功、無效權杖、非管理員的權杖無效、並行使用、新權杖取代舊權杖、頻率限制、寄信失敗不影響結果、日誌不含權杖）；`tests/test_mail_sender.py` 對應調整。
- [ ] 3.4 `AccountDeletionService` 移除 `DeletionMailer` 與寄通知信，刪除申請前狀態為 `pending_verification` 的還原邏輯（`UserRepository.restore_from_deletion` 一律還原為 `active`）；`CurrentUser.may_delete_account` 與 `AdminService`／`AdminRepository` 的停權、解除停權移除 `pending_verification`（解除一律 `active`，`AdminService` 不再有 `email_verification_required` 參數）。驗證：`tests/test_account_deletion_service.py`、`tests/test_account_api.py`、`tests/test_admin_service_users.py`、`tests/test_admin_repository_users.py`、`tests/test_admin_api.py`、`tests/test_user_repository_deletion_request.py` 改寫並新增「申請刪除不寄任何信」；`tests/test_privacy_lifecycle_flow.py` 依新行為調整。
- [ ] 3.5 `GoogleSignInService`：合併規則的註解與日誌事件維持，移除只為舊流程存在的 `STATUS_PENDING_VERIFICATION` 分支（遷移之後不會再出現）。驗證：`tests/test_google_sign_in_service.py` 的「遺留的未驗證帳號」「遺留的已驗證玩家帳號」「管理員第一次綁定」「全新信箱」四個情境通過。

## 4. 控制器與頁面外殼

- [ ] 4.1 `app/controllers/auth_api_controller.py` 移除 `register`、`login`、`verify-email`、`resend-verification`、`forgot-password` 與相關常數；`reset-password` 的可用條件只剩 `admin_password_reset_available()`；`change-password`、`admin-password`、`admin-password/forgot`、`verify-age`、`google/start`、`me`、`logout`、`logout-all` 不變。驗證：`tests/test_auth_api.py`、`tests/test_email_flow_api.py` 改寫——五個已移除端點回 404（含管理員的正確信箱與密碼呼叫 `/api/auth/login` 不建立 session、不計入失敗次數）、`reset-password` 在沒有寄信設定時 404；`tests/test_restricted_session.py` 的路由掃描、`tests/test_admin_two_step_login.py`、`tests/test_idor_harness.py` 仍通過。
- [ ] 4.2 `app/controllers/auth_page_controller.py` 移除 `/forgot-password`、`/verify-email` 與初始狀態中的 `verificationRequired`；`/reset-password` 在沒有寄信設定時 404；`/register` 仍導向 `/login`。驗證：`tests/test_auth_pages.py`、`tests/test_admin_password_page.py` 改寫（已移除的頁面 404、重設頁可用性、`/account` 不再帶驗證旗標）。

## 5. 前端

- [ ] 5.1 刪除 `app/templates/auth/forgot_password.html`、`verify_email.html` 與 `app/static/js/auth/forgot-password.js`、`verify-email.js`；`reset_password.html`／`reset-password.js` 的用語改為管理員；`login.html` 與 `login-password.js` 檢查不再引用已刪除的頁面（「忘記密碼」連結只在管理員密碼驗證頁）。驗證：`tests/test_auth_pages.py` 掃描範本不含 `forgot-password`、`verify-email`；`tests/test_assets_manifest.py`、`tests/test_fonts_and_static.py`（靜態資源與引用一致、無 CDN、無 `v-html`）通過；必要時 `python scripts/update_asset_manifest.py` 更新資源清單。
- [ ] 5.2 `app/templates/auth/account.html` 與 `account.js` 移除「尚未驗證」提示、重寄按鈕與 `verificationRequired`、`resend`；`auth-flows.js` 與 JS 單元測試移除只為驗證流程存在的函式。驗證：`node --test "tests/js/*.test.js"` 與 `tests/test_js_unit_tests.py` 通過（`auth-common.test.js`、`auth-flows.test.js` 改寫）；`tests/test_account_page.py`、`tests/test_auth_pages.py` 的「沒有重寄驗證信」情境。

## 6. 客服提示詞

- [ ] 6.1 `app/services/cs_prompt.py` 的 `_platform_facts()` 與服務範圍第 1 項改為如實描述 Google 登入（D9，`cs-nlp-contract`「客服的平台資料如實描述 Google 登入」）：條款勾選、登入後填生日與區間、24 小時期限取自 `AGE_CONFIRMATION_DEADLINE`、最低年齡取自年齡政策、被鎖定只能申訴、帳號頁的登出所有裝置／匯出／刪除；移除對 `MIN_PASSWORD_LENGTH`、`MAX_PASSWORD_LENGTH`、`MAX_FAILED_LOGINS`、`LOCK_DURATION`、`VERIFY_TTL`、`RESET_TTL` 的匯入；`cs_intents.INTENT_ACCOUNT_HELP` 說明改為「登入、Google 登入、帳號設定等帳號問題」。驗證：`tests/test_cs_prompt.py`、`tests/test_cs_intents.py` 新增三個情境（只描述 Google 登入、不含「忘記密碼」「信箱驗證」「驗證信」「密碼長度」、期限數字隨常數改變），意圖三處一致的既有測試通過；`tests/test_chat_service.py`、`tests/test_cs_context.py` 中引用舊字樣的案例同步調整。

## 7. 自動化驗收工具

- [ ] 7.1 新增 `scripts/test_sessions.py`（D8）：產生一段在目標環境程序內執行的程式——拒絕 `APP_ENV=production`、建立 N 位 `@loadtest.invalid` 的測試玩家（`active`、已完成年齡確認的成年人、條款已同意、無密碼）、以 `app.session_interface` 的簽章器簽出 session cookie、以 JSON 印出；提供 `--local`（以目前環境變數載入應用程式）與 `--docker-exec <容器>` 兩種執行方式；輸出檔寫 UTF-8、已加入 `.gitignore`。映像不新增任何指令或端點。驗證：`tests/test_test_sessions.py`（以測試應用程式執行產生的程式：簽出的 cookie 能通過 `/api/auth/me`、信箱在保留網域、`production` 拒絕且沒建立帳號、輸出不含密碼雜湊）；`tests/test_container_files.py` 新增「映像不含 `scripts/` 與 `loadtest/`」並通過。
- [ ] 7.2 `loadtest/actions.py` 的 `register_and_login` 改為 `sign_in`（讀 cookie 檔、不足就在送出任何請求前失敗並說明缺少數量）；`loadtest/locustfile*.py` 與 `loadtest/fake_app.py` 調整（不再設定驗證旗標、說明文字更新）；`loadtest/guard.py` 的測試帳號網域沿用。驗證：`tests/test_loadtest_harness.py` 改寫（cookie 不足的失敗、一位虛擬使用者的流程、保留網域）；以 `fake_app` 加 `scripts/test_sessions.py --local` 實際跑一輪短測（20 位虛擬使用者、1 分鐘），結果記入 README 驗收紀錄。
- [ ] 7.3 `scripts/staging_smoke.py`：移除註冊與登入步驟與 `/api/auth/login` 的錯誤憑證檢查，新增「`POST /api/auth/login`、`/api/auth/register` 得到 404」；需要登入的項目只在環境變數 `SMOKE_SESSION_COOKIE` 提供 cookie 時執行，否則輸出「未驗證」並說明原因（結束代碼不因此非零）；輸出不含 cookie 值。驗證：`tests/test_staging_smoke.py` 改寫並新增「沒有 cookie 時需要登入的項目標為未驗證」「輸出不含 cookie」。
- [ ] 7.4 `scripts/verify_container.py`：玩家流程改為以 7.1 的輔助模組在容器內簽出 cookie 後檢查（`/api/auth/me`、`/account`、缺 CSRF 被拒、登出後 401），移除註冊與密碼登入的檢查，新增五個已移除端點回 404、管理員以密碼登入端點得到 404 且沒有 session cookie；`scripts/dev_smtp_sink.py` 的說明改為管理員重設信。驗證：`tests/test_container_files.py` 通過；實際執行 `python scripts/verify_container.py`（WSL Docker）全部通過，記錄項目數。

## 8. 文件、規範與總驗證

- [ ] 8.1 更新 `docs/` 設計文件（對照表見 `docs/README.md`）：`帳號與安全-auth-security.md`（端點表、流程圖、事件、設定，移除註冊／密碼登入／驗證信／玩家忘記密碼，補上遷移 `0011` 與管理員重設）、`系統架構-architecture.md`、`資料模型-data-model.md`（狀態值、權杖用途、`status_before_deletion`）、`營運與部署-operations.md`（上線前關卡 1 改為「營運告警與管理員重設密碼的寄信設定」、環境變數表、部署步驟含 `0011`、負載測試與冒煙驗收的測試 cookie 做法）、`前端結構-frontend.md`（頁面與腳本清單）、`路線圖-roadmap.md`（change ② 狀態與日期）；`docs/report.md` 與 `.claude/skills/python-flask-skill/references/auth-security.md` 中相關敘述同步。驗證：`pytest tests/test_design_docs.py tests/test_launch_docs.py tests/test_agents_skill_sync.py tests/test_readme.py` 通過。
- [ ] 8.2 更新 `README.md`：環境變數表、端點與頁面說明、營運手冊與上線前關卡、載入測試與冒煙驗收的用法，並新增「驗收紀錄（remove-player-password-auth）」，分成「已驗證」與「未驗證」兩欄，沒有實際執行的項目標為未驗證。驗證：`tests/test_readme.py` 通過；README 中不再出現 `EMAIL_VERIFICATION_REQUIRED`（除了說明已淘汰的那一句）、`/api/auth/register`、`/forgot-password`、`/verify-email`。
- [ ] 8.3 規格收尾：移除 15 份主規格頁首「待 change ② 更新」的提示；改寫 `member-registration`、`email-token-flows`、`session-authentication` 的 Purpose（delta 不能改 Purpose，直接編輯主規格）；`launch-gates`、`admin-user-management` 等被修改的規格在封存時同步。驗證：`openspec validate remove-player-password-auth --strict` 通過；`grep -rn "待 change ②" openspec/specs` 無結果（封存後）。
- [ ] 8.4 總驗證並記錄數字：完整 `pytest`（含 SQL Server `project_test`，SQLite 與 SQL Server 各跑一次、不略過）、`node --test "tests/js/*.test.js"`、`python scripts/verify_container.py`、`openspec validate --specs`。驗證：全部通過，數字寫入 README 的驗收紀錄；任何失敗先修正再繼續。
- [ ] 8.5 人工驗收（需要使用者在場，沿用 `local-rehearsal` 的 ngrok 通道與預備環境專用 OAuth 用戶端）：遷移前用舊版程式建立一個密碼玩家與一個待驗證玩家，升級遷移與新版程式後，以同信箱的 Google 登入回到原帳號（不需重填年齡、不需勾選條款）；`/login`、`/forgot-password`、`/verify-email` 與五個已移除端點的行為；管理員先 Google 再密碼與忘記密碼寄信（Brevo）仍正常；申請刪除帳號不寄信。驗證：結果逐項記入 README；使用者不在場則標為未驗證。

## Workflow follow-up

- 封存：`/openspec-archive-change remove-player-password-auth`（封存前確認設計文件已隨本 change 更新；封存後更新 `docs/路線圖-roadmap.md` 的狀態與日期，並複查各設計文件標頭的「對應 change」）。
- 之後的獨立決定：是否讓 staging／production 強制要求 Google 設定（見 design 的 Open Questions）。
