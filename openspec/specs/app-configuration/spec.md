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

### Requirement: 簽章金鑰的強度與輪替
`staging` 與 `production` 的 `SECRET_KEY` SHALL 至少 32 個字元，否則啟動失敗。系統 SHALL 支援以 `SECRET_KEY_FALLBACKS`（以逗號分隔的舊金鑰）在輪替期間繼續驗證舊金鑰簽發的登入 cookie 與其他簽章值，但新簽發的內容 SHALL 一律使用目前的 `SECRET_KEY`。

#### Scenario: 金鑰太短
- **WHEN** `APP_ENV` 為 `production` 且 `SECRET_KEY` 只有 16 個字元
- **THEN** 啟動失敗，訊息不含金鑰內容

#### Scenario: 輪替期間
- **WHEN** 以舊金鑰簽發的登入 cookie 在更換 `SECRET_KEY`（舊金鑰列入 `SECRET_KEY_FALLBACKS`）後送回
- **THEN** 仍被視為有效，且之後新簽發的 cookie 使用新金鑰

### Requirement: 全站每日用量上限設定
設定 SHALL 提供 `USAGE_DAILY_TOKEN_LIMIT`（全站每日 token 上限）與 `USAGE_DAILY_CALL_LIMIT`（全站每日模型呼叫次數上限），皆為正整數，未設定時使用預設值；不是正整數或超過合理上限時 SHALL 在啟動時以明確訊息失敗。`.env.example` SHALL 列出這兩個變數名稱而不含實際值。

#### Scenario: 預設值
- **WHEN** 載入沒有這兩個變數的設定
- **THEN** 設定物件的兩個上限為預設值

#### Scenario: 自訂值
- **WHEN** `USAGE_DAILY_TOKEN_LIMIT=500000` 且 `USAGE_DAILY_CALL_LIMIT=300`
- **THEN** 設定物件的兩個上限分別為 500000 與 300

#### Scenario: 不合法
- **WHEN** `USAGE_DAILY_CALL_LIMIT=0`
- **THEN** 載入設定失敗，訊息指出變數名稱

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
