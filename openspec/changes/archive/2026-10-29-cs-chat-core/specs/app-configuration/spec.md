# Spec Delta

## MODIFIED Requirements

### Requirement: 預備與正式環境缺少必要機密時啟動失敗
在 `staging` 與 `production`，必要機密缺漏或為空時，系統 SHALL 啟動失敗（fail-fast）。必要機密為工作階段簽章金鑰、資料庫連線字串與模型金鑰（`GEMINI_API_KEY`）；後續 change 可擴充清單。錯誤訊息 SHALL 只列出缺漏的變數名稱，不得包含任何機密值。

#### Scenario: 正式環境缺少資料庫連線
- **WHEN** `APP_ENV` 為 `production` 且資料庫連線字串未設定
- **THEN** 啟動失敗，訊息指出缺少的變數名稱

#### Scenario: 開發環境缺少機密
- **WHEN** `APP_ENV` 為 `development` 且工作階段簽章金鑰未設定
- **THEN** 系統仍可啟動，並以僅限開發使用的臨時金鑰運作，且在日誌中提示此狀態

#### Scenario: 預備環境缺少模型金鑰
- **WHEN** `APP_ENV` 為 `staging` 且 `GEMINI_API_KEY` 未設定
- **THEN** 啟動失敗，訊息只指出缺少 `GEMINI_API_KEY`，不含任何金鑰值

#### Scenario: 開發環境缺少模型金鑰
- **WHEN** `APP_ENV` 為 `development` 且 `GEMINI_API_KEY` 未設定
- **THEN** 系統仍可啟動，客服功能不可用並回應服務暫時無法使用
