# Tasks

> 分批原則（`openspec-batch-workflow`）：後端不改，所以中間批次只跑相關測試加全域守門子集；全套 pytest、全套 Node 與變異驗證只放最後一批（驗收前）。AGENTS.md 與規格不需修訂。

## 1. 純函式與流程控制器（批 1）

- [ ] 1.1 新增 `app/static/js/auth/onboarding.js`（UMD）：`STEPS`、進度與「第 N 步，共 M 步」的計算、`canAdvance(stepId, values, acknowledged)`（含固定的繁體中文 `reason`）、`stepForFailure(outcome)`；驗證：新增 `tests/js/onboarding.test.js` 涵蓋 `onboarding-wizard` 規格的「三個步驟與進度」「各步驟的可前進條件」，並有「在清單加第四步，進度與前進後退自動涵蓋」的測試
- [ ] 1.2 `onboarding.js` 加 `createOnboarding({ http, navigate, destination, storage })` 的送出流程：先 `verify-age`、成功後偏好不是 `all` 才 `story-preference`（失敗被吞掉）、最後導向；處理中旗標與重複點擊只送一次；驗證：Node 測試涵蓋「一次送出，成功之後才存故事偏好」「錯誤回到對應的步驟」的每個情境（含 `age_not_eligible`、401、`account_locked`、欄位錯誤對應步驟、偏好失敗不影響、`verify-age` 失敗不呼叫偏好），並以假 `storage` 斷言整個流程對儲存區零寫入
- [ ] 1.3 `auth-flows.js` 只在必要處補小修（若 `ageOutcome` 需要帶出欄位名稱供 `stepForFailure` 使用）；驗證：既有 `tests/js/auth-flows.test.js` 全部維持通過
- [ ] 1.4 跑 Node 測試與全域守門子集（`tests/architecture`、`test_service_errors`、`test_design_docs`、`test_readme`、`test_restricted_session`、`test_legal_pages`、`test_agents_skill_sync`）；驗證：通過，新檔沒有 CDN、`v-html`、Bootstrap JavaScript

## 2. 樣板、樣式與登入頁（批 2）

- [ ] 2.1 改寫 `age.html` 與 `age.js` 的接線：三段進度條與「第 N 步，共 M 步」、三個步驟的欄位、上一步與下一步或開始、使用守則列與自製 Modal（`ui-modal`，`v-if`、純文字、Esc 與關閉鈕、焦點進出）、取向三選項（預設全讀者）、處理中禁用、錯誤顯示與回到對應步驟、步驟切換後移焦點；全部以 `{{ }}` 渲染，沒有 `v-html` 與 `data-bs-*`；驗證：`tests/test_age_page.py` 更新後通過（樣板內容、守則關鍵語句、`/age` 的存取規則不變、載入新腳本），架構守門通過
- [ ] 2.2 `auth.css`：進度條（琥珀色與 `#2A2D34`）、步驟區、守則列、取向選項卡、`prefers-reduced-motion`；驗證：沒有純白、沒有外部資源，`tests/test_theme_css.py` 與 `test_fonts_and_static.py` 通過
- [ ] 2.3 `login.html` 與 `login.js` 加「全部同意」總開關（雙向衍生狀態，送出仍是兩個獨立旗標）；驗證：`tests/js/` 新增測試涵蓋 `auth-pages` 規格的五個情境，`tests/test_auth_pages.py` 更新後通過
- [ ] 2.4 無頭 Chrome（`/age` 用已登入的待確認年齡帳號，自有實例）在 1280 與 360 寬度逐步截圖並目視：每一步、守則 Modal、錯誤回到第 1 步與第 2 步、小螢幕沒有橫向捲軸、焦點與動畫；驗證：記錄結果給使用者
- [ ] 2.5 跑 Node、頁面測試與全域守門子集；驗證：通過

## 3. 整體驗證與文件（批 3）

- [ ] 3.1 變異驗證：對 `canAdvance`、`stepForFailure`、`createOnboarding`（送出順序、偏好只在非預設時呼叫、失敗被吞掉、對儲存區零寫入）與登入頁總開關的關鍵判斷做變異，設定測試逾時與行程逾時，在專案副本上執行；驗證：每個變異都被測試殺死（存活者以差分模糊測試分類為測試缺口或等價），專案原始碼未動
- [ ] 3.2 全套 pytest 與全套 Node 測試（背景執行、輸出存檔）；驗證：全部通過，記錄通過與略過的數量
- [ ] 3.3 `openspec validate onboarding-polish --strict`；驗證：通過
- [ ] 3.4 依 `deck-conflict-watch` 檢查是否與 `docs/2.0簡報.md`、六張圖解、`docs/4.0簡報.pptx` 衝突，只通知使用者、不自行修改；驗證：把衝突位置與選項列給使用者
- [ ] 3.5 更新 `docs/` 對應的設計文件（`docs/前端結構-frontend.md`、`docs/帳號與安全-auth-security.md`、`docs/路線圖-roadmap.md`、`README.md` 的頁面表與驗收紀錄），並讓 `tests/test_design_docs.py`、`tests/test_readme.py` 通過；驗證：`pytest tests/test_design_docs.py tests/test_readme.py` 通過

## Workflow follow-up

- 使用者驗收（含實機的步驟手感、守則文案、取向選項文字）通過後才封存。
- 封存後更新 `docs/路線圖-roadmap.md` 的狀態與日期。
- 後續（不在這個 change）：記錄「已確認使用守則」（需要欄位、遷移、匯出）、「喜歡的類型」多選（需要偏好標籤的資料結構）、暱稱的提早檢查端點。
