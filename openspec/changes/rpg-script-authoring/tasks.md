# Tasks

## 1. 資料模型與遷移

- [x] 1.1 `User` 新增 `nickname`、`nickname_code`、`publish_suspended_at`（皆可為空，後者建索引）；`ScriptVersion` 新增 `review_hash`、`review_verdict`、`review_categories_json`、`reviewed_at`（皆可為空）；新增模型 `ScriptSubmission`（`script_submissions`，`user_id` 串聯刪除，`(user_id, created_at)` 索引，沒有版本欄位）與 `RuntimeLimit`（`runtime_limits`，`key` 唯一）；`llm_call_log` 新增 `KIND_SCRIPT_REVIEW`；`CurrentUser` 新增暱稱、短代號與暫停時間（暫停只以布林進 `public_dict`）。驗證：模型可建立，匯入無循環。
- [x] 1.2 撰寫遷移 `0013_rpg_script_authoring`：`llm_call_logs.kind` 加寬為 20 字（5.4 的 SQL Server 測試才發現 13 字的 `script_review` 放不進 10 字）、`users` 與 `script_versions` 以 `batch_alter_table` 加欄位、兩張新表與索引、以 `op.bulk_insert` 寫入七個初始限制列（free／plus／vip 的劇本數 3／10／30、每日送審 3／5／10、連續被拒門檻 3）；降級移除全部並沿用 `recreate="never"` 的說明；腳本只用 op API 與 ORM，無原生 SQL。驗證：升級、再升級、降級在 SQLite 與真實 SQL Server 都成功。
- [x] 1.3 遷移與約束測試（SQLite 與 SQL Server 兩軌）：從 `0012` 升級並保留既有帳號與劇本、新欄位為空、七個初始列存在且鍵唯一、重複升級不產生重複列、降級後新欄位與新表消失；`script_submissions` 隨使用者串聯刪除；`alembic.ini` 與範本仍純 ASCII。驗證：`tests/test_migrations.py` 擴充通過。

## 2. 營運限制（資料庫設定表）

- [x] 2.1 新增 `app/services/runtime_limits.py`（`LIMIT_SPECS`：七個鍵的預設與範圍、`RuntimeLimits.get/all`、`script_limit_for(tier)`、`submit_daily_for(tier)`、`reject_streak()`）與 `RuntimeLimitRepository`（逐鍵讀取與條件式更新）；每次呼叫都查資料庫，無模組層快取；缺值、非整數或範圍外回保底預設並記一筆只含鍵名的警告事件；不認得的等級套用 free。驗證：單元測試涵蓋缺值、損壞、範圍外、修改後下一次呼叫立即生效，以及以兩個 Session 模擬兩個 worker 讀到同一個值。
- [x] 2.2 `admin_audit_log` 動作列舉新增 `limit.set`、`publish.suspend`、`publish.lift`（管理員欄位可為空的動作清單同步）；`app/cli.py` 新增 `flask set-script-limit KEY VALUE`（驗證鍵與範圍、條件式更新、寫稽核、輸出舊值與新值）與 `flask show-script-limits`，兩者列入 `MANUAL_COMMANDS`；`scripts/launch_plan.py` 的指令清單同步。驗證：CLI 測試涵蓋成功、未知鍵、範圍外、稽核內容只有鍵與新舊值；測試確認沒有任何路由能寫入限制。
- [x] 2.3 限制的行為測試（`tests/test_runtime_limits.py`）：逐一對應 `runtime-limits` 規格的情境（鍵與範圍、初始值、每次現讀、保底預設、指令與稽核、沒有網路端點）。驗證：測試通過，SQLite 與 SQL Server 兩軌。

## 3. 暱稱與年齡步驟

- [x] 3.1 新增 `app/services/nickname.py`：`clean_nickname`（NFC、去控制字元與零寬字元、頭尾空白、2 至 20 字、網址／電子郵件／電話的樣式、含「官方」，比對前摺疊）與 `new_code`（不含易混淆字元的 4 碼，`secrets.choice`）。驗證：`tests/test_nickname.py` 逐一對應 `player-nickname` 的格式情境（合格、長度、控制字元、網址信箱電話、冒用官方、全形變體）。
- [x] 3.2 `UserRepository` 新增 `set_nickname`（第一次設定時一併寫入短代號，之後只更新暱稱）；`complete_age_verification_if_unset` 在同一個條件式 UPDATE 一併寫入暱稱與短代號；`AuthService.verify_age` 先驗證暱稱再做年齡檢查（暱稱不合格不計入鎖定計次、不保存任何東西；管理員可不提供）；`POST /api/auth/verify-age` 接受 `nickname`；新增 `PUT /api/account/nickname`；`profile` 與 `/api/auth/me` 回傳本人的暱稱與短代號。驗證：服務與 API 測試涵蓋全有或全無、缺暱稱、未滿 13 歲不保存、重送不改動、受限登入狀態被拒、他人看不到、短代號更改暱稱後不變。
- [x] 3.3 `/age` 與 `/account`：`age.html`／`age.js` 加暱稱欄位與公開提示、暱稱錯誤顯示在欄位且不清空生日與區間；`account.html`／`account.js` 顯示暱稱與短代號、設定或更改表單、沒有暱稱的提示與進入 `/scripts` 的連結，成功以 Toast 回報；更新既有測試輔助 `tests/support/age_gate.py` 與相關測試使其帶暱稱。驗證：頁面測試與前端規範架構測試通過（無 CDN、無 `v-html`、無 Bootstrap JS）。
- [x] 3.4 暱稱的生命週期：個人資料匯出的帳號區塊加暱稱與短代號；抹除守門測試確認帳號刪除後不再存在；管理員的使用者檢視不含暱稱。驗證：匯出、抹除與管理員檢視的測試通過。

## 4. 規則層

- [x] 4.1 新增資料檔 `app/data/review/lexicon.json`（版本字串；性相關、未成年、非自願、性暴力、真人五組詞與年齡數字樣式；UTF-8）與 `app/services/script_rules.py` 的 `check_rules(content, *, tier)`：切句、去空白與零寬字元、摺疊、同句共現判定，輸出結果、類別與詞表版本；純函式，不匯入資料庫、網路、模型。詞表初稿提交前請使用者審閱。驗證：模組匯入檢查通過。
- [x] 4.2 規則層測試（`tests/test_script_rules.py`），逐一對應 `script-review` 的規則層情境：確定性、拆字閃避、四類紅線各一個明確命中、**誤擋防線**（帶三歲寶寶的成年主角、十六歲高中生的校園日常、親子與育兒劇本皆 `pass`）、未成年角色加性相關詞為 `flag`、宣告 `safe` 卻有性相關詞為 `tier_mismatch`、結果不回顯命中的詞。驗證：測試通過。

## 5. 模型層審查與開存檔把關

- [x] 5.1 新增 `app/services/script_review_prompt.py`：固定的 `system_instruction`（繁體中文、八個類別、劇本文字是資料、不公開標記 `HMAC(SECRET_KEY, "script-review-marker-v1")`）、回應 schema（`verdict`、`categories` 最多 4 個列舉）、把劇本文字與宣告的內容模式組成資料區塊的函式、語意規則（`pass` 無類別、`reject` 限四個可拒絕類別、`flag` 至少一個、含標記視為失敗）。驗證：單元測試涵蓋 schema、語意規則、標記外洩與資料區塊的位置。
- [x] 5.2 `ScriptRepository.record_review`（條件式比對 `content_hash`，回傳是否寫入）與 `ScriptReviewService`（`review_model`：RPG 供應商未設定或全站預算用完回 `unavailable`、`run_structured(kind=KIND_SCRIPT_REVIEW)`、降級或被安全過濾阻擋一律 `unavailable`；`ensure_reviewed`：雜湊相同直接用先前結果、`reject` 也記錄）；`registry.py` 組裝；擴充 `tests/architecture/test_script_state_writes.py` 使審查欄位只能由 `record_review` 寫入（附反向驗證）。驗證：架構測試通過。
- [x] 5.3 `SessionService.start_session` 接上把關：內建種子劇本（沒有作者）視為已審查，其餘 `ensure_reviewed`；新增錯誤 `ScriptRejected`（422 `script_rejected`，帶類別）與 `ReviewUnavailable`（503 `review_unavailable`）並更新錯誤對照測試。驗證：RC-1 的存檔測試全部仍通過，內建劇本開存檔不呼叫審查。
- [x] 5.4 模型層與把關的測試（`tests/test_script_review.py`，假供應商、不打真實 API），逐一對應 `script-review` 的情境：合法輸出、不合法輸出重試後視為不可用、標記外洩、劇本文字要求放行不影響指示且位置正確、雜湊相同不重複呼叫、修改後重新審查、審查期間被修改時結果丟棄、供應商逾時與未設定與預算用完皆 503 且不寫通過標記、用量帳本有 `script_review` 一筆且不含內容、日誌掃描找不到劇本文字。驗證：測試通過，SQLite 與 SQL Server 兩軌。

## 6. 創作 API

- [x] 6.1 `ScriptRepository` 補齊創作所需：`count_owned`、`update_draft` 新增 `expected_hash`（不符時拋出 `DraftChangedError`，與 `VersionNotDraftError` 區分）、由最新的非草稿版本取得可複製的內容（沿用里程碑 `id`）、`list_owned` 回傳版本摘要所需欄位；新增的 Repository 錯誤對應 409 代碼 `draft_changed`。驗證：Repository 測試涵蓋條件式更新與並行。
- [x] 6.2 新增 `ScriptAuthoringService`（`create`、`update_draft`、`new_version`、`delete_draft`、`withdraw`、`refresh_author_name`、`list_mine`、`get_version`、`example`、`limits`）與 `AccessPolicy.require_can_submit`：每個方法先過年齡閘門；建立前檢查暱稱（`nickname_required`）與劇本數（`script_limit_reached`）；`validate_script` 的上限取作者年齡上限；儲存前跑規則層，`reject` 回 422 `script_auto_rejected` 且不寫入。驗證：服務單元測試。
- [x] 6.3 `script_api_controller.py`（Blueprint `/api/scripts`：列表、建立、讀取版本、更新草稿、建立新草稿、刪除、撤回、重新取用筆名、範例、額度；回應 `no-store`）、`script_page_controller.py`（`/scripts`，僅外殼）與 `registry.py` 的組裝；`flask_app` 註冊。驗證：路由清單測試與 README 端點表測試更新通過。
- [x] 6.4 創作 API 的測試（`tests/test_script_authoring_api.py`、權限測試框架）：逐一對應 `script-authoring` 的情境，包含 IDOR（讀取、更新、刪除、撤回、新草稿、筆名都對他人的資源回 404）、未登入 401、未完成年齡確認 403、受限登入狀態、缺 CSRF、`nickname_required`、`script_limit_reached`、未成年作者宣告 `standard` 被拒、`draft_changed`、`not_draft`、`open_version_exists`、`author_name_locked`、範例通過驗證。驗證：測試通過。

## 7. 送審、暫停與解除

- [x] 7.1 `ScriptSubmissionRepository`（寫入紀錄、今日次數、最近 N 筆、清除過期）與 `ScriptSubmissionService.submit`：依序年齡閘門、擁有者、草稿、未暫停、今日次數、固定規格與規則層、模型層、合併 `flag` 類別並 `record_review`、條件式轉為待審、寫 `sent`；`reject` 寫 `auto_rejected` 並回 422 與類別；`unavailable` 回 503 且不寫紀錄；`POST /api/scripts/versions/<id>/submit`。驗證：服務單元測試。
- [x] 7.2 暫停發佈：`UserRepository.suspend_publish_if_clear`（條件式 UPDATE，管理員不被暫停）與連續被拒的評估（最近 N 筆全為 `auto_rejected`，`sent` 與 `lifted` 使連續中斷）、`publish.suspend` 稽核（管理員欄位為空）與事件；`AccessPolicy.require_can_submit` 擋送審並回 403 `publish_suspended`，其餘功能不受影響。驗證：測試涵蓋連續三次、成功一次即中斷、並行只暫停一次與只寫一筆稽核、管理員不被暫停、暫停期間仍可編輯與開存檔。
- [x] 7.3 送審的行為測試（`tests/test_script_submission.py`），逐一對應 `script-submission` 的情境：固定檢查順序（每個檢查失敗時後面的步驟都不執行）、並行重送只有一個成功、自動拒絕只告知類別、紀錄不含任何內容、每日次數的台灣日界（23:59／00:01）與依等級的不同上限、不可用與儲存時的拒絕不計入、`flag` 的送審成功且版本記有類別。驗證：測試通過，SQLite 與 SQL Server 兩軌。
- [x] 7.4 管理員的暫停檢視與解除：`AdminService.lift_publish_suspension`（管理員、目標為一般使用者、重新輸入密碼、未被暫停回 409 `not_suspended`、清旗標並補一筆 `lifted`、寫 `publish.lift` 稽核）、`GET /api/admin/publish-suspended` 與 `POST /api/admin/users/<public_id>/lift-publish-suspension`；後台頁面在「被鎖定的帳號」旁新增「被暫停發佈的帳號」區塊與自製 Modal 的解除確認。驗證：管理員 API 與頁面測試涵蓋密碼錯誤、非管理員 404、解除後可再送審、解除後再被拒一次不立即暫停。

## 8. 創作頁面

- [x] 8.1 `/scripts` 頁面：`templates/scripts/workshop.html` 與 `static/js/scripts/`（`script-api.js`、`script-draft.js`、`milestone-preview.js`、`script-workshop.js`）：劇本與版本列表、分區編輯器（目錄、背景與體系、角色、地點、里程碑、結局與初始對話、標籤）、欄位範例帶入、儲存草稿帶 `base_hash`、`draft_changed` 的 Modal、送審與刪除的自製 Modal、被暫停時的說明與申訴連結、額度顯示；Vue 全域變數模式，資源經 `url_for('static', ...)`，不用 CDN、`.vue`、Bootstrap JS 與 `v-html`；送審的 axios 逾時 35 秒並以 `AbortController` 於離開頁面時中止，`finally` 恢復按鈕。驗證：前端規範架構測試與頁面測試通過。
- [x] 8.2 前端純邏輯的 Node 測試（`tests/js/`）：`milestone-preview`（依觸發排序、年齡與年份混合、空清單）、`script-draft`（空白內容、欄位計數、與伺服器錯誤路徑對應）、`restriction`（`account_locked`、`publish_suspended`、其他 403 代碼、`/api/auth/me` 的 `restriction` 與 `publish_suspended`、無關輸入皆回 `null`）。驗證：`node --test "tests/js/*.test.js"` 全部通過。
- [x] 8.3 申訴捷徑（`appeal-shortcut`）：新增 `static/js/ui/restriction.js`（`restrictionOf`，純函式）與 `ui-components.js` 的 `UiRestrictionNotice`（常駐橫幅與阻擋提示兩種樣式，按鈕為指向 `/appeal` 的一般連結，無 `data-bs-*`、無查詢字串；`UI.install` 註冊）；串接 `/cs`（`GATE_CODES` 新增 `account_locked`，`gateNotice` 附「前往申訴」連結並停用輸入區、保留已輸入文字）、`/scripts`（載入判定、儲存與送審錯誤判定、送審按鈕停用）、`/account`（暫停的說明與按鈕）；`/api/auth/me` 的 `publish_suspended` 由 `current_user.py` 提供。驗證：頁面測試涵蓋各情境（使用中被鎖定、被暫停時開啟 `/scripts` 與 `/account`、送審被拒、解除後重新整理消失、按鈕 HTML 形式），前端規範架構測試通過。

## 9. 申訴對暫停發佈的擴充（相依規範修訂）

- [x] 9.1 AGENTS.md 第 7、10、11 節的修訂（design D13，使用者已於 2026-11-04 核可全部五項）、`.claude/skills/python-flask-skill/` 鏡像與 `tests/test_agents_skill_sync.py` 釘住新字句，**單獨提交**，且須在 9.2 之前完成。驗證：同步測試通過。
- [x] 9.2 `AppealService` 改為鎖定或暫停都可提交與查看（兩者皆非回 404）、`AdminRepository` 的申訴檢視擴為鎖定或暫停且解除其中之一時只標示該項、`/appeal` 頁面的存取判斷同步、解除暫停時待處理申訴標為已處理。驗證：測試涵蓋被暫停者提交成功、每小時 3 則與同時 1 則的上限相同、兩者皆非 404、管理員看得到、解除後不可見、不能讀取對話、申訴內容不進稽核與日誌。

## 10. 生命週期與可觀測性

- [x] 10.1 匯出：`PersonalExportService` 加 `script_submissions`（結果、類別、時間）與 CSV `script_submissions.csv`，並確認劇本區塊不含審查欄位；更新匯出測試的檔案清單。驗證：匯出測試涵蓋含送審紀錄、不含劇本文字、不含審查欄位、公式字元中和。
- [x] 10.2 抹除與保存期限：`test_account_erasure_coverage` 登記 `script_submissions` 與新欄位；`RetentionRepository`／`DataRetentionService` 新增類別 `script_submissions`（365 天、預覽、分批、輸出只有筆數），`app/cli.py` 的 `RETENTION_LABELS` 同步。驗證：抹除與保存期限測試涵蓋 364／365 天邊界、預覽不刪除、不影響暫停旗標。
- [x] 10.3 事件與日誌：`security_events`／生命週期事件新增暱稱設定、劇本操作、審查結果、自動拒絕、暫停與解除、限制不合法的事件常數與欄位白名單；日誌掃描測試擴充到整個創作與送審流程（暱稱、短代號、筆名、劇本文字、標題、標籤皆不得出現）。驗證：日誌白名單與掃描測試通過。

## 11. 驗收與文件

- [x] 11.1 容器驗收：`scripts/verify_container.py` 新增檢查（遷移升級到 `0013`、`flask show-script-limits` 輸出七個鍵、`set-script-limit` 修改並可讀回、`purge-expired-data` 預覽含 `script_submissions`），輸出只有鍵與筆數。驗證：`verify_container.py` 全部通過。
- [x] 11.2 README：端點表（`/api/scripts/*`、`PUT /api/account/nickname`、`/api/admin/publish-suspended` 與解除、`/scripts`）、flask 指令表（`set-script-limit`、`show-script-limits`）、保存期限表與驗收紀錄（分已驗證與未驗證兩欄，沒有實際執行的項目一律標為未驗證）。驗證：`tests/test_readme.py` 通過。
- [x] 11.3 人工驗收（使用者在場，未做就在驗收紀錄標為未驗證；**實際以驗收環境的模擬身分與假審查模型進行，未經真實 Google 登入，見 README 驗收紀錄**）：以全新 Google 帳號在 `/age` 填暱稱完成年齡確認；在 `/account` 更改暱稱；在 `/scripts` 建立劇本、帶入範例、儲存、故意觸發 `draft_changed`、刪除草稿、送審並看到待審狀態；用真實或假的 RPG 模型看到審查通過與被拒絕的回應；以 `flask set-script-limit` 調降上限後立即在頁面看到新的上限。
- [x] 11.4 更新 docs/ 對應的設計文件（對照表見 docs/README.md），並讓 tests/test_design_docs.py 通過：`資料模型-data-model.md`（新欄位與兩張新表、遷移 `0013`、保存與刪除）、`系統架構-architecture.md`（新元件與依賴方向、新的架構守門）、`帳號與安全-auth-security.md`（暱稱、年齡步驟、暫停發佈與申訴範圍）、`模型呼叫層-llm-layer.md`（劇本審查的呼叫類別與提示詞結構、移除「尚未實作」提示）、`前端結構-frontend.md`（`/scripts` 的模組）、`營運與部署-operations.md`（新指令、保存期限、日誌事件）、`路線圖-roadmap.md`（RC-7 狀態）與 `RPG.md` 的 RC-7 列。
- [x] 11.5 總驗證：`pytest` 全套（SQLite 與 SQL Server 一次只跑一個行程）、`node --test "tests/js/*.test.js"`、容器驗收、`openspec validate rpg-script-authoring --strict`、`openspec validate --specs`；結果與未驗證項目記入 README 驗收紀錄。
