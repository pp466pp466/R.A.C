# Spec Delta

## MODIFIED Requirements

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
`/account` SHALL 顯示會員的顯示名稱、email、會員等級的繁體中文標籤（一般、進階、尊榮）、信箱驗證狀態與年齡確認狀態；提供變更密碼（僅在已設定密碼時，實際上只有管理員）、登出目前裝置與登出所有裝置。頁面 SHALL NOT 提供重寄驗證信。尚未完成年齡確認時 SHALL 說明確認前無法使用 AI 功能，並提供生日與年齡區間的補填表單，成功後以 Toast 回報並更新狀態。13 至 17 歲的使用者 SHALL 看到目前為安全模式的提示。頁面 SHALL NOT 顯示生日與年齡區間。登出所有裝置與變更密碼 SHALL 先以自製 Modal 讓使用者確認。一般會員的頁面 SHALL 另提供「匯出我的資料」（JSON 與 CSV 兩個按鈕）與「刪除帳號」；刪除帳號 SHALL 先以自製 Modal 說明 7 天寬限與不可復原的範圍並建議先匯出，已設定密碼者在 Modal 內輸入密碼，其餘輸入確認文字。管理員的頁面 SHALL NOT 顯示刪除帳號。

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

## ADDED Requirements

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

## REMOVED Requirements

### Requirement: 信箱驗證與重設密碼的確認頁
**Reason**: `/verify-email` 隨信箱驗證一併移除；`/reset-password` 只剩管理員使用，改以新增的「重設密碼的確認頁」描述。
**Migration**: 見「重設密碼的確認頁」。
