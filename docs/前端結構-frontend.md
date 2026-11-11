# 前端架構

> 狀態：已實作（`/game` 入口與存檔頁外殼已上線；開存檔精靈〔樣板、純函式、API 封裝與 Vue 掛載的 `wizard.js`〕已實作但尚未在瀏覽器驗收；遊戲主畫面為規劃中） ｜ 最後更新：2026-11-11 ｜ 對應 change：rpg-entry-ui、story-preference、game-wallet、rpg-script-authoring、remove-player-password-auth、platform-foundation、member-auth、cs-chat-core、usage-limits、admin-console、privacy-lifecycle、google-first-login ｜ 規範依據：AGENTS.md 第 1、2、5、6、7、10 節
> 注意（2026-11-03）：本文件已納入 change ① `google-first-login` 的前端變化（登入頁只有 Google 與條款、新增 `/age`、`/appeal`、`/login/password`、移除註冊頁、管理員後台新增「被鎖定的帳號」與「申訴」）與 change ② `remove-player-password-auth`（刪除 `forgot-password`、`verify-email` 的頁面與腳本；`reset-password` 只服務管理員；帳號頁沒有「尚未驗證」提示與重寄按鈕）。

本文件說明前端的資產載入、共用元件、會員頁面、客服頁面、RPG 入口與管理員後台的模組關係，以及客服的離線暫存與同步。伺服器端的對應流程見 [客服對話-chat-service.md](客服對話-chat-service.md) 與 [帳號與安全-auth-security.md](帳號與安全-auth-security.md)。

## 1. 資產與載入順序

```mermaid
flowchart TD
    Base["base.html<br/>lang zh-Hant-TW、data-bs-theme dark、meta csrf-token"]
    Base --> Css["CSS：bootstrap.min.css → fonts.css → theme.css → components.css → 頁面的 CSS"]
    Base --> Vendor["vendor：vue.global.prod.js → axios.min.js"]
    Base --> Ui["共用：typewriter.js → toast-store.js → restriction.js → ui-components.js"]
    Ui --> Page{"頁面"}
    Page -->|"會員頁面"| Auth["auth-common.js → auth-form.js → auth-flows.js → 各頁腳本<br/>（login、age、appeal、login-password、account…；account 另先載入 account-lifecycle.js）"]
    Page -->|"/cs"| Cs["auth-common.js → cs-sidebar.js → cs-store.js → cs-sync.js → cs-usage.js → chat.js"]
    Page -->|"/admin"| Adm["auth-common.js → admin-format.js → admin.js（另載入 admin.css）"]
    Page -->|"/scripts"| Scr["auth-common.js → script-draft.js → milestone-preview.js → script-api.js → script-workshop.js（另載入 scripts.css）"]
    Page -->|"/game"| Gm["auth-common.js → game-labels.js → game-api.js → entry.js（另載入 game.css）"]
    Page -->|"/game/start/版本"| Wzd["auth-common.js → game-labels.js → wizard-state.js → game-api.js → wizard.js"]
```

- 第三方資產（Vue、Bootstrap CSS、axios、字型）全部在 `static/vendor`、`static/fonts`，**不使用 CDN**；字型為 Noto Sans TC（108 個 `unicode-range` 切片的 woff2）。
- 範本一律用 `url_for('static', …)` 引用；不使用 `.vue`、打包工具、`v-html`、外部彈窗套件，也不使用 Bootstrap 的 JavaScript 與 `data-bs-*`（唯一例外是 `data-bs-theme="dark"`）。`tests/architecture/test_frontend_rules.py` 掃描範本、自有 CSS/JS 與實際渲染出來的頁面。
- 色彩只在 `theme.css` 定義一次（主背景 `#0F1012`、文字 `#E2E4E9`、次要 `#8A8F99`、焦點 `#FF6B35`、分隔線 `#2A2D34`），嚴禁純白。

## 2. 共用元件

| 檔案 | 內容 | 測試 |
|---|---|---|
| `js/ui/ui-components.js` | 自製 Modal（左上角狀態碼、焦點陷阱、Esc 關閉）與 Toast 容器（右下角、由下往上淡入、左側 2px 琥珀線），以及受限狀態提示 `UiRestrictionNotice`（`ui-restriction-notice`：說明文字加指向 `/appeal` 的「前往申訴」一般連結）；顯示由 Vue 響應式狀態接管 | 瀏覽器驗收、`tests/js/script-workshop-template.test.js`（範本編譯） |
| `js/ui/toast-store.js` | Toast 的狀態庫（與框架無關） | `tests/js/toast-store.test.js` |
| `js/ui/restriction.js` | 申訴捷徑的判定與文字（純函式，`UI.restrictionOf`、`UI.restrictionNoticeOf`、`UI.APPEAL_PATH`）：API 回應 403 且代碼為 `account_locked` 或 `publish_suspended`，或 `/api/auth/me` 的 `restriction === 'locked'`／`publish_suspended === true`，才回傳 `locked` 或 `suspended`；其他一律 `null`。不記住任何狀態，所以解除後重新整理即消失 | `tests/js/restriction.test.js`、`tests/test_appeal_shortcut.py` |
| `js/ui/typewriter.js` | 打字機緩衝區：以字素為單位、積壓越多越快、`finish()` 後加速補完、可 `flush()` 與 `cancel()` | `tests/js/typewriter.test.js` |
| `js/auth/auth-common.js` | CSRF 標頭、初始狀態讀取、錯誤整理（`describeError`）、登入導向 | `tests/js/auth-common.test.js` |
| `js/auth/auth-flows.js` | 登入、年齡確認、申訴與管理員密碼驗證頁的判斷邏輯（純函式，`Auth.*`）：安全的導向位置、Google 發起的請求內容（只有條款旗標與 `next`）、檢查輸入、每種錯誤代碼的處理結果（顯示欄位錯誤、清空欄位、導向、結束流程）、申訴字數以 Unicode 字元計 | `tests/js/auth-flows.test.js`（22 項） |
| `js/auth/account-lifecycle.js` | 帳號頁匯出與刪除的純函式（`Auth.lifecycle`）：確認文字比對、送出條件、檔名、預定刪除時間（台灣時間）、錯誤代碼說明、Blob 錯誤整理 | `tests/js/account-lifecycle.test.js` |

## 3. 客服頁面（`/cs`）的模組關係

```mermaid
flowchart LR
    Html["templates/cs/chat.html<br/>外殼 + initial-state JSON"] --> Chat
    Chat["chat.js<br/>Vue 3 應用：畫面狀態與事件"]
    Chat --> Sidebar["cs-sidebar.js<br/>開關規則（純函式）"]
    Chat --> Sync["cs-sync.js<br/>同步引擎"]
    Chat --> Usage["cs-usage.js<br/>剩餘輪數與暫停時間（純函式）"]
    Chat --> Tw["typewriter.js<br/>新回覆逐字顯示"]
    Chat --> Ui["ui-components.js<br/>Modal 與 Toast"]
    Sync --> Store["cs-store.js<br/>IndexedDB 或記憶體"]
    Sync --> Api["createAxiosApi → /api/cs/*（含 /api/cs/usage）"]
    Chat -.->|"getUsage"| Api
    Sidebar --> Ls["localStorage（讀寫皆包 try/catch）"]
    Html -.->|"request_timeout_ms、chat_gate、user"| Chat
```

- **逾時不寫死**：送出訊息的 axios 逾時只讀初始狀態的 `request_timeout_ms`（伺服器的總預算 + 5 秒）；後端預算一改，前端自動跟著改。
- **純文字**：所有使用者與助理的文字都以 `{{ }}` 輸出並以 CSS `white-space: pre-wrap` 保留換行，不解析任何標記語言。
- **Sidebar**：寬 280px；992px 以上預設開啟並推擠主區域、狀態記在 `localStorage`；未滿 992px 預設關閉、以覆蓋方式顯示並附遮罩、不記住狀態、選擇對話後自動關閉。
- **chips**：最後一則助理訊息帶建議動作、打字機結束、其後沒有任何新訊息時才顯示（最多 4 個）；點擊後立即移除並以其文字送出。
- **閘門與 503**：`chat_gate` 不是 `null` 時顯示說明（需補填年齡時附前往帳號設定的連結）並停用輸入；服務暫時無法使用時保留已輸入的文字為待送。
- **用量**（`GET /api/cs/usage`，邏輯在 `cs-usage.js`）：Sidebar 底部一行顯示「今日剩餘 N 輪」（用完時「今日額度已用完」），每則回覆或被拒絕之後重新讀取；**取得失敗或離線就整行隱藏**，其他功能不受影響，因為真正的限制永遠在後端。暫停期間橫幅顯示恢復時間（台灣時間，固定 UTC+8，不依賴 `Intl`）並停用輸入與「新對話」，到點由計時器自動恢復，不必重新整理。
- **用量類的拒絕**：每分鐘限制（`rate_limited`）與全站忙碌（`system_busy`）是暫時的，訊息保留為待送、稍後自動重送，並以 toast 說明原因（不是「無法連線」）；今日額度用完、對話暫停、對話數已滿（`daily_quota_exceeded`、`chat_suspended`、`conversation_limit_reached`）短時間內不會成功，訊息標記為無法送出、**不自動重送**並顯示伺服器的說明，使用者可以重試或移除。對話數已滿時，離線建立的對話不可能建立成功，會連同其中的待送一起移除，原因以 toast 說明，輸入的文字留在輸入框。

## 3a. 管理員後台（`/admin`）的模組關係

```mermaid
flowchart LR
    Html["templates/admin/console.html<br/>外殼 + initial-state JSON（只有管理員的顯示名稱）"] --> Admin
    Admin["admin.js<br/>Vue 3 應用：八個分頁（含被鎖定的帳號與申訴）、確認視窗、對話全文視窗"]
    Admin --> Fmt["admin-format.js<br/>台灣時間、中文標籤、用量百分比與警示等級（純函式）"]
    Admin --> Ui["ui-components.js<br/>Modal 與 Toast"]
    Admin --> Common["auth-common.js<br/>CSRF 標頭、describeError、登入導向"]
    Admin --> Api["axios → /api/admin/*"]
    Admin -.->|"401"| Login["導向 /login?next=/admin"]
```

- **六個分頁**（依管理員的使用順序）：總覽、被標記對話、意見回饋、使用者、模型失敗、稽核。總覽最上面是可點擊的「今日警告」，點它切到被標記對話；被標記對話與意見回饋的每一列有「看對話」與「看使用者」；「使用者」分頁只有標明「供客服反應時使用」的信箱查詢，沒有使用者清單。
- **只有純文字**：使用者、模型與管理員填寫的原因一律以 `{{ }}` 輸出，對話內容以 `white-space: pre-wrap` 保留換行；`<script>` 與 `<img onerror>` 字樣逐字顯示，不被解析。
- **確認視窗共用一個**：停權與刪除對話顯示原因欄位（必填、200 字、提醒勿填個人資料），解除停權與調整等級只有密碼（調整等級另有等級選單，不含目前的等級）。密碼欄位 `type=password`、`autocomplete="off"`，**送出的當下與視窗關閉時立即清空**，不存進任何儲存體；密碼錯誤的訊息顯示在密碼欄位下方。同時只開一個視窗：從對話全文按「刪除對話」會先關掉全文視窗，視窗關閉時全文也從記憶體丟掉。
- **調整遊戲幣（game-wallet）**：「使用者」分頁的使用者檢視多一列「遊戲幣」（免費與付費餘額）與「調整遊戲幣」按鈕（目標是管理員或待刪除者時不顯示）。確認視窗多兩個欄位：餘額種類（付費或免費，預設付費）與增減量（不為 0 的整數，扣除用負數；`admin-format.js` 的 `parseCoinDelta` 解析、`bucketLabel` 提供繁體中文標籤），加上必填的原因與管理員密碼，送到 `POST /api/admin/users/<id>/wallet/adjust`；欄位錯誤（`bucket`、`delta`）顯示在對應欄位，餘額不足（409）顯示在視窗內。玩家端的錢包與記錄畫面屬 `player-hub`，目前沒有。
- **載入策略**：進入分頁時才載入一次，「重新整理」才重讀；做了會改變資料的操作後，相關分頁標記為過期、下次進入才重新載入。模型失敗每次讀取都會寫稽核，所以不因切換分頁而重複讀取。清單以 `limit=50` 分頁，「載入更多」依已載入的筆數作為起始位置。
- **錯誤處理**：`Auth.describeError` 整理後端的錯誤；401 導向登入；欄位錯誤（`password`、`reason`、`tier`）顯示在對應欄位，其餘顯示在視窗內；視窗已被關掉時改用 Toast。
- **版面**：頂欄與分頁列與內容區左右對齊（最大寬度 64rem）；手機寬度時分頁列自己橫向捲動、頁面不會；清單的每一列改為直向排列；用量以長條加百分比與文字說明呈現，**不單靠顏色**（達 80% 長條為琥珀橙並寫明「已達 80% 告警線」）。

## 3a. 登入、年齡確認、申訴與管理員密碼驗證頁（google-first-login）

```mermaid
flowchart LR
    Login["login.js<br/>Google 按鈕＋條款勾選框"] -->|"POST /api/auth/google/start<br/>只送 accept_terms 與 next"| Provider["導向 Google（https 才導向）"]
    Provider --> Callback["/auth/google/callback"]
    Callback -->|"新帳號"| Age["age.js<br/>生日＋年齡區間"]
    Callback -->|"被鎖定"| Appeal["appeal.js<br/>申訴＋自己的申訴狀態"]
    Callback -->|"管理員"| Pw["login-password.js<br/>密碼＋忘記密碼按鈕"]
    Callback -->|"其他"| Cs["/cs（預設頁面）"]
    Age -->|"POST /api/auth/verify-age"| Done["導向 next 或 /cs"]
    Age -.->|"age_not_eligible：帳號已抹除"| Final["只留說明與回登入頁的連結"]
    Pw -->|"POST /api/auth/admin-password"| Done
    Pw -.->|"忘記密碼：信中的連結"| Reset["reset-password.js<br/>新密碼＋確認（只服務管理員）"]
    Reset -->|"POST /api/auth/reset-password"| Back["前往登入"]
    Flows["auth-flows.js（純函式）"] -.-> Login
    Flows -.-> Age
    Flows -.-> Appeal
    Flows -.-> Pw
```

- **登入頁只有 Google 與條款**：沒有電子郵件、密碼、生日與年齡欄位，也沒有註冊與忘記密碼的連結；按鈕在條款沒勾選時仍可按（回訪的使用者不必勾選，新使用者沒勾由伺服器擋下並導回提示），只在送出中禁用。`/register` 與註冊頁的範本、腳本已移除。
- **年齡確認頁**：伺服器提供年齡規則（區間選項與範圍）放在初始狀態，沒有任何個人資料；`age_mismatch` 顯示通用說明並清空兩個欄位；`age_not_eligible`（帳號已抹除）只顯示說明與回登入頁的連結；登入逾時導向登入頁。生日只存在輸入框的響應式欄位，不寫入任何瀏覽器儲存體。
- **申訴頁**：字數提示（以 Unicode 字元計）、自己的申訴與處理狀態、已有待處理的申訴時不顯示表單；申訴內容一律純文字；沒有被鎖定時 API 回 404，頁面導向預設頁面。
- **管理員密碼驗證頁**：密碼在送出的當下立即清空；沒有或已過期的中間狀態導回 `/login?error=google_state`；「忘記密碼」只在伺服器說有寄信設定時顯示。
- **重設密碼確認頁**（`/reset-password`，`reset-password.js`，只服務管理員；玩家沒有密碼，也不會收到這類信）：權杖在信中連結的網址片段（`#token=…`），頁面讀取後立即從網址列移除，**載入時不自動送出任何動作**，使用者輸入新密碼（8 至 128 字）與確認、按下按鈕才 `POST /api/auth/reset-password`。兩次輸入不一致在前端擋下；成功後清空兩個欄位，只顯示結果訊息與「前往登入」；缺少權杖時顯示「連結不完整」的說明與回到登入的連結。沒有寄信設定（寄信器與 `BASE_URL`）時伺服器回 404，這個頁面不存在。
- 所有頁面都遵守前端規範：無 `v-html`、無 Bootstrap 的 JavaScript、處理中禁用按鈕並在 `finally` 恢復（`Auth.useForm`）。

## 3b. 帳號頁（`/account`）的匯出、刪除、撤銷與故事偏好（C-4c、story-preference）

```mermaid
flowchart LR
    Html["templates/auth/account.html<br/>兩份 HTML：一般會員／等待刪除的帳號<br/>（伺服器依狀態選擇，多餘的區塊不在頁面裡）"] --> Acc
    Acc["account.js<br/>Vue 3 應用"] --> Life["account-lifecycle.js<br/>確認文字、送出條件、檔名、台灣時間、錯誤說明（純函式）"]
    Acc --> Form["auth-form.js<br/>useForm：送出中、欄位錯誤、finally 恢復"]
    Acc --> Ui["ui-components.js<br/>Modal 與 Toast"]
    Acc --> Api["axios → /api/account/export、/delete、/cancel-deletion、/nickname、/story-preference"]
    Acc -.->|"刪除成功"| Login["導向 /login?notice=deletion_requested"]
    Acc -.->|"撤銷成功"| Reload["Toast 後重新載入 /account"]
```

- 年齡補填區塊只留給管理員（玩家沒完成年齡確認之前連這一頁都進不來，會被導向 `/age`）。
- **匯出**：`POST /api/account/export` 以 `responseType: 'blob'`、逾時 60 秒、`AbortController` 送出（離開頁面時中止），回應轉成物件網址以 `<a download>` 下載；無論成功、失敗或中斷，`finally` 恢復按鈕。失敗時錯誤本文也是 Blob，`unwrapBlobError` 先讀成 JSON 再交給共用的錯誤整理。
- **刪除**：自製 Modal（Vue 狀態接管）說明 7 天寬限、抹除範圍並建議先匯出；有密碼者輸入密碼，其餘輸入確認文字（文字由伺服器放進初始狀態，前端不寫死第二份）。**確認前不送出請求**；送出後密碼欄位立即清空；成功後所有裝置登出並導向登入頁，登入頁以固定列舉的代碼顯示寬限期說明。
- **等待刪除**：另一份 HTML 只有預定刪除時間（台灣時間）、撤銷與登出；寬限期已過但排程還沒執行時，不顯示注定失敗的撤銷按鈕；頁面開著時剛好過期則由前端處理 `grace_expired`（顯示說明、按鈕恢復）。登入頁與 Google 回呼在 `deletion_pending` 時一律去 `/account`，不依 `next`。
- **管理員**：頁面沒有刪除區塊（HTML 裡完全沒有，不是用 `v-if` 藏起來）；匯出照常提供。
- **故事偏好**（story-preference）：「暱稱與劇本創作」之後的區塊，與暱稱區塊同樣 `v-if="ageConfirmed"`，等待刪除者的那份 HTML 沒有。三個單選（全讀者、男頻、女頻；標籤是樣板裡的固定文字）綁定 `storyForm.values.story_preference`，初始值取自 `user.story_preference`（來自 `/api/auth/me` 的使用者資料，沒有生日與年齡區間；缺值或不認得的值退回全讀者）。`saveStoryPreference` 以 `Auth.useForm` 的 `run` 送出 `PUT /api/account/story-preference`：送出中按鈕禁用並標示忙碌、`finally` 恢復、重複點擊只送一次；成功 Toast，失敗以 Toast 或欄位錯誤說明且**不改動玩家目前的選取**（網路、逾時與 CSRF 的 Toast 由 `useForm` 顯示，不重複）；401 導向登入頁並保留位置，鎖定時顯示申訴捷徑。偏好不存進瀏覽器儲存空間。

## 3c. 劇本創作頁（`/scripts`，rpg-script-authoring）

```mermaid
flowchart LR
    Html["templates/scripts/workshop.html<br/>外殼加初始狀態：user、gate、submit_timeout_ms"] --> Ws
    Ws["script-workshop.js<br/>Vue 3 應用：劇本與版本列表、分區編輯器、Modal"] --> Draft["script-draft.js<br/>表單資料與送出內容互轉、字數、欄位路徑的中文名稱、讀者頻道的值域（純函式）"]
    Ws --> Prev["milestone-preview.js<br/>里程碑預覽：依觸發排序 純前端（純函式）"]
    Ws --> Api["script-api.js<br/>axios 封裝 /api/scripts/*"]
    Ws --> Ui["ui-components.js<br/>Modal、Toast、UiRestrictionNotice"]
    Api --> Srv["伺服器：唯一的驗證者 規則層 模型層"]
    Ws -.->|"403 publish_suspended 或 account_locked"| Rs["restriction.js<br/>顯示前往申訴按鈕"]
```

- **伺服器是唯一的驗證者**：前端只顯示字數與伺服器回傳的逐欄位錯誤（路徑轉成中文名稱），不自行擋下、不改寫使用者輸入；空白只在送出時修剪。
- **草稿衝突**：儲存帶開始編輯時的 `base_hash`，別處已更新時伺服器回 `draft_changed`，頁面以自製 Modal 讓作者選擇重新載入或保留自己的修改。
- **送審**：自製確認 Modal；axios 逾時取自初始狀態的 `submit_timeout_ms`（伺服器總預算加 5 秒，前端不寫死數字），以 `AbortController` 於離開頁面時中止，無論成功、失敗或中斷，`finally` 恢復按鈕。
- **被暫停發佈**：頁面載入時由初始狀態判定，送審被拒（403 `publish_suspended`）時即時判定；顯示 `UiRestrictionNotice` 並停用送審按鈕，編輯與儲存照常。
- **欄位說明與範例**：每個區塊最上方有一句用途說明，每個文字欄位都有範例（`placeholder`）；「狀態轉移規則」對玩家顯示為「世界規則（狀態轉移規則，選填）」並附白話說明。人工驗收發現原本玩家看不懂，所以 `tests/test_script_page.py` 要求任何文字欄位都必須有範例。
- **讀者頻道**（story-preference）：編輯器最下方（結局之後、字數統計之前）的可摺疊區塊，`open.audience` 預設展開；男頻、女頻、全讀者三選一，`fieldset` 隨 `canEdit` 禁用，所以待審與已發佈的版本顯示但不可改。`script-draft.js` 的 `toDraft` 與 `toContent` 共用 `audienceOf`：非三個列舉值的輸入（含舊版本沒有這個鍵）一律為全讀者，`toContent` 永遠送出 `audience`，全讀者時由伺服器決定不保存該鍵（雜湊相容）；帶入範例就是整份草稿換成範例的 `toDraft`，所以頻道回到全讀者。頻道不計入字數，伺服器的 `audience` 欄位錯誤顯示在單選旁。
- **個資**：劇本文字與暱稱只在記憶體與伺服器，不寫入 `localStorage`、`sessionStorage`、IndexedDB 或 cookie（`tests/test_script_page.py` 掃描）；所有文字以 `{{ }}` 純文字顯示。
- 欄位範例由 `GET /api/scripts/example` 帶入（內建首發劇本的標準化內容）；沒有種子資料時伺服器回 404，頁面在編輯器上方顯示一般的錯誤說明，原有內容不變。

## 3d. RPG 入口（`/game`，rpg-entry-ui）

```mermaid
flowchart LR
    Html["templates/game/index.html<br/>外殼加初始狀態：user（會員等級、故事偏好）、gate"] --> En
    En["entry.js<br/>Vue 3 應用：選劇本與我的存檔兩個分頁、載入更多、重試"] --> Labels["game-labels.js<br/>列舉值的中文對照、閘門與內容模式的提示、站內路徑、台灣時間（純函式）"]
    En --> Api["game-api.js<br/>axios 封裝 GET /api/sessions/catalog 與 GET /api/sessions"]
    En --> Ui["ui-components.js<br/>Toast 區、UiRestrictionNotice"]
    Api --> Srv["伺服器：GameEntryService 唯讀 不過年齡閘門"]
    En -->|"開始"| Start["/game/start/版本識別碼<br/>開存檔精靈 見下一小節"]
    En -->|"繼續"| Save["/game/存檔識別碼<br/>存檔頁空殼 伺服器直接渲染"]
    En -.->|"401"| Login["登入頁 帶 next=/game"]
    En -.->|"403 account_locked"| Rs["restriction.js<br/>顯示前往申訴按鈕"]
```

- **三個頁面、同一個前綴**：`/game` 是 Vue 應用；`/game/<存檔識別碼>` 是存檔頁空殼，由伺服器以 Jinja 直接渲染（標題與主角姓名自動跳脫、不用 `safe`），**沒有腳本與請求**，所以不會產生內容也不會扣遊戲幣；`/game/start/<版本識別碼>` 是開存檔精靈（見下一小節；版本識別碼只在網址裡，頁面內容不含它）。登入後的預設頁面是 `/game`（`DEFAULT_AFTER_LOGIN`），`/cs` 仍可直接開啟。
- **初始狀態只有 `user` 與 `gate`**：`user` 是會員等級與故事偏好（沒有 email、生日與年齡），`gate` 是 `AccessPolicy.chat_gate` 的結果（`null` 或 `age_confirmation_required`、`age_restricted`、`forbidden`），前端不自行判斷。**清單內容不在初始狀態裡**，由兩個唯讀端點取得（`tests/test_game_pages.py` 以劇本與存檔的標題證明它們不在 HTML 中）。損壞的初始狀態不會讓頁面壞掉，也不會憑猜測禁用任何東西。
- **載入規則**（`entry.js` 的 `loadNext`，兩個清單共用）：載入中再按不送第二次請求；位移依伺服器**實際回傳的筆數**前進（不是去重後的筆數），並以 `id` 去重（兩次請求之間若有新劇本上架，同一張卡可能再出現）；失敗時已載入的卡片不消失、位移不前進，所以重試取的是同一頁；`finally` 一律恢復載入狀態；空的一頁不算還有下一頁（避免壞回應造成無止境的載入）；取消的請求不顯示錯誤；401 導向登入頁並帶回 `/game`，403 `account_locked` 顯示申訴捷徑。預設分頁是「選劇本」，掛載時只載入它；「我的存檔」第一次切換時才載入，**首次載入失敗後不會因為再切換分頁而自動重試**，由重試按鈕負責。
- **提示**（`game-labels.js`）：後端只回列舉值，中文對照集中在這裡；未知值一律退回固定的預設文字，不顯示原始值（分級退回「未標示」，不把未知的分級當成安全或成人）。`gateNotice` 對未知的非空閘門代碼也當成「不可開始」（失敗時關閉）；閘門非空時「開始」按鈕禁用，但清單仍可瀏覽。`contentModeNotice` 只負責說明「為什麼沒看到某些劇本」：未滿成年門檻者固定為安全模式，只說明原因、沒有連結；成年但選了安全模式者提示可到帳號設定調整。**頁面上沒有任何改變內容模式的動作，也不自動調高**。後端列舉值與對照表是否一致，由 `tests/test_game_pages.py` 以後端常數比對（後端新增列舉值時會失敗）。
- **只讀與個資**：入口頁只呼叫兩個 GET 端點，不碰遊戲幣；作者與玩家的文字一律以 `{{ }}` 純文字顯示；不寫入 `localStorage`、`sessionStorage`、IndexedDB 或 cookie，也沒有 `console.log`（`tests/test_game_pages.py` 掃描）。
- **版面**：`game.css` 預設單欄，768px 以上兩欄；沒有固定寬度，長文字以 `overflow-wrap` 折行，360px 不橫向溢出；沿用深色琥珀配色，沒有純白。
- **導覽**：`/game` 頂列有「客服」「創作」「帳號」；`/scripts` 與 `/cs` 的頁首、`/account`（卡片頂端的小導覽；等待刪除者的版本沒有）各有一個回 `/game` 的「冒險」連結，都是一般站內連結、沒有 `data-bs-*`（各頁的測試與 `tests/test_game_pages.py` 檢查）。

### 開存檔精靈（`/game/start/<版本識別碼>`，rpg-entry-ui）

```mermaid
flowchart LR
    Html["templates/game/start.html<br/>初始狀態：member_tier、gate、suggest_timeout_ms、start_timeout_ms（沒有劇本內容）"] --> Wz
    Wz["wizard.js<br/>Vue 3 應用：步驟、三個自製 Modal、AbortController"] --> St["wizard-state.js<br/>純函式：草稿、字數、前進條件、示範氣泡、套用預覽、組開存檔本文、失敗訊息"]
    St --> Labels["game-labels.js<br/>審查類別的中文對照"]
    Wz --> Api["game-api.js<br/>setup、suggest、start：timeout 與 signal 由呼叫端傳入"]
    Api -->|"GET setup/版本"| S1["序章與創角資料"]
    Api -->|"POST protagonist/suggest"| S2["AI 預覽 無狀態 不建立存檔"]
    Api -->|"POST /api/sessions"| S3["開存檔 201 後導向 /game/存檔"]
```

- **單頁三步驟、狀態只在記憶體**：序章（唯讀的背景故事）、聯繫、創角在同一頁以 `step` 切換（`v-if`）；創角內容是個資，**不寫 `localStorage`、`sessionStorage`、IndexedDB 或 cookie，不放網址，重新整理就回到序章（刻意的）**。`game-api.js` 的三個新呼叫只有 `suggest`（無狀態的預覽）與 `start`（開存檔）是 POST，`tests/test_game_pages.py` 以程式碼掃描固定「寫入的呼叫恰好這兩個」。
- **初始狀態只有 `member_tier`、`gate` 與兩個逾時**：`gate` 與 `/game` 相同（`AccessPolicy.chat_gate`，前端不自行判斷）。`suggest_timeout_ms` = 後端單次請求總預算 + 5 秒（與 `/scripts` 的送審相同的算法，預設 35 秒）；`start_timeout_ms` = `2 × 後端單次請求總預算 + 5 秒`（預設 65 秒，與 `session_api_controller` 說明的一致）：開存檔最壞是「劇本審查（必要時）＋主角模型審查」兩次呼叫各用滿一份預算。兩者由 `game_page_controller` 從設定算出，前端與樣板都不寫死（`test_game_pages.py` 以不同的預算設定驗證）。**劇本內容不在初始狀態裡**，由 `GET /api/sessions/setup/<id>` 取得。
- **`wizard-state.js`（UMD 純函式，沒有請求、儲存、日誌與 DOM）**：草稿是一組字串欄位（角色卡七欄、來歷、選填的自我介紹）；聯繫有三態：未設定、已設定（`kind`、`ref`、`text`）、略過，另分出「選了一半」（選了對象沒寫、或寫了沒選對象）。`blockers`／`canAdvance` 只做「必要欄位已填、沒有超過長度」的即時回饋（上限取自 `setup` 的 `limits`，不寫死），**合規與審查全由伺服器判斷**；自我介紹超長只擋「請 AI 生成」，不擋開始（它不會隨開存檔送出）。`applySuggestion` 回傳新的 `{ draft, anchor }`：AI 預覽覆蓋角色卡與聯繫，保留玩家選的來歷與自我介紹，預覽殘缺時不用空白蓋掉玩家的內容。`buildStartPayload` 只搬運與去頭尾空白（看不懂的年齡原樣送出，讓伺服器指出欄位），略過聯繫時帶 `skip_anchor: true` 而不帶 `anchor`；沒有頭像與性別。`targetGroups` 的人物選項只取姓名與職業。
- **失敗訊息只來自固定文字與列舉對照，不含玩家文字**：`describeSetupFailure`、`describeSuggestFailure`、`describeStartFailure` 依狀態碼與代碼回傳 `{ kind, code, message, step, fields, link, retry }`；422 只列出認得的審查類別（不認得的識別字略過，不顯示原始值）；唯一原樣放進 `fields` 的是 400 的逐欄位規則說明（伺服器保證不回顯輸入），`stepOfField` 與 `fieldKeyOf` 把欄位路徑對應回步驟（`protagonist.*`、`intro` 在創角，`anchor.*` 在聯繫；兩步都有錯誤時回到較前面的步驟）。開始冒險逾時或斷線時說明伺服器可能已經完成，請玩家先到「我的存檔」確認再重送。所有失敗都保留玩家已填的內容。
- **樣板與 `wizard.js` 的約定**：樣板標頭列出 `wizard.js` 的 `setup()` 要回傳的名稱（狀態、計算、方法），`tests/js/wizard-template.test.js` 以離線 Vue 編譯樣板，並檢查樣板用到的名稱與這份清單完全一致（多一個或少一個都會失敗）。`wizard-template.test.js` 另以 `vm` 沙盒執行 `wizard.js` 的 `setup()`，檢查它實際回傳了清單的每一個名稱、不多不少；過渡用的「建置中」說明已隨 `wizard.js` 移除。
- **`wizard.js`（Vue 掛載）**：只負責狀態、請求的時機與 `finally` 的恢復，邏輯都在 `wizard-state.js`。掛載時才呼叫 `setup`（版本識別碼從網址最後一段讀，讀不出來就是「找不到」，不送請求）；載入有 404、403（分級高於目前模式、年齡確認、鎖定）與可重試失敗各自的畫面，看不懂的成功回應也當成可重試的失敗。往後的步驟要通過前進條件，往前（`back`、`goTo`）不受限；「請 AI 生成」在已有手動內容時先開覆蓋確認 Modal，成功只填表、更新今日剩餘並出 Toast；「開始冒險」同一時間只送一個請求，送出期間所有按鈕禁用，成功後導向 `/game/<存檔>`，**導向之前仍保持禁用**（開存檔沒有冪等鍵，連點會開出兩個存檔）；400 依欄位路徑跳回較前面的那一步，422 與其他失敗只顯示固定文字；`pagehide` 時三種請求（載入、生成、開存檔）的 `AbortController` 都中止，被中止的請求不顯示錯誤。沒有任何瀏覽器儲存、主控台輸出與網址參數；`tests/js/wizard.test.js` 在 `vm` 沙盒用儲存物件與主控台的陷阱斷言這一點。

## 4. 暫存、待送與同步

```mermaid
flowchart TD
    Open["開啟暫存<br/>名稱含使用者的對外識別碼"] --> Try{"IndexedDB 可用？"}
    Try -->|"是"| Idb["IndexedDB 後端<br/>conversations、messages、pending、meta"]
    Try -->|"否（隱私模式、被禁止、逾時）"| Mem["記憶體後端<br/>頁面照常可用，顯示安靜的提示"]
    Idb -->|"任何一次操作失敗（配額…）"| Mem
    Idb --> Domain["領域操作：依序號排序、待送依建立順序、以後端覆寫"]
    Mem --> Domain
```

```mermaid
sequenceDiagram
    participant U as 使用者
    participant V as chat.js
    participant Sy as 同步引擎
    participant St as 暫存
    participant Sv as 伺服器

    U->>V: 送出文字
    V->>Sy: enqueue(對話, 內容, 新的 client_msg_id)
    Sy->>St: 寫入待送（依建立順序）
    V->>Sy: flush(AbortController 的 signal)
    loop 每則待送（循序，同一時間只有一則）
        Sy->>Sv: POST messages（原本的識別碼）
        alt 成功
            Sv-->>Sy: user_message、assistant_message、conversation
            Sy->>St: 以回應取代待送、更新對話
        else 網路錯誤、逾時、5xx、429（含 rate_limited、system_busy）
            Sy->>St: 保留待送並停止本輪（10 秒後自動重試、連線恢復時重試）
        else daily_quota_exceeded、chat_suspended、conversation_limit_reached
            Sy->>St: 標記為無法送出並記下伺服器的說明（不自動重送），繼續下一則
        else 409 conversation_busy
            Sy->>Sy: 依序退避 1、2、4 秒後重試，仍忙則保留並停止
        else 401
            Sy->>V: 登入已失效，導向登入頁
        else 其他 4xx
            Sy->>St: 標記為無法送出（可重試或移除），繼續下一則
        end
    end
    V->>V: finally：恢復送出鈕、清除載入動畫
```

- **後端是權威**：訊息由後端先寫入並指派序號；本機只有可離線閱讀的快取與尚未同步成功的待送。衝突時以後端為準：`since_seq` 增量拉取、同序號覆寫、已有完整回覆的待送隨之移除。
- **中止不代表後端中止**：取消、切換對話、離開頁面只中止請求；後端仍會完成並寫入，之後以序號補齊（重送同一識別碼不會重複）。
- **離線**：只能閱讀暫存的歷史並保存待送訊息；產生新回覆需要連線。離線建立的對話以 `client_id` 冪等重送建立，成功後訊息與待送改掛正式識別碼。

## 5. 驗證方式

| 層級 | 內容 |
|---|---|
| Node 單元測試 | `tests/js/*.test.js`（劇本創作頁另有 `script-draft`、`milestone-preview`、`restriction` 與 Vue 範本編譯 `script-workshop-template`；帳號頁的故事偏好有 `account-story-preference`：以真的 Vue 與 `Auth.useForm` 在 `vm` 沙盒執行 `account.js` 的 `setup()`，axios、Toast 與 mount 為替身；RPG 入口有 `game-labels`〔對照表、未知值、閘門與內容模式的提示、路徑、台灣時間〕與 `entry`〔同樣以 `vm` 沙盒執行 `entry.js` 的 `setup()`：預設分頁、載入更多、失敗保留與重試、閘門禁用開始、401、樣板用到的名稱都由 `setup()` 回傳；每個測試都有逾時，被破壞的程式讓 Promise 永遠不完成時會失敗而不是掛住〕；開存檔精靈有 `wizard-state`〔純函式：草稿與聯繫三態、字數與前進條件、示範氣泡、套用預覽、開存檔本文的兩種形狀、欄位路徑對應步驟、各種失敗的固定訊息且不含玩家文字〕、`wizard-template`〔樣板以離線 Vue 編譯，用到的名稱與 `wizard.js` 的約定清單一致，`setup()` 實際回傳每一個名稱〕與 `wizard`〔以 `vm` 沙盒執行 `wizard.js`，沙盒在 `tests/js/support/wizard-sandbox.js`：載入的三種失敗與重試、聯繫的選擇與略過、示範氣泡不送請求、欄位上限禁用、生成預覽只填表與覆蓋確認、各種失敗保留內容、離開頁面時中止、重複點擊只送一次、導向前保持禁用、整個流程沒有儲存寫入與主控台輸出〕）：Sidebar 規則、暫存（記憶體與假 IndexedDB、開啟失敗退回記憶體）、同步引擎（假伺服器、重送不重複、退避、停止條件、用量代碼的歸類）、用量顯示（`cs-usage.test.js`）、打字機、Toast、共用函式、後台的顯示邏輯（`admin-format.test.js`：每個列舉值都有標籤、日界、比例邊界）；`tests/test_admin_format_labels.py` 比對前端標籤與後端的列舉 |
| 架構掃描 | `tests/architecture/test_frontend_rules.py`、`tests/test_cs_pages.py`、`tests/test_admin_page.py`、`tests/test_script_page.py`、`tests/test_game_pages.py`、`tests/test_appeal_shortcut.py`：離線規則、逾時不寫死、`v-cloak`、純文字、自製 Modal 與 Toast；`/admin` 只有外殼與管理員的顯示名稱 |
| 瀏覽器驗收 | Edge 無頭模式（CDP）的整頁驗收，結果與截圖摘要記在 README 的「瀏覽器驗收」與「管理員後台（C-4b）」 |

---

**何時更新本文**：新增或改名前端模組與頁面、載入順序、共用元件 API、Sidebar 規則、暫存資料集、同步引擎的停止與重試規則、前端規範改變時。`tests/test_design_docs.py` 會檢查 `static/js/cs/`、`static/js/ui/`、`static/js/admin/` 的每個檔案都出現在本文。
