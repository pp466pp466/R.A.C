# Tasks

## 1. 年齡規則與未滿 18 歲抹除

- [x] 1.1 `AgePolicy`（`app/services/age_policy.py`）：最低註冊年齡與最低使用年齡改為 18、版本字串改為 `2026-11-17-adults-only`，說明文字與錯誤訊息只由最低年齡組成；更新 `tests/test_age_policy.py`（15 歲、17 歲邊界、18 歲生日當天通過；選「未滿 13 歲」「13–17」一律 `age_not_eligible`；不一致仍是 `age_mismatch`；日後把最低年齡改為 21 也只需換設定）。驗證：`pytest tests/test_age_policy.py` 通過。
- [x] 1.2 玩家補填未滿 18 歲的帳號被抹除（沿用 `erase_underage`）、管理員不被抹除、不保存生日與暱稱：更新 `tests/test_age_step_service.py`、`tests/test_age_gate_api.py`、`tests/test_account_erasure_service.py` 與暱稱步驟的測試（15 歲與 12 歲都抹除、生日與暱稱不在資料庫與日誌）。驗證：這些測試通過。
- [x] 1.3 `AccessPolicy` 的最低使用年齡情境：更新 `tests/test_access_policy.py`（由生日算出 15 歲 → `age_restricted`、剛滿 18 歲 → 通過、未完成確認 → `age_confirmation_required` 的順序不變），並確認 `app/services/access_policy.py` 沒有程式變更（只靠最低使用年齡自然拒絕，見 design D7）。驗證：測試通過，`git diff --stat` 沒有 `access_policy.py`。
- [x] 1.4 年齡事件與日誌：`tests/test_security_events.py` 與 `tests/test_log_scan_full_flow.py` 涵蓋「未滿 18 歲被抹除」只有事件名稱與內部編號、日誌掃描找不到生日、區間、暱稱與 email。驗證：測試通過。（第 2 批：完整流程新增 12 歲與 17 歲兩位被抹除者，掃描樣本含生日、17 歲的區間代碼、暱稱與 email；`test_security_events` 新增 `account.underage_erased`、`log_age_rejection` 的兩個事件與可就地修正的欄位錯誤不記錄的測試。）

## 2. 資料庫欄位、遷移與紀錄

- [x] 2.1 `app/models/user.py` 新增 `adult_declared_at`（`UTCDateTime`，可為空）與 `adult_declaration_version`（`Unicode(40)`，可為空）；新增遷移 `0019_adult_declaration`（`op.batch_alter_table`、ORM 型別、沒有原生 SQL；downgrade 用 `recreate="never"`；檔頭說明 expand 階段與回復限制，同 0017 的格式）。驗證：`tests/test_migrations.py`（升級、降級、再升級、每個欄位可為空）通過。
- [x] 2.2 `UserRepository.confirm_age`（見 design D5）：不再寫 `is_adult_at_confirmation`、`content_tier_preference` 固定為 `safe`、移除 `is_adult` 參數與呼叫端；`tests/test_user_repositories.py` 與 `tests/test_age_step_service.py` 更新（18 歲整與 25 歲確認後偏好都是 `safe`、失敗時沒有偏好、已完成者再次提交不改紀錄、`is_adult_at_confirmation` 為空）。驗證：測試通過。
- [x] 2.3 同步 `docs/project資料結構文件.md`（`users` 逐欄字典與欄位數、ERD 若有、固定值、migration 歷程）與第 14 節最下方追加一列 `0019` 的異動紀錄（日期與 migration 檔名一致），並更新 `docs/資料模型-data-model.md` 的欄位說明與 migration 歷程。驗證：`pytest tests/test_project_db_schema_doc.py tests/test_design_docs.py` 通過。

## 3. Google 登入的年滿 18 歲聲明

- [x] 3.1 `GoogleSignInService`（`app/services/google_sign_in_service.py`）：`begin` 多收 `declare_adult`（必須嚴格為 `true`）並放進簽章的交易 cookie；`_resolve` 在沒有帳號的分支先檢查條款、再檢查聲明，缺聲明以新的 `ERROR_ADULT = "adult_required"` 拒絕並記錄 `auth.google_rejected.adult`（新增於 `app/observability/security_events.py`）；建立帳號時寫入 `adult_declared_at` 與 `adult_declaration_version`（取伺服器時鐘與 `AgePolicy.version`）；回訪與合併既有帳號的路徑不檢查。驗證：更新 `tests/test_google_sign_in_service.py`（兩項都勾、缺條款、缺聲明、兩者都缺的優先順序、回訪者、管理員合併、舊交易 cookie 沒有聲明旗標）通過。
- [x] 3.2 `app/controllers/auth_api_controller.py` 的 `/api/auth/google/start` 只讀 `accept_terms` 與 `declare_adult` 兩個布林，忽略任何時間、版本、生日與年齡區間欄位；`app/controllers/auth_page_controller.py` 的 `LOGIN_ERROR_MESSAGES` 加 `adult_required` 的繁體中文提示。驗證：更新 `tests/test_google_endpoints.py` 與 `tests/test_auth_pages.py`（請求帶假的時間或版本被忽略、`/login?error=adult_required` 顯示提示、純文字渲染）通過。
- [x] 3.3 記錄聲明的日誌掃描：`tests/test_log_scan_full_flow.py` 的完整流程（註冊、年齡確認、登出、再登入）涵蓋新的拒絕事件，日誌沒有 email、Google 識別、授權碼與任何個資。驗證：測試通過。（第 2 批：完整流程新增「勾條款、沒勾聲明」的新帳號，確認 `auth.google_rejected.adult` 出現、導向 `/login?error=adult_required`、沒有建帳號，其 email、Google 識別與授權碼不在日誌與指令輸出。）

## 4. 登入頁、年齡確認頁與帳號設定頁

- [x] 4.1 `app/templates/auth/login.html` 增加第二個勾選框「我確認已滿 18 歲」並說明本服務僅限滿 18 歲者使用；`app/static/js/auth/auth-flows.js` 的 `googleStartBody` 送 `declare_adult`、Google 按鈕在未勾選時仍可按（回訪者）、`adult_required` 的提示、`finally` 恢復。驗證：更新 `tests/js/auth-flows.test.js`（請求本文、兩個勾選的組合、錯誤代碼對應文案）與 `tests/test_auth_pages.py`，`node --test` 該檔通過。（第 3 批：`googleStartBody(acceptTerms, declareAdult, next)` 只送兩個布林與 `next`；範本與腳本不寫死年齡數字（既有的 `test_no_template_or_script_hardcodes_an_age_number` 掃描 `templates/` 與 `static/js/auth/`），勾選框與說明用 Vue 插值 `ageRules.minRegisterAge`，`login.js` 多回傳 `ageRules`；`adult_required` 的提示文案是 3.2 已完成的伺服器固定文字（`LOGIN_ERROR_MESSAGES`），前端不另存一份；`finally` 恢復沿用 `Auth.useForm`。`test_auth_pages` 新增兩個勾選框恰好兩個、勾選框關聯到說明、按鈕不依勾選而禁用、請求本文不含時間或版本的測試。）
- [x] 4.2 `app/templates/auth/age.html` 加「本服務僅限滿 18 歲者使用」的提示，`age_not_eligible` 的說明改為未滿 18 歲；`app/templates/auth/account.html` 與 `app/static/js/auth/account.js` 移除 `ageGroup === 'minor'` 的安全模式提示區塊與相關狀態（`/api/auth/me` 的 `age_group` 欄位保留，見 design D6）。驗證：更新 `tests/test_auth_pages.py` 與 `tests/js/account-lifecycle.test.js`（沒有未成年提示、年齡確認頁有提示）通過。（第 3 批：年齡確認頁的提示改為獨立的 `#adults-only-note`；`age_not_eligible` 的說明文字本來就由伺服器依 `AgePolicy` 組出（1.1 已改為 18），前端只顯示它，所以只更新 `auth-flows.test.js` 的樣本並確認 `final` 結果沒有 `clear`（不暗示重填）。帳號頁移除 `safe-mode-title` 區塊與 `ageGroup` 狀態，順帶把管理員補填區塊裡「只用於年齡確認與內容分級」改為「只用於年齡確認與去識別的統計」（與隱私權政策一致）。`tests/js/account-lifecycle.test.js` 只涵蓋刪除與匯出的純函式，與未成年提示無關，**沒有需要修改的內容**；未成年提示與年齡確認頁提示的檢查在 `test_auth_pages.py` 與 `test_age_page.py`。）
- [x] 4.3 前端規範檢查：`pytest tests/architecture/test_frontend_rules.py tests/test_fonts_and_static.py`（沒有 `v-html`、`data-bs-*`、外部資源）與 `node --test tests/js/auth-flows.test.js tests/js/account-lifecycle.test.js` 通過。驗證：這些測試全數通過。（第 3 批：上述兩個 pytest 檔與兩個 Node 檔 42 項全數通過；另跑整個 `tests/js/*.test.js` 1100 項通過。）

## 5. 條款與隱私權政策

- [x] 5.1 `app/services/legal.py` 提高 `CURRENT_TERMS_VERSION`；`app/templates/legal/terms.html` 載明僅限滿 18 歲、註冊時須聲明已滿 18 歲且如實聲明並自行承擔謊報的責任、未滿 18 歲者不得使用且帳號與資料立即刪除，移除「13 歲以上」「13 至 17 歲僅限安全模式」；`app/templates/legal/privacy.html` 的生日用途拿掉「內容分級」並把未滿 13 歲改為未滿 18 歲（仍標示草稿；不寫露骨內容與供應商留存，見 design D8）。驗證：更新 `tests/test_legal.py` 與 `tests/test_legal_pages.py`（新舊字樣、版本字串、頁面與註冊記錄的版本一致）通過。（第 3 批：版本 `2026-11-16-draft-7` → `2026-11-18-draft-8`。條款第 2 節改為「僅限滿 N 歲者使用」、新增一條「註冊時須聲明您已滿 N 歲，且聲明必須據實……謊報年齡者須自行承擔因此產生的一切責任」並說明記錄聲明的時間與年齡政策版本，N 全由 `min_age` 帶入；刪除兩處 `{% if min_age < adult_age %}` 的未成年說明（含第 5 節的「固定使用安全模式」與「年齡上限」）。隱私權政策的生日用途改為「①年齡確認；②以去識別的統計方式了解用戶群」。「未滿 N 歲」本來就由 `min_age` 帶入，1.1 之後自動是 18。`tests/test_legal.py` 不需要修改（它只驗證版本字串合法與年齡政策版本）；`test_legal_pages`、`test_auth_pages`、`test_age_page` 更新。）

## 6. 匯出、測試玩家與其他腳本

- [ ] 6.1 `app/services/personal_export_service.py` 的帳號資料加 `adult_declared_at` 與 `adult_declaration_version`（JSON 與 CSV，沒有紀錄時為空值）。驗證：更新 `tests/test_personal_export_service.py`（有聲明、沒有聲明的管理員）通過。
- [ ] 6.2 `scripts/mint_test_sessions.py` 產生的測試玩家同時寫入聲明的時間與版本；`tests/test_mint_test_sessions.py` 驗證產生的欄位與真實註冊一致。驗證：測試通過。
- [ ] 6.3 全專案搜尋仍以舊流程假設的程式與腳本：`scripts/verify_container.py`、`scripts/staging_smoke.py`、`loadtest/`、`scripts/acceptance_*.py` 中只帶 `accept_terms` 的發起請求、「13 歲」的假設與 `is_adult` 的呼叫，逐一更新並以對應測試（`tests/test_staging_smoke.py` 等）驗證。驗證：`grep` 沒有殘留，相關測試通過。
- [ ] 6.4 客服提示詞的平台資料（2026-11-18 第 3 批後由使用者決定併入第 4 批）：`app/services/cs_prompt.py` 的 `_platform_facts` 改為如實描述新流程——登入頁有兩個勾選框（同意服務條款與隱私權政策、我確認已滿最低年齡；第一次登入都必須勾選，回訪不必）、生日與年齡區間「僅供年齡確認」（拿掉「內容分級」）；**刪除**「`{min_use_age} 至 {adult_age - 1} 歲可以使用，但只能使用安全模式…」那一句**（預設 18／18 時它會變成「18 至 17 歲」並送進 Gemini 的 system_instruction；全站只限滿最低年齡者，沒有可使用的未成年區間）**，數字仍取自 `DEFAULT_AGE_POLICY`。同步新增 `specs/cs-nlp-contract/spec.md` 的 delta：MODIFIED「客服的平台資料如實描述 Google 登入」（主規格仍寫「首次登入須勾選同意服務條款與隱私權政策」與「未滿 13 歲者不得使用」；MODIFIED 須保留既有情境名稱），並在 `proposal.md` 的 Impact 補上這個能力。驗證：更新 `tests/test_cs_prompt.py`（兩個勾選、沒有「內容分級」與「至 … 歲可以使用」；自訂年齡政策時數字跟著變且不會產生下界大於上界的區間；既有的 Google-only 與禁用字樣檢查照舊）通過，`test_cs_*` 其餘測試通過，`openspec validate adult-only-signup --strict` 通過。

## 7. 部署到展示環境與驗收（使用者在場）

- [ ] 7.1 依 `aws_demo.py` 的流程（先用 `--tag` 明確指定新映像標籤）：建立部署前手動快照、對 RDS 執行遷移到 `0019`、部署兩個池、`verify`。驗證：`verify` 通過，公開 `/healthz` 與 `/readyz` 回 200，資料庫版本為 `0019`；舊版程式在新結構上可運作（回復時不需降級資料庫）。
- [ ] 7.2 Google 登入的人工驗收（使用者本人，登入為人工）：①新帳號只勾條款、不勾聲明 → 不建立帳號並顯示提示；②兩項都勾 → 進年齡確認頁；③填 15 歲的生日 → 帳號被抹除並回登入頁、同一信箱可重新註冊；④填 25 歲 → 通過、預設為安全模式、首次贈幣 200；⑤回訪者不勾任何項目一鍵登入。驗證：依序看到上述結果，並由日誌（只看事件名稱）確認 `auth.google_rejected.adult`、`age_not_eligible` 與 `auth.age_confirmed` 的事件各出現；驗後抹除這些驗收帳號，管理員保留。
- [ ] 7.3 記錄驗收結果到 README 的驗收紀錄，並向使用者說明「新玩家預設為安全模式」的展示差異（見 design 風險）。驗證：README 新增本 change 的驗收列。

## 8. 整體驗證與文件

- [ ] 8.1 全套 `pytest` 與 `node --test` 通過；確認沒有改動 `AccessPolicy` 的內容上限、三種內容模式、遊戲相關程式與 `AGENTS.md`：以 `git diff --stat` 對照本 change 開始前的提交檢查。驗證：兩組測試都通過，差異清單符合預期。
- [ ] 8.2 更新 docs/ 對應的設計文件（對照表見 docs/README.md）：`帳號與安全-auth-security.md`（年齡規則、登入頁兩個勾選、聲明紀錄、未滿 18 歲抹除）、`前端結構-frontend.md`（登入頁與帳號頁）、`營運與部署-operations.md`（遷移 `0019` 與展示環境的部署與驗收紀錄）、`路線圖-roadmap.md`（本 change 的狀態、`safe-mode-toggle` 的待辦、遷移編號順延）；並讓 `tests/test_design_docs.py` 通過。驗證：`pytest tests/test_design_docs.py tests/test_readme.py` 通過。

## Workflow follow-up

- 先於 2026-11-23 部署到展示環境（2026-11-24、11-25 要展示全部功能）；之後再 `/openspec-propose safe-mode-toggle`。
- 封存本 change 後，更新 `docs/路線圖-roadmap.md` 的狀態與日期，並複查各設計文件標頭的「對應 change」。
