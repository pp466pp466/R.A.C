# Spec Delta

## ADDED Requirements

### Requirement: 告警收件人設定
系統 SHALL 以 `ALERT_EMAIL_TO`（以逗號分隔的電子郵件地址）設定告警信的收件人，至多 5 個，每個 SHALL 是單純的電子郵件地址（不含顯示名稱、換行與空白），重複的地址 SHALL 只算一個。設定了收件人時，寄信設定（`MAIL_*`）SHALL 同樣完整，否則啟動失敗。`staging` 與 `production` SHALL 要求收件人已設定，缺漏時啟動失敗且訊息只列出變數名稱；`development` 與 `testing` 可以不設定。

#### Scenario: 正式環境沒有收件人
- **WHEN** `APP_ENV` 為 `production` 且 `ALERT_EMAIL_TO` 未設定
- **THEN** 啟動失敗，訊息指出缺少的變數名稱

#### Scenario: 開發環境沒有收件人
- **WHEN** `APP_ENV` 為 `development` 且 `ALERT_EMAIL_TO` 未設定
- **THEN** 系統可啟動，告警檢查只評估不寄信

#### Scenario: 設定了收件人但沒有寄信設定
- **WHEN** 設定了 `ALERT_EMAIL_TO` 但沒有寄信主機、連接埠或寄件者
- **THEN** 啟動失敗，訊息只列出缺少的變數名稱

#### Scenario: 多個收件人
- **WHEN** `ALERT_EMAIL_TO` 為 `a@example.com, b@example.com,a@example.com`
- **THEN** 收件人為兩個地址（去除前後空白與重複）

#### Scenario: 不合法的地址
- **WHEN** `ALERT_EMAIL_TO` 含有顯示名稱、含換行或不是電子郵件格式的值，或超過 5 個地址
- **THEN** 啟動失敗，訊息說明格式與上限，不回顯該值
