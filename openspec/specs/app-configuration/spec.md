# app-configuration Specification

## Purpose
定義應用程式如何依執行環境載入設定與機密：環境不明時拒絕啟動、預備與正式環境缺少必要機密時啟動失敗，並保證機密不進入版本庫、日誌與回應。

## Requirements

### Requirement: 執行環境必須明確指定
系統 SHALL 以環境變數 `APP_ENV` 選擇設定，合法值為 `development`、`testing`、`staging`、`production`。`APP_ENV` 未設定或不是合法值時，系統 SHALL 拒絕啟動，且不得預設為任何環境。

#### Scenario: 未設定環境
- **WHEN** 啟動應用程式且 `APP_ENV` 未設定
- **THEN** 啟動失敗，並以繁體中文訊息說明必須設定 `APP_ENV` 及合法值

#### Scenario: 不合法的環境值
- **WHEN** `APP_ENV` 為 `prod` 之類不在合法清單的值
- **THEN** 啟動失敗，訊息列出合法值，且不套用任何預設設定

### Requirement: 預備與正式環境缺少必要機密時啟動失敗
在 `staging` 與 `production`，必要機密缺漏或為空時，系統 SHALL 啟動失敗（fail-fast）。本 change 的必要機密為工作階段簽章金鑰與資料庫連線字串；後續 change 可擴充清單。錯誤訊息 SHALL 只列出缺漏的變數名稱，不得包含任何機密值。

#### Scenario: 正式環境缺少資料庫連線
- **WHEN** `APP_ENV` 為 `production` 且資料庫連線字串未設定
- **THEN** 啟動失敗，訊息指出缺少的變數名稱

#### Scenario: 開發環境缺少機密
- **WHEN** `APP_ENV` 為 `development` 且工作階段簽章金鑰未設定
- **THEN** 系統仍可啟動，並以僅限開發使用的臨時金鑰運作，且在日誌中提示此狀態

### Requirement: 預備與正式環境禁用不安全的開發設定
在 `staging` 與 `production`，系統 SHALL 禁用除錯模式，並在資料庫連線使用 `sa` 帳號、或關閉伺服器憑證驗證（`TrustServerCertificate=yes`）時拒絕啟動。這兩項只允許在 `development` 與 `testing` 使用。

#### Scenario: 正式環境使用 sa 帳號
- **WHEN** `APP_ENV` 為 `production` 且資料庫連線字串的登入帳號為 `sa`
- **THEN** 啟動失敗，訊息說明正式環境不得使用 `sa`

#### Scenario: 預備環境信任自簽憑證
- **WHEN** `APP_ENV` 為 `staging` 且連線字串包含 `TrustServerCertificate=yes`
- **THEN** 啟動失敗，訊息說明該設定只限本機開發

#### Scenario: 本機開發信任自簽憑證
- **WHEN** `APP_ENV` 為 `development` 且連線字串包含 `TrustServerCertificate=yes`
- **THEN** 系統正常啟動並可連線本機資料庫

### Requirement: 機密的注入來源依環境區分
`development` SHALL 可從專案根目錄的 `.env` 檔載入機密；`staging` 與 `production` SHALL 只從行程環境變數讀取，不讀取 `.env` 檔。專案 SHALL 提供只含變數名稱、不含任何實際值的 `.env.example`，且 `.env` SHALL 不被納入版本控制。

#### Scenario: 開發環境載入 .env
- **WHEN** `APP_ENV` 為 `development` 且根目錄存在 `.env`
- **THEN** `.env` 內的變數成為設定來源，且行程環境變數優先於 `.env`

#### Scenario: 正式環境忽略 .env
- **WHEN** `APP_ENV` 為 `production` 且根目錄存在 `.env`
- **THEN** 系統不讀取該檔，只使用行程環境變數

#### Scenario: 範例檔不含實際值
- **WHEN** 檢查 `.env.example`
- **THEN** 每個變數都只有名稱與說明，沒有任何實際密碼、金鑰或連線帳密

### Requirement: 機密不得外洩
系統 SHALL NOT 在日誌、錯誤訊息、HTTP 回應、設定物件的字串表示中輸出機密值。資料庫連線字串在任何輸出中 SHALL 隱藏密碼。

#### Scenario: 設定被列印
- **WHEN** 設定物件被轉為字串或寫入日誌
- **THEN** 機密欄位顯示為遮罩，不含實際值

#### Scenario: 連線字串出現在錯誤中
- **WHEN** 資料庫連線失敗並產生錯誤訊息
- **THEN** 訊息不包含連線字串中的密碼

### Requirement: 測試環境與外部資源隔離
`testing` 環境 SHALL 預設使用記憶體中的 SQLite，不需要任何真實機密，也不得連到正式或預備環境的資料庫。

#### Scenario: 單元測試的預設資料庫
- **WHEN** `APP_ENV` 為 `testing` 且未指定資料庫
- **THEN** 系統使用記憶體資料庫，測試結束後資料全部消失
