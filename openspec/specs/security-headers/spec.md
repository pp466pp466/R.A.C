# security-headers Specification

## Purpose
定義應用程式對瀏覽器送出的安全標頭：在 staging 與 production 強制瀏覽器只用 HTTPS 連線、不猜測內容類型、不被嵌入其他網站的框架、不任意洩漏來源網址與裝置權限，並明確記下刻意不做的內容安全政策。

## Requirements

### Requirement: 預備與正式環境的回應帶有安全標頭
系統 SHALL 在 `staging` 與 `production` 對**每一個**回應（頁面、API、靜態資源與錯誤回應，包含 4xx 與 5xx）加上下列標頭：`Strict-Transport-Security: max-age=31536000`（一年；SHALL NOT 含 `includeSubDomains` 與 `preload`）、`X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`、`X-Frame-Options: DENY`，以及停用相機、麥克風、定位、付款與 USB 的 `Permissions-Policy`。端點已明確設定同名標頭時，系統 SHALL NOT 覆蓋。

#### Scenario: 正式環境的頁面
- **WHEN** 在 `production` 請求任何頁面
- **THEN** 回應含上述五個標頭，HSTS 的值為 `max-age=31536000`

#### Scenario: 錯誤回應與靜態資源
- **WHEN** 在 `staging` 請求不存在的路徑、被 CSRF 拒絕的請求、導致 500 的請求，或一個靜態檔案
- **THEN** 各回應同樣含上述五個標頭

#### Scenario: 不含子網域與預載入
- **WHEN** 讀取 HSTS 標頭
- **THEN** 其值不含 `includeSubDomains` 與 `preload`

#### Scenario: 端點自訂的標頭不被覆蓋
- **WHEN** 某個回應已自行設定 `Referrer-Policy`
- **THEN** 該值維持原樣

### Requirement: 開發與測試環境不送 HSTS
系統 SHALL NOT 在 `development` 與 `testing` 送出 `Strict-Transport-Security`，因為本機以 http 連線，瀏覽器會把 HSTS 記在 `localhost` 並讓之後的本機開發全部失敗。其餘四個標頭 SHALL 在所有環境都送出。

#### Scenario: 本機開發
- **WHEN** 在 `development` 請求任何頁面
- **THEN** 回應沒有 `Strict-Transport-Security`，但有 `X-Content-Type-Options`、`Referrer-Policy`、`X-Frame-Options` 與 `Permissions-Policy`

### Requirement: 刻意不做內容安全政策並明文記錄
系統 SHALL NOT 送出 `Content-Security-Policy`，因為前端使用的 Vue 全域建置在瀏覽器編譯模板，需要 `unsafe-eval`，這樣的政策沒有實際保護力。這項限制 SHALL 記載於營運文件，並在日後前端改為預先編譯模板時重新評估。

#### Scenario: 限制有記載
- **WHEN** 檢視營運文件
- **THEN** 載明未送出內容安全政策及其原因
