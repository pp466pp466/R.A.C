# Design

## Context

動機與範圍見 `proposal.md`；行為契約見 `specs/onboarding-wizard`（新）與 `specs/auth-pages`（修改）。決定的來源是 `docs/RPG.md` 7.17.2、7.17.3（2026-11-10 對競品截圖的取捨，使用者未異議；2026-11-23 使用者再次提供截圖並確認「按照我們的設計，只是把登入後的設定改成分步精靈」）。

現況（已讀程式確認）：

- `/age`（`app/templates/auth/age.html`、`app/static/js/auth/age.js`）是單頁表單：生日、年齡區間、暱稱，一個「確認」按鈕；`Auth.useForm` 管送出中與欄位錯誤，`Auth.ageOutcome(info)` 把錯誤代碼對應成處理方式（`age_not_eligible`、`age_mismatch`、欄位錯誤、401、`account_locked`），這些都在 `auth-flows.js`（UMD，有 Node 測試）。
- 年齡確認走 `POST /api/auth/verify-age`（`birth_date`、`age_range`、`nickname`，全有或全無）。**待確認年齡的玩家只能呼叫它、登出與查詢自己**：`require_login()` 預設拒絕受限登入狀態（403 `age_confirmation_required`），由 `tests/test_restricted_session.py` 掃描所有路由守住。
- 故事偏好是既有端點 `PUT /api/account/story-preference`（值 `all`、`male`、`female`，冪等）與 `/account` 的設定；它不要求通過年齡閘門，但要通過 `require_login()`，所以待確認年齡的玩家呼叫會被 403。
- `/login`（`login.html`、`login.js`）有兩個勾選框，送出時是兩個獨立的旗標進簽章的交易 cookie。
- 前端有自製的 `ui-modal`（`ui-components.js`，Vue 狀態控制、已處理焦點與 `ui-modal-open`）。
- 規範：AGENTS.md 第 10 節「暱稱與筆名」要求暱稱與年齡確認在同一步驟（同一次提交）、同受 24 小時期限；第 2 節禁止 Bootstrap JS 與 `data-bs-*`；第 8 節繁體中文。**這個 change 不違反也不需要修訂任何一條。**

## Goals / Non-Goals

**Goals:**
- 把登入後的設定拆成三個一次一件事的步驟，並把故事偏好帶進流程，讓新玩家在第一次進入目錄之前就有合理的排序。
- 精靈邏輯是純函式加依賴注入的流程控制器，可用 Node 完整測試；後端零改動。
- 錯誤回到對的步驟，不讓玩家在最後一步面對一個看不出是哪裡出錯的訊息。

**Non-Goals:**
- 不做頭像、性別、通知權限、圖片類型多選、行銷同意（見 proposal）。
- 不新增後端端點、欄位、遷移，不放寬受限登入狀態。
- 不記錄「已確認使用守則」；不在精靈中途保存任何資料（沒有草稿、沒有續填）。
- 不改 `/account` 的管理員補填流程。

## Decisions

### D1. 三步：歡迎、年齡確認、故事偏好
排成三步而非兩步，因為參考畫面是三段進度條，且每步只放一類資訊最清楚：第 1 步暱稱加使用守則（對應參考圖的歡迎頁），第 2 步生日與區間（取代參考圖的人設步驟；人設屬創角，已由 `rpg-entry-ui` 完成），第 3 步取向（對應參考圖的類型步驟，只做取向）。
替代案：把生日放進第 1 步、只做兩步——否決：第 1 步會同時有守則 Modal、暱稱與生日，而生日與區間是最容易填錯的欄位，獨立一步才好放提示與錯誤。

### D2. 步驟是資料驅動的清單；流程控制器是依賴注入的純模組
新增 `app/static/js/auth/onboarding.js`（UMD，與 `auth-flows.js` 同風格）：
- `STEPS`：`[{ id: 'welcome' }, { id: 'age' }, { id: 'preference' }]`；進度條段數、「第 N 步，共 M 步」、「上一步」與「下一步／開始」的判斷都由清單長度與索引算出，新增一步（例如喜歡的類型）只需加一項與對應樣板。
- `canAdvance(stepId, values, acknowledged)` → `{ ok, reason }`：`welcome` 要暱稱去頭尾後非空且已確認守則；`age` 要生日與區間都有；`preference` 永遠可以。`reason` 是固定的繁體中文說明。**不檢查暱稱格式**（長度、網址、電話、署名只由伺服器判斷，避免前後端兩份規則；代價是格式錯誤要到最後才知道，由 D4 的「回到第 1 步」補償）。
- `stepForFailure(outcome)`：把 `Auth.ageOutcome` 的結果對應到步驟：欄位錯誤含 `nickname` → `welcome`；含 `birth_date` 或 `age_range`，或 `age_mismatch` → `age`；其他留在 `preference`。
- `createOnboarding({ http, navigate, destination, delay })`：封裝送出順序。`http.post`、`http.put`、導向函式與計時器都由外部注入，所以 Node 測試不需要瀏覽器：先 `post('/api/auth/verify-age', body)`；成功後若偏好是 `male` 或 `female`（不是預設的 `all`）才 `put('/api/account/story-preference', …)`，其失敗或超過 5 秒（任何情形）只吞掉不拋出；最後 `navigate(destination)`。模組不接受也不存取任何儲存區，測試以讀原始碼證明。
`age.js` 只剩 Vue 接線（步驟索引、欄位、Modal 開關、焦點）。

### D3. 故事偏好在年齡確認成功之後才存
受限登入狀態呼叫 `story-preference` 會被 403，而放寬它等於修改「待確認年齡的玩家只能完成年齡確認與登出」這條規格與守門測試——不值得為一個可選的偏好這樣做。所以順序固定為：確認成功 → 偏好 → 導向。偏好失敗不阻擋導向（玩家已通過確認，卡在這裡沒有意義），代價是偶爾沒存成功，玩家可到 `/account` 設定；因為導向之後頁面就沒了，不另外顯示失敗訊息。預設值（全讀者）不發請求，因為資料庫缺值本來就視為全讀者（`normalize_audience`），多一個請求沒有任何好處。

### D4. 錯誤回到對的步驟，且不清空沒出錯的欄位
沿用 `Auth.ageOutcome` 的分類，只加上「回到哪一步」：暱稱錯誤 → 第 1 步、錯誤顯示在暱稱欄位、生日與區間保留；`age_mismatch` → 第 2 步、清空生日與區間、通用說明；`age_not_eligible`、401、`account_locked` 與現在相同（結束或導向）。回到某一步之後，玩家要重新按「下一步」才會再往前（欄位值還在，所以只是幾次點擊）。

### D5. 使用守則 Modal 與「已確認」只在前端
內容是 AGENTS.md 第 10、11 節與 `/terms` 既有規則的白話摘要（見 specs；不新增規範）：滿 18 歲、暱稱公開、勿輸入真實個資、AI 處理與供應商可能留存、四類絕對禁止的內容、內容為虛構、連到條款與隱私權政策。文字寫在樣板裡（固定常數，不來自玩家或伺服器），頁面測試斷言關鍵語句存在，並與 `/terms` 對照不互相矛盾。「已確認」是 Vue 響應式變數：**不送伺服器、不寫任何瀏覽器儲存**，因為記錄它要新增欄位、遷移、匯出與刪除路徑（`terms_version` 已涵蓋條款同意），與「改版只動畫面」的範圍衝突；若日後需要稽核，另開 change。它因此只是「請你讀過」的引導，不是可被查證的同意，規格與使用者說明都不得把它說成法律上的同意紀錄。

### D6. 不保存、不續填
精靈不寫 `localStorage`／`sessionStorage`／IndexedDB／cookie，因為內容含生日（個資，AGENTS.md 第 11 節）；重新整理就從第 1 步開始。24 小時期限不變。替代案：用 `sessionStorage` 保存步驟與非敏感欄位——否決：收益很小（精靈只有三步），而且「瀏覽器儲存中沒有個資」是現有的可驗證性質，不值得為此放寬。

### D7. 登入頁「全部同意」
`login.js` 新增一個衍生的總開關：讀取時是兩個旗標的 AND，寫入時同時設定兩個旗標（Vue 的可寫入 computed）。送出時仍只讀兩個原本的旗標，所以交易 cookie 的內容與伺服器契約不變。

### D8. 版面、焦點與動畫
沿用 `auth.css` 的卡片與按鈕樣式，新增進度條（三段、已填滿用琥珀色 `#FF6B35`，未填用 `#2A2D34`）、守則列、取向選項（三個互斥的選項卡，選取時邊框與勾號）。步驟切換後把焦點移到該步的標題（`tabindex="-1"`），`aria-live="polite"` 宣告「第 N 步，共 M 步」；`prefers-reduced-motion` 取消步驟的淡入。配色不使用純白。圖示若有，用內嵌 SVG。

### D9. 測試策略
Node：`onboarding.js` 的步驟與進度、`canAdvance` 各步各情境、`stepForFailure`、`createOnboarding` 的送出順序（含偏好失敗被吞掉、預設不呼叫、`verify-age` 失敗不呼叫偏好、重複點擊只一次）與「儲存區沒有被寫入」（注入假 `storage` 並斷言零呼叫）；`login` 的總開關（衍生狀態雙向）。pytest：`age.html`／`login.html` 的樣板內容（沒有 `v-html`、`data-bs-*`、外部網址；守則關鍵語句；載入新腳本；`/age` 的存取規則不變）。實機：無頭 Chrome 在 1280 與 360 寬度逐步截圖。後端沒有改動，所以中間批次不跑全套 pytest。

## Risks / Trade-offs

- [暱稱格式錯誤要到最後才知道] → 錯誤回到第 1 步並保留其他欄位，玩家只需改暱稱再點兩次；若日後覺得太煩，可新增一個「檢查暱稱」的唯讀端點（需要新的後端與限流，不在這次）。
- [偏好請求失敗而玩家不知道] → 偏好預設值本來就合理，`/account` 隨時可改；不為此阻擋導向。
- [「已確認使用守則」被誤解為法律同意] → 文案與規格都標明它只是引導；條款同意仍以登入頁的勾選與 `terms_version` 為準。
- [步驟增加而放棄率升高] → 三步都很輕；每步只有必要欄位；無法量測放棄率（不新增分析），列為觀察項目。
- [24 小時期限內走不完] → 精靈不延長也不縮短期限；三步比單頁多幾次點擊，不改變實質難度。
- [重寫 `age.js` 的回歸風險] → 錯誤對應仍用既有的 `Auth.ageOutcome`，只新增「回到哪一步」；既有 `auth-flows.test.js` 全部保留。

## Migration Plan

純前端：沒有遷移、沒有設定、沒有資料變更。部署用既有的 `aws_demo.py deploy`（展示環境的部署另案，需使用者同意）。回復：舊版頁面在同一個後端上照常運作（契約不變）。

## Open Questions

- 使用守則 Modal 的白話文案：先依 specs 的七點起草，驗收時由使用者過目；內容屬法務覆核範圍（與 `/terms`、`/privacy` 同列），若法務有意見只改文字。
- 第 3 步的選項文字：沿用 `/account` 的「全讀者、男頻、女頻」（與目錄一致），而不是參考圖的「男性向、女性向、全部」；若使用者偏好參考圖的字樣，只改文字。
- 精靈各步的小標與歡迎文案（繁體中文，自寫）：實作時起草，驗收時調整。
