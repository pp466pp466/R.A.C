# Spec Delta

## ADDED Requirements

### Requirement: Gemini 端點覆寫僅限非正式環境
系統 SHALL 以選填的 `GEMINI_BASE_URL` 讓 Gemini 轉接器改用其他端點（供隔離的壓測環境指向假模型伺服器）。因為金鑰與提示詞會被送到該位址，值 SHALL 是不含帳號、密碼、查詢字串與片段的絕對 https 網址（`development` 與 `testing` 另允許回送位址的 http）；`production` 設定了這個變數 SHALL 啟動失敗。錯誤訊息 SHALL 只指出變數名稱，不回顯網址。未設定時 SHALL 使用轉接器的預設端點。

#### Scenario: 壓測環境指向假伺服器
- **WHEN** `APP_ENV` 為 `staging` 且 `GEMINI_BASE_URL` 為合法的 https 網址
- **THEN** Gemini 轉接器以該網址呼叫

#### Scenario: 正式環境拒絕
- **WHEN** `APP_ENV` 為 `production` 且設定了 `GEMINI_BASE_URL`
- **THEN** 啟動失敗，訊息指出變數名稱

#### Scenario: 不合法的網址
- **WHEN** 值含帳號、查詢字串、不是 https（且不是開發的回送位址）
- **THEN** 啟動失敗，訊息說明規則而不回顯該值

#### Scenario: 未設定
- **WHEN** 沒有設定此變數
- **THEN** 轉接器使用預設的 Gemini 端點
