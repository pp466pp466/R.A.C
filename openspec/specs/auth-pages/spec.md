# auth-pages Specification

## Purpose
定義會員相關頁面的使用者體驗：只有 Google 與條款勾選的登入頁、年齡確認頁、管理員的密碼驗證頁與申訴頁、管理員的重設密碼確認頁，以及帳號設定頁，如何呈現狀態、回報錯誤與保護使用者，並遵守專案的離線前端與電影感深色風格規範。

## Requirements



### Requirement: 頁面清單與存取規則
系統 SHALL 提供下列頁面：`/login`、`/login/password`、`/age`、`/appeal`、`/reset-password`、`/account`、`/terms`、`/privacy`；`/register` 只作導向。`/forgot-password` 與 `/verify-email` SHALL NOT 存在（回應 404）。`/account`、`/age` SHALL 需要登入；已登入的使用者開啟 `/login` SHALL 被導向預設頁面（見「登入後的預設頁面與首頁導向」）。`/reset-password` 只服務管理員的重設流程，需要寄信設定（寄信器與 `BASE_URL`），缺少時 SHALL 回應 404。所有頁面 SHALL 繼承基礎樣板，只載入本站資源。

#### Scenario: 未登入開啟帳號設定
- **WHEN** 未登入者開啟 `/account`
- **THEN** 導向 `/login`，且登入後返回 `/account`

#### Scenario: 已登入開啟登入頁
- **WHEN** 已登入的一般玩家開啟 `/login`
- **THEN** 導向預設頁面

#### Scenario: 已移除的頁面
- **WHEN** 任何人開啟 `/forgot-password` 或 `/verify-email`
- **THEN** 回應 404 的繁體中文頁面

#### Scenario: 驗證關閉時的相關頁面
- **WHEN** 沒有寄信設定，開啟 `/reset-password`
- **THEN** 回應 404 的繁體中文頁面

### Requirement: 帳號設定頁
`/account` SHALL 顯示會員的顯示名稱、email、會員等級的繁體中文標籤（一般、進階、尊榮）、信箱驗證狀態與年齡確認狀態；提供變更密碼（僅在已設定密碼時，實際上只有管理員）、登出目前裝置與登出所有裝置。頁面 SHALL NOT 提供重寄驗證信。尚未完成年齡確認時 SHALL 說明確認前無法使用 AI 功能，並提供生日與年齡區間的補填表單（管理員的補填不含暱稱），成功後以 Toast 回報並更新狀態。13 至 17 歲的使用者 SHALL 看到目前為安全模式的提示。頁面 SHALL NOT 顯示生日與年齡區間。已完成年齡確認的帳號 SHALL 看到目前的暱稱與短代號，並有設定或更改暱稱的表單（沒有暱稱的帳號 SHALL 看到「創作劇本前請先設定暱稱」的說明，以及暱稱會公開、請勿使用真實姓名的提示），成功以 Toast 回報；頁面 SHALL 提供進入 `/scripts` 的連結；被暫停發佈的玩家 SHALL 另看到說明與「前往申訴」按鈕（見 `appeal-shortcut`）。登出所有裝置與變更密碼 SHALL 先以自製 Modal 讓使用者確認。一般會員的頁面 SHALL 另提供「匯出我的資料」（JSON 與 CSV 兩個按鈕）與「刪除帳號」；刪除帳號 SHALL 先以自製 Modal 說明 7 天寬限與不可復原的範圍並建議先匯出，已設定密碼者在 Modal 內輸入密碼，其餘輸入確認文字。管理員的頁面 SHALL NOT 顯示刪除帳號。

#### Scenario: 顯示等級標籤
- **WHEN** 等級為 `plus` 的使用者開啟 `/account`
- **THEN** 頁面顯示「進階」

#### Scenario: 登出所有裝置的確認
- **WHEN** 使用者按下登出所有裝置
- **THEN** 先顯示自製 Modal 說明影響，確認後才送出請求

#### Scenario: 待驗證的提示
- **WHEN** 信箱驗證時間為空的遺留帳號開啟 `/account`
- **THEN** 頁面沒有「尚未驗證」的提示與重寄按鈕

#### Scenario: 尚未確認年滿 18 歲的提示
- **WHEN** 尚未完成年齡確認的使用者開啟 `/account`
- **THEN** 頁面顯示確認前無法使用 AI 功能的說明與生日、年齡區間的補填表單

#### Scenario: 補確認成功
- **WHEN** 使用者提交一致的生日與年齡區間，伺服器回應成功
- **THEN** 以 Toast 回報，頁面顯示已完成，表單消失

#### Scenario: 沒有勾選就按確認
- **WHEN** 使用者沒有填生日或沒有選區間就按下確認按鈕
- **THEN** 顯示須先填寫的提示，且不送出請求

#### Scenario: 未成年的安全模式提示
- **WHEN** 13 至 17 歲的使用者開啟 `/account`
- **THEN** 頁面顯示目前為安全模式及其原因，且不顯示生日

#### Scenario: 不顯示生日
- **WHEN** 已完成年齡確認的使用者開啟 `/account`
- **THEN** 頁面與其初始狀態都沒有生日與年齡區間

#### Scenario: 匯出
- **WHEN** 使用者按下匯出的 JSON 或 CSV 按鈕
- **THEN** 瀏覽器下載對應格式的檔案，按鈕在請求期間停用並於結束後恢復

#### Scenario: 刪除帳號的確認
- **WHEN** 使用者按下刪除帳號
- **THEN** 顯示自製 Modal，說明 7 天寬限、刪除的範圍與建議先匯出；未輸入密碼或確認文字之前確認鈕不送出請求

#### Scenario: 刪除成功後
- **WHEN** 刪除申請成功
- **THEN** 頁面導向登入頁，並說明帳號已進入 7 天寬限期

#### Scenario: 管理員沒有刪除帳號
- **WHEN** 管理員開啟 `/account`
- **THEN** 頁面不顯示刪除帳號

#### Scenario: 沒有暱稱的既有帳號
- **WHEN** 沒有暱稱的既有玩家開啟 `/account`
- **THEN** 頁面顯示請先設定暱稱的說明與表單

#### Scenario: 更改暱稱
- **WHEN** 玩家提交合格的新暱稱，伺服器回應成功
- **THEN** 以 Toast 回報，頁面顯示新的暱稱與不變的短代號

#### Scenario: 被暫停發佈
- **WHEN** 被暫停發佈的玩家開啟 `/account`
- **THEN** 頁面顯示只有送審被暫停的說明與連到 `/appeal` 的「前往申訴」按鈕

### Requirement: 轉址的安全
登入後的返回位置（`next`）SHALL 只接受本站內以單一 `/` 開頭的路徑，SHALL 拒絕外部網址、`//` 開頭、含反斜線或控制字元的值，不合法時 SHALL 退回預設頁面。轉址的判斷 SHALL 在伺服器端完成，並以頁面初始狀態提供給前端。

#### Scenario: 外部網址
- **WHEN** 以 `/login?next=https://evil.example/` 開啟並登入
- **THEN** 登入後導向預設頁面，而不是外部網址

#### Scenario: 協定相對網址
- **WHEN** `next` 為 `//evil.example/x`
- **THEN** 被視為不合法

#### Scenario: 站內路徑
- **WHEN** `next` 為 `/account`
- **THEN** 登入後導向 `/account`

### Requirement: 遵守前端規範
所有頁面 SHALL 只使用 Vue 3 全域變數模式與離線 axios，SHALL NOT 使用 `.vue`、打包工具、Bootstrap 的 JavaScript、`data-bs-*` 互動屬性（`data-bs-theme="dark"` 除外）或 `v-html`；Modal 與 Toast 的顯示 SHALL 完全由 Vue 狀態接管。頁面配色與字型 SHALL 使用專案的色彩規範與本機字型。表單 SHALL 可由鍵盤完整操作，欄位 SHALL 有關聯的標籤，錯誤提示 SHALL 可被輔助技術讀出。頁面初始狀態 SHALL 以安全的 JSON 區塊傳遞，不以字串拼接進腳本。

#### Scenario: 靜態掃描
- **WHEN** 對新增的樣板與前端檔案執行既有的前端規範掃描
- **THEN** 檢查通過

#### Scenario: 鍵盤操作
- **WHEN** 只使用鍵盤操作登入表單
- **THEN** 可依序聚焦欄位並以 Enter 送出

#### Scenario: 響應式
- **WHEN** 在寬度 360px 與 1280px 檢視各頁
- **THEN** 版面不出現水平捲軸，表單可正常使用

### Requirement: 待刪除帳號的帳號設定頁
持有受限登入狀態（帳號為 `deletion_pending`）者開啟 `/account` 時，頁面 SHALL 只顯示預定刪除的時間、「撤銷刪除」按鈕與登出，SHALL NOT 顯示變更密碼、匯出、年齡補填或其他設定。撤銷成功後 SHALL 以 Toast 回報並重新載入為一般的帳號設定頁。

#### Scenario: 只有撤銷與登出
- **WHEN** 待刪除帳號的使用者登入後開啟 `/account`
- **THEN** 頁面顯示預定刪除時間、撤銷按鈕與登出，沒有其他設定區塊

#### Scenario: 撤銷
- **WHEN** 使用者按下撤銷刪除且伺服器回應成功
- **THEN** 以 Toast 回報並顯示完整的帳號設定頁

#### Scenario: 頁面開著時寬限期剛好過去
- **WHEN** 使用者在頁面開著的期間寬限期過去，按下撤銷，伺服器回應 `grace_expired`
- **THEN** 頁面顯示無法撤銷的說明，按鈕恢復

#### Scenario: 寬限期已過後開啟頁面
- **WHEN** 寬限期已過、排程尚未執行，使用者開啟 `/account`
- **THEN** 頁面說明帳號即將刪除、無法再撤銷，沒有撤銷按鈕，只剩登出

### Requirement: 隱私權政策如實描述資料權利
`/privacy` 的資料權利說明 SHALL 如實描述已提供的功能：匯出的內容與格式、刪除帳號的 7 天寬限與撤銷方式、抹除的範圍、用量紀錄僅匿名化保留，以及各類資料的保存期限。頁面 SHALL NOT 再出現「功能將於後續版本提供」之類的敘述。政策的版本字串 SHALL 隨內容更新。

#### Scenario: 不再註記尚未提供
- **WHEN** 使用者開啟 `/privacy`
- **THEN** 資料權利說明中沒有「將於後續版本提供」的字樣，並描述匯出與刪除帳號

#### Scenario: 保存期限與實作一致
- **WHEN** 檢查 `/privacy` 的保存期限表
- **THEN** 對話、用量紀錄、失敗原文、驗證權杖與管理稽核的期限與清除指令使用的數值一致

### Requirement: 登入頁只有 Google 與條款勾選
`/login` SHALL 是登入與註冊合一的唯一入口，只有「使用 Google 登入」按鈕與一個「同意服務條款與隱私權政策」的勾選框（連到 `/terms` 與 `/privacy`），SHALL NOT 有電子郵件、密碼、顯示名稱、生日或年齡區間欄位。Google 按鈕 SHALL 在勾選框未勾選時仍可按下（回訪者不必勾選）；按下後 SHALL 禁用按鈕並顯示處理中，無論成功、失敗或中斷 SHALL 在 `finally` 恢復可操作狀態。錯誤訊息（含被導回時的固定代碼，例如未勾選條款）SHALL 以繁體中文顯示，內容一律以純文字渲染。Google 登入未啟用時頁面 SHALL 顯示服務暫時無法使用的說明。

#### Scenario: 頁面內容
- **WHEN** 訪客開啟 `/login`
- **THEN** 頁面只有 Google 登入按鈕與條款勾選框，沒有任何密碼或年齡欄位

#### Scenario: 新使用者忘記勾選
- **WHEN** 沒有帳號的使用者沒勾條款就完成 Google 授權
- **THEN** 被導回 `/login` 並提示須先同意條款，沒有建立帳號

#### Scenario: 處理中
- **WHEN** 使用者按下 Google 按鈕且伺服器尚未回應
- **THEN** 按鈕被禁用並顯示處理中，失敗後恢復可用

### Requirement: 年齡確認頁
`/age` SHALL 需要登入，提供生日欄位、年齡區間下拉選單（選項與提示來自伺服器提供的年齡規則）與暱稱欄位（必填），並提示「暱稱會公開顯示於您創作的劇本，請勿使用真實姓名」；送出期間禁用按鈕、結束時（`finally`）恢復。成功後 SHALL 導向登入前要去的位置（不合法時為預設頁面）。待確認年齡的玩家 SHALL 被導向此頁；已完成年齡確認的玩家開啟此頁 SHALL 導向預設頁面。未滿 13 歲被抹除、不一致、格式錯誤的訊息 SHALL 是通用的繁體中文說明，不指出是哪個欄位不一致，也不暗示可以改填其他值；暱稱不合格 SHALL 顯示在暱稱欄位且不清空生日與年齡區間。

#### Scenario: 補填成功
- **WHEN** 待確認年齡的玩家在 `/age` 提交一致的生日與區間以及合格的暱稱
- **THEN** 導向預設頁面

#### Scenario: 已完成者開啟
- **WHEN** 已完成年齡確認的玩家開啟 `/age`
- **THEN** 導向預設頁面

#### Scenario: 不一致
- **WHEN** 伺服器回應 `age_mismatch`
- **THEN** 頁面顯示通用的「資料有誤」提醒並清空生日與區間，使用者可立即重新填寫

#### Scenario: 未滿 13 歲
- **WHEN** 伺服器回應 `age_not_eligible`
- **THEN** 頁面顯示說明並回到 `/login`，帳號已不存在

#### Scenario: 暱稱不合格
- **WHEN** 伺服器回應暱稱欄位的錯誤
- **THEN** 錯誤顯示在暱稱欄位，生日與年齡區間保持原樣，使用者可只修改暱稱再送出

#### Scenario: 公開顯示的提示
- **WHEN** 玩家開啟 `/age`
- **THEN** 頁面顯示暱稱會公開、請勿使用真實姓名的提示

### Requirement: 管理員密碼驗證頁與申訴頁
`/login/password` SHALL 只接受持有管理員中間狀態的請求（其餘導向 `/login`），提供密碼欄位與「忘記密碼」按鈕（寄信設定缺少時不顯示）。`/appeal` SHALL 需要登入且只對被鎖定或被暫停發佈的玩家有內容（其餘導向預設頁面），提供申訴文字欄位、字數提示、提交後的處理狀態。兩頁 SHALL 遵守前端規範（純文字渲染、`finally` 恢復、自製 Toast）。

#### Scenario: 沒有中間狀態
- **WHEN** 沒有管理員中間狀態的請求開啟 `/login/password`
- **THEN** 導向 `/login`

#### Scenario: 被鎖定者的申訴頁
- **WHEN** 被鎖定的玩家開啟 `/appeal`
- **THEN** 看到申訴表單與已提交申訴的處理狀態

#### Scenario: 被暫停發佈者的申訴頁
- **WHEN** 被暫停發佈但沒有被鎖定的玩家開啟 `/appeal`
- **THEN** 看到申訴表單與已提交申訴的處理狀態

#### Scenario: 兩者皆非
- **WHEN** 既未被鎖定也未被暫停發佈的玩家開啟 `/appeal`
- **THEN** 導向預設頁面

### Requirement: 登入後的預設頁面與首頁導向
登入後的預設頁面 SHALL 由單一常數決定（目前為 `/game`，也就是選擇劇本的首頁，不直接進入聊天；客服 `/cs` 改由導覽列進入）。已登入者開啟 `/` 或 `/login` SHALL 被導向預設頁面（受限登入狀態各導向自己的頁面：待確認年齡 → `/age`，被鎖定 → `/appeal`，等待刪除 → `/account`）；`/register` SHALL 一律導向 `/login`。未登入者開啟 `/` 仍看到首頁。

#### Scenario: 已登入者開啟首頁
- **WHEN** 已登入的一般玩家開啟 `/`
- **THEN** 導向 `/game`

#### Scenario: 受限狀態開啟首頁
- **WHEN** 待確認年齡的玩家開啟 `/`
- **THEN** 導向 `/age`

#### Scenario: 舊的註冊網址
- **WHEN** 任何人開啟 `/register`
- **THEN** 導向 `/login`

#### Scenario: 完成年齡確認之後
- **WHEN** 新玩家完成年齡確認與暱稱，且沒有指定返回位置
- **THEN** 導向 `/game`

#### Scenario: 客服仍可直接開啟
- **WHEN** 已登入的玩家直接開啟 `/cs`
- **THEN** 顯示客服頁面，不被導向 `/game`

### Requirement: 重設密碼的確認頁
`/reset-password` SHALL 從網址片段讀取權杖，讀取後立即從網址列移除，且 SHALL NOT 在頁面載入時自動完成動作；使用者需輸入新密碼並按下確認才送出 POST。缺少權杖時 SHALL 顯示說明與回到登入的連結。完成後 SHALL 顯示結果並提供回到登入的連結。頁面用語 SHALL 以管理員為對象（不再說明玩家的註冊或信箱驗證）。

#### Scenario: 開啟重設連結
- **WHEN** 管理員開啟含權杖片段的重設連結
- **THEN** 頁面顯示新密碼欄位與確認按鈕，網址列的片段被移除，尚未送出任何完成動作的請求

#### Scenario: 缺少權杖
- **WHEN** 使用者直接開啟 `/reset-password`（沒有片段）
- **THEN** 顯示說明與回到登入的連結

#### Scenario: 重設成功
- **WHEN** 管理員輸入合規的新密碼並按下確認，伺服器回應成功
- **THEN** 顯示已完成並提供回到登入的連結
