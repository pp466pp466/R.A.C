# Spec Delta

## ADDED Requirements

### Requirement: 信箱驗證開關
系統 SHALL 以 `EMAIL_VERIFICATION_REQUIRED` 控制是否啟用信箱驗證與重設密碼。值只接受 `true` 或 `false`（不分大小寫），其他值 SHALL 啟動失敗。未設定時，`development` 與 `testing` SHALL 視為 `false`，`staging` 與 `production` SHALL 視為 `true`。`production` 設為 `false` SHALL 啟動失敗；`staging` 可明確設為 `false`。

#### Scenario: 正式環境關閉驗證
- **WHEN** `APP_ENV` 為 `production` 且 `EMAIL_VERIFICATION_REQUIRED` 為 `false`
- **THEN** 啟動失敗，訊息說明正式環境必須開啟信箱驗證

#### Scenario: 預備環境明確關閉
- **WHEN** `APP_ENV` 為 `staging` 且 `EMAIL_VERIFICATION_REQUIRED` 為 `false`，其餘設定完整
- **THEN** 系統正常啟動

#### Scenario: 開發環境未設定
- **WHEN** `APP_ENV` 為 `development` 且未設定該變數
- **THEN** 視為關閉，不要求任何寄信設定

#### Scenario: 不合法的值
- **WHEN** `EMAIL_VERIFICATION_REQUIRED` 為 `maybe`
- **THEN** 啟動失敗，訊息列出合法值

### Requirement: 寄信與對外網址設定
信箱驗證開啟時，系統 SHALL 要求寄信主機、連接埠、寄件者地址與對外網址（`BASE_URL`）皆已設定，缺漏時啟動失敗且訊息只列出變數名稱；寄信帳號與密碼 SHALL 同時設定或同時省略。寄信連線預設使用 STARTTLS，亦可設為 SSL；不加密的連線 SHALL 只允許在 `development` 與 `testing`。對外網址 SHALL 為不含路徑、查詢與片段的絕對網址，在 `staging` 與 `production` SHALL 為 `https`。Google 登入啟用時同樣要求對外網址。寄信密碼 SHALL 與其他機密一樣被遮罩。

#### Scenario: 驗證開啟但缺少寄信設定
- **WHEN** 信箱驗證開啟且沒有設定寄信主機
- **THEN** 啟動失敗，訊息指出缺少的變數名稱

#### Scenario: 正式環境的對外網址
- **WHEN** `APP_ENV` 為 `production` 且 `BASE_URL` 以 `http://` 開頭
- **THEN** 啟動失敗

#### Scenario: 只設定寄信帳號
- **WHEN** 設定了寄信帳號但沒有密碼
- **THEN** 啟動失敗，訊息指出須同時設定

#### Scenario: 不加密的寄信連線
- **WHEN** `APP_ENV` 為 `staging` 且寄信連線設為不加密
- **THEN** 啟動失敗

#### Scenario: 設定被列印
- **WHEN** 設定物件被轉為字串或寫入日誌
- **THEN** 寄信密碼與 Google 用戶端密鑰顯示為遮罩

### Requirement: 簽章金鑰的強度與輪替
`staging` 與 `production` 的 `SECRET_KEY` SHALL 至少 32 個字元，否則啟動失敗。系統 SHALL 支援以 `SECRET_KEY_FALLBACKS`（以逗號分隔的舊金鑰）在輪替期間繼續驗證舊金鑰簽發的登入 cookie 與其他簽章值，但新簽發的內容 SHALL 一律使用目前的 `SECRET_KEY`。

#### Scenario: 金鑰太短
- **WHEN** `APP_ENV` 為 `production` 且 `SECRET_KEY` 只有 16 個字元
- **THEN** 啟動失敗，訊息不含金鑰內容

#### Scenario: 輪替期間
- **WHEN** 以舊金鑰簽發的登入 cookie 在更換 `SECRET_KEY`（舊金鑰列入 `SECRET_KEY_FALLBACKS`）後送回
- **THEN** 仍被視為有效，且之後新簽發的 cookie 使用新金鑰
