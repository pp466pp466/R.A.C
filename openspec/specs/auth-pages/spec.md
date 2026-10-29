# auth-pages Specification

## Purpose
定義會員相關頁面的使用者體驗：登入、註冊、忘記與重設密碼、信箱驗證與帳號設定，如何呈現狀態、回報錯誤與保護使用者，並遵守專案的離線前端與電影感深色風格規範。

## Requirements

### Requirement: 頁面清單與存取規則
系統 SHALL 提供下列頁面：`/login`、`/register`、`/forgot-password`、`/reset-password`、`/verify-email`、`/account`、`/terms`、`/privacy`。`/account` SHALL 需要登入；已登入的使用者開啟 `/login` 或 `/register` SHALL 被導向 `/account`。忘記密碼、重設密碼與信箱驗證頁在信箱驗證關閉時 SHALL 回應 404。所有頁面 SHALL 繼承基礎樣板，只載入本站資源。

#### Scenario: 未登入開啟帳號設定
- **WHEN** 未登入者開啟 `/account`
- **THEN** 導向 `/login`，且登入後返回 `/account`

#### Scenario: 已登入開啟登入頁
- **WHEN** 已登入者開啟 `/login`
- **THEN** 導向 `/account`

#### Scenario: 驗證關閉時的相關頁面
- **WHEN** 信箱驗證關閉，開啟 `/forgot-password`
- **THEN** 回應 404 的繁體中文頁面

### Requirement: 登入與註冊表單
登入與註冊頁 SHALL 提供電子郵件與密碼欄位（註冊另有顯示名稱、同意條款勾選（連到 `/terms` 與 `/privacy`）、生日欄位與年齡區間下拉選單；年齡區間的選項與提示來自伺服器提供的年齡規則），欄位 SHALL 帶有正確的自動填入屬性。註冊頁 SHALL NOT 再有「我已年滿 18 歲」的勾選。送出期間 SHALL 禁用送出按鈕並顯示處理中狀態，無論成功、失敗或中斷，SHALL 在 `finally` 恢復可操作狀態。錯誤訊息 SHALL 以繁體中文顯示在欄位旁或 Toast，內容一律以純文字渲染。Google 登入啟用時，頁面 SHALL 顯示 Google 入口，並與同意條款勾選、生日與年齡區間連動。

#### Scenario: 送出中
- **WHEN** 使用者送出登入表單且伺服器尚未回應
- **THEN** 送出按鈕被禁用並顯示處理中

#### Scenario: 伺服器回應失敗
- **WHEN** 伺服器回應 401
- **THEN** 顯示通用的錯誤訊息，按鈕恢復可用，密碼欄位被清空

#### Scenario: 網路中斷
- **WHEN** 請求因網路失敗或逾時而中止
- **THEN** 以 Toast 顯示無法連線的提示，按鈕恢復可用

#### Scenario: 欄位錯誤
- **WHEN** 伺服器回應 400 與各欄位的錯誤說明
- **THEN** 錯誤顯示在對應欄位旁，內容以純文字呈現

#### Scenario: 未勾選同意就按 Google
- **WHEN** 使用者尚未勾選同意條款就按下 Google 入口
- **THEN** 不離開本站，提示須先同意條款

#### Scenario: 未確認年滿 18 歲就按 Google
- **WHEN** 使用者尚未填生日或選年齡區間就按下 Google 入口
- **THEN** 不離開本站，提示須先填寫缺少的那一項

#### Scenario: 註冊時只勾選其中一項
- **WHEN** 使用者在註冊頁只同意條款而沒有填生日與年齡區間
- **THEN** 錯誤顯示在缺少的生日與區間欄位旁，而不是同意條款旁

#### Scenario: 註冊頁沒有年齡勾選
- **WHEN** 訪客開啟註冊頁
- **THEN** 頁面有生日欄位與年齡區間下拉選單，沒有「我已年滿 18 歲」的勾選

#### Scenario: 資料有誤
- **WHEN** 伺服器回應「資料有誤，請重新註冊」
- **THEN** 頁面顯示提醒並回到註冊頁的初始狀態：生日與年齡區間被清空，電子郵件與顯示名稱保留，密碼清空，使用者可立即以同一個信箱重新填寫並送出，不需要更換信箱

#### Scenario: Google 入口的資料有誤
- **WHEN** 使用者按下 Google 入口時，生日與年齡區間不一致
- **THEN** 頁面顯示相同的提醒、不離開本站，並清空生日與年齡區間，使用者可立即重新填寫

#### Scenario: 未滿 13 歲
- **WHEN** 伺服器回應不符合註冊資格
- **THEN** 頁面顯示說明且清空生日，不暗示可以改填其他值

### Requirement: 註冊完成的畫面
註冊成功後，頁面 SHALL 顯示伺服器回應的通用訊息，並提供前往登入的連結；SHALL NOT 顯示或暗示該信箱是否已存在。信箱驗證開啟時 SHALL 提示至信箱收信。

#### Scenario: 註冊成功與信箱已存在
- **WHEN** 分別以新信箱與已存在的信箱註冊
- **THEN** 兩種情況下畫面完全相同

### Requirement: 信箱驗證與重設密碼的確認頁
`/verify-email` 與 `/reset-password` SHALL 從網址片段讀取權杖，讀取後立即從網址列移除，且 SHALL NOT 在頁面載入時自動完成動作；使用者需按下確認（重設頁另需輸入新密碼）才送出 POST。缺少權杖時 SHALL 顯示說明與回到登入的連結。完成後 SHALL 顯示結果並提供下一步連結。

#### Scenario: 開啟驗證連結
- **WHEN** 使用者開啟含權杖片段的驗證連結
- **THEN** 頁面顯示確認按鈕，網址列的片段被移除，尚未送出任何完成動作的請求

#### Scenario: 缺少權杖
- **WHEN** 使用者直接開啟 `/verify-email`（沒有片段）
- **THEN** 顯示說明與回到登入的連結

#### Scenario: 驗證成功
- **WHEN** 使用者按下確認且伺服器回應成功
- **THEN** 顯示已完成並提供登入連結

### Requirement: 帳號設定頁
`/account` SHALL 顯示會員的顯示名稱、email、會員等級的繁體中文標籤（一般、進階、尊榮）、信箱驗證狀態與年齡確認狀態；提供變更密碼（僅在已設定密碼時）、登出目前裝置與登出所有裝置。信箱驗證開啟且尚未驗證時 SHALL 顯示提示與重寄驗證信按鈕（顯示頻率限制的結果）。尚未完成年齡確認時 SHALL 說明確認前無法使用 AI 功能，並提供生日與年齡區間的補填表單，成功後以 Toast 回報並更新狀態。13 至 17 歲的使用者 SHALL 看到目前為安全模式的提示。頁面 SHALL NOT 顯示生日與年齡區間。登出所有裝置與變更密碼 SHALL 先以自製 Modal 讓使用者確認。

#### Scenario: 顯示等級標籤
- **WHEN** 等級為 `plus` 的使用者開啟 `/account`
- **THEN** 頁面顯示「進階」

#### Scenario: 登出所有裝置的確認
- **WHEN** 使用者按下登出所有裝置
- **THEN** 先顯示自製 Modal 說明影響，確認後才送出請求

#### Scenario: 待驗證的提示
- **WHEN** 信箱驗證開啟且尚未驗證的使用者開啟 `/account`
- **THEN** 頁面顯示尚未驗證的提示與重寄按鈕

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
