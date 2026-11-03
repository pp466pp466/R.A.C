# Spec Delta

## ADDED Requirements

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
`/age` SHALL 需要登入，提供生日欄位與年齡區間下拉選單（選項與提示來自伺服器提供的年齡規則）；送出期間禁用按鈕、結束時（`finally`）恢復。成功後 SHALL 導向登入前要去的位置（不合法時為預設頁面）。待確認年齡的玩家 SHALL 被導向此頁；已完成年齡確認的玩家開啟此頁 SHALL 導向預設頁面。未滿 13 歲被抹除、不一致、格式錯誤的訊息 SHALL 是通用的繁體中文說明，不指出是哪個欄位不一致，也不暗示可以改填其他值。

#### Scenario: 補填成功
- **WHEN** 待確認年齡的玩家在 `/age` 提交一致的生日與區間
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

### Requirement: 管理員密碼驗證頁與申訴頁
`/login/password` SHALL 只接受持有管理員中間狀態的請求（其餘導向 `/login`），提供密碼欄位與「忘記密碼」按鈕（寄信設定缺少時不顯示）。`/appeal` SHALL 需要登入且只對被鎖定的玩家有內容（其餘導向預設頁面），提供申訴文字欄位、字數提示、提交後的處理狀態。兩頁 SHALL 遵守前端規範（純文字渲染、`finally` 恢復、自製 Toast）。

#### Scenario: 沒有中間狀態
- **WHEN** 沒有管理員中間狀態的請求開啟 `/login/password`
- **THEN** 導向 `/login`

#### Scenario: 被鎖定者的申訴頁
- **WHEN** 被鎖定的玩家開啟 `/appeal`
- **THEN** 看到申訴表單與已提交申訴的處理狀態

### Requirement: 登入後的預設頁面與首頁導向
登入後的預設頁面 SHALL 由單一常數決定（目前為 `/cs`；RPG 上線後改為選擇劇本的首頁，不直接進入聊天）。已登入者開啟 `/` 或 `/login` SHALL 被導向預設頁面（受限登入狀態各導向自己的頁面：待確認年齡 → `/age`，被鎖定 → `/appeal`，等待刪除 → `/account`）；`/register` SHALL 一律導向 `/login`。未登入者開啟 `/` 仍看到首頁。

#### Scenario: 已登入者開啟首頁
- **WHEN** 已登入的一般玩家開啟 `/`
- **THEN** 導向 `/cs`

#### Scenario: 受限狀態開啟首頁
- **WHEN** 待確認年齡的玩家開啟 `/`
- **THEN** 導向 `/age`

#### Scenario: 舊的註冊網址
- **WHEN** 任何人開啟 `/register`
- **THEN** 導向 `/login`

## MODIFIED Requirements

### Requirement: 頁面清單與存取規則
系統 SHALL 提供下列頁面：`/login`、`/login/password`、`/age`、`/appeal`、`/forgot-password`、`/reset-password`、`/verify-email`、`/account`、`/terms`、`/privacy`；`/register` 只作導向。`/account`、`/age` SHALL 需要登入；已登入的使用者開啟 `/login` SHALL 被導向預設頁面（見「登入後的預設頁面與首頁導向」）。忘記密碼、重設密碼與信箱驗證頁在信箱驗證關閉時 SHALL 回應 404（管理員的重設密碼流程需要寄信設定，缺少時同樣 404）。所有頁面 SHALL 繼承基礎樣板，只載入本站資源。

#### Scenario: 未登入開啟帳號設定
- **WHEN** 未登入者開啟 `/account`
- **THEN** 導向 `/login`，且登入後返回 `/account`

#### Scenario: 已登入開啟登入頁
- **WHEN** 已登入的一般玩家開啟 `/login`
- **THEN** 導向預設頁面

#### Scenario: 驗證關閉時的相關頁面
- **WHEN** 信箱驗證關閉且沒有寄信設定，開啟 `/forgot-password`
- **THEN** 回應 404 的繁體中文頁面

## REMOVED Requirements

### Requirement: 登入與註冊表單
**Reason**: 登入與註冊合一為只有 Google 與條款勾選的 `/login`（見「登入頁只有 Google 與條款勾選」）；年齡欄位移到登入之後的 `/age`。
**Migration**: 密碼登入與註冊的 API 仍保留到 change ② 才移除，但沒有任何頁面呼叫它們；密碼與年齡欄位的前端邏輯與測試隨本 change 移除。

### Requirement: 註冊完成的畫面
**Reason**: `/register` 頁面不再存在，玩家沒有密碼註冊流程。
**Migration**: `/register` 導向 `/login`。
