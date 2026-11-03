# Spec Delta

## ADDED Requirements

### Requirement: 寄信設定的觸發與對外網址
設定了告警收件人（`ALERT_EMAIL_TO`），或設定了任何一項寄信參數（`MAIL_SMTP_HOST`、`MAIL_SMTP_PORT`、`MAIL_FROM`）時，系統 SHALL 要求寄信主機、連接埠與寄件者地址皆已設定，缺漏時啟動失敗且訊息只列出變數名稱；寄信帳號與密碼 SHALL 同時設定或同時省略。寄信連線預設使用 STARTTLS，亦可設為 SSL；不加密的連線 SHALL 只允許在 `development` 與 `testing`。對外網址（`BASE_URL`）SHALL 為不含路徑、查詢與片段的絕對網址，在 `staging` 與 `production` SHALL 為 `https`；Google 登入啟用時 SHALL 要求已設定對外網址（OAuth 回呼）。管理員的重設密碼連結需要寄信設定與對外網址，兩者缺一時該功能停用而不是啟動失敗。寄信密碼 SHALL 與其他機密一樣被遮罩。

#### Scenario: 設定了寄信主機但缺少其他參數
- **WHEN** 設定了 `MAIL_SMTP_HOST` 但沒有設定寄件者
- **THEN** 啟動失敗，訊息指出缺少的變數名稱

#### Scenario: 沒有任何寄信設定與告警收件人
- **WHEN** `APP_ENV` 為 `development`，沒有設定 `ALERT_EMAIL_TO` 與任何 `MAIL_*`
- **THEN** 系統可啟動，沒有寄信器，管理員的忘記密碼停用

#### Scenario: 正式環境的對外網址
- **WHEN** `APP_ENV` 為 `production` 且 `BASE_URL` 以 `http://` 開頭
- **THEN** 啟動失敗

#### Scenario: Google 登入需要對外網址
- **WHEN** 已設定 Google 用戶端識別碼與密鑰，但沒有 `BASE_URL`
- **THEN** 啟動失敗，訊息指出缺少 `BASE_URL`

#### Scenario: 只設定寄信帳號
- **WHEN** 設定了寄信帳號但沒有密碼
- **THEN** 啟動失敗，訊息指出須同時設定

#### Scenario: 不加密的寄信連線
- **WHEN** `APP_ENV` 為 `staging` 且寄信連線設為不加密
- **THEN** 啟動失敗

#### Scenario: 設定被列印
- **WHEN** 設定物件被轉為字串或寫入日誌
- **THEN** 寄信密碼與 Google 用戶端密鑰顯示為遮罩

### Requirement: 已淘汰的信箱驗證開關被忽略
系統 SHALL NOT 再讀取 `EMAIL_VERIFICATION_REQUIRED` 來決定任何行為：不論值為何（包含 `true`、`false` 與不合法的值），啟動都 SHALL NOT 因它失敗。環境中出現這個變數時，系統 SHALL 在啟動時記錄一則警告，只指出變數名稱與已淘汰，SHALL NOT 回顯值。`.env.example` SHALL NOT 再列出它。

#### Scenario: 正式環境殘留舊設定
- **WHEN** `APP_ENV` 為 `production`，平台設定仍含 `EMAIL_VERIFICATION_REQUIRED=false`，其餘設定完整
- **THEN** 系統正常啟動，行為與沒有這個變數時相同，日誌有一則指出該變數已淘汰的警告，警告不含值

#### Scenario: 不合法的值
- **WHEN** `EMAIL_VERIFICATION_REQUIRED` 為 `maybe`
- **THEN** 系統正常啟動並記錄同一則警告

#### Scenario: 範本不含舊變數
- **WHEN** 檢查 `.env.example`
- **THEN** 沒有 `EMAIL_VERIFICATION_REQUIRED`

## REMOVED Requirements

### Requirement: 信箱驗證開關
**Reason**: 玩家只用 Google 登入，信箱由 Google 驗證；沒有信箱驗證信，也就沒有需要開關的功能。「production 必須開啟信箱驗證」的檢查隨之消失。
**Migration**: 從平台設定移除 `EMAIL_VERIFICATION_REQUIRED`（留著也無害，會被忽略並警告，見「已淘汰的信箱驗證開關被忽略」）。寄信設定改由 `ALERT_EMAIL_TO` 與 `MAIL_*` 決定（見「寄信與對外網址設定」）。

### Requirement: 寄信與對外網址設定
**Reason**: 原需求以信箱驗證開啟為前提；該開關已移除，寄信設定改由告警收件人或任何一項 `MAIL_*` 觸發。
**Migration**: 見「寄信設定的觸發與對外網址」。
