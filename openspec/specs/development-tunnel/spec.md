# development-tunnel Specification

## Purpose

定義開發環境如何在 HTTPS 通道（例如 ngrok）後面使用：讓本機服務暫時擁有對外的 https 網域以測試 Google 登入與信件連結，同時不放寬預備與正式環境，並提醒通道會把開發環境暴露在網際網路上。

## Requirements

### Requirement: 開發環境可選擇信任通道的轉送標頭
系統 SHALL 提供只在 `development` 生效的設定 `TRUST_PROXY_HEADERS`：值為真時，應用程式 SHALL 與預備及正式環境一樣信任入口層轉送的協定與主機標頭（單層代理），使經 HTTPS 通道到達的請求被視為 https 與通道的主機名稱。未設定時 SHALL 維持原本不信任的行為。

#### Scenario: 通道後的 POST
- **WHEN** development 設定 `TRUST_PROXY_HEADERS` 為真，請求帶 `X-Forwarded-Proto: https` 與通道的主機名稱，且 Origin 為該 https 網址
- **THEN** CSRF 檢查通過

#### Scenario: 預設不信任
- **WHEN** development 沒有設定此變數，請求帶 `X-Forwarded-Proto: https` 但 Origin 為 https 網址
- **THEN** 因來源與請求不符而被拒絕（維持原本行為）

### Requirement: 其他環境拒絕這個設定
`testing`、`staging` 與 `production` SHALL 在設定了 `TRUST_PROXY_HEADERS`（不論值）時啟動失敗，訊息只指出變數名稱：staging 與 production 本來就信任入口層，不需要也不應該有第二個開關；testing 不該被通道影響。

#### Scenario: 預備環境設定了
- **WHEN** `APP_ENV` 為 `staging` 且設定了 `TRUST_PROXY_HEADERS`
- **THEN** 啟動失敗，訊息指出變數名稱

#### Scenario: 值不合法
- **WHEN** development 的值不是 true 或 false
- **THEN** 啟動失敗並說明可接受的值，不回顯該值

### Requirement: 通道網址作為對外網址並附使用注意事項
使用通道時，`BASE_URL` SHALL 設為通道的 https 網址，Google 登入的回呼網址與信件連結因此指向通道。營運文件 SHALL 載明使用步驟，以及：通道會把開發環境（含開發工具頁與測試帳號）暴露在網際網路上，使用者 SHOULD 在通道端加上存取控制（例如 ngrok 的基本驗證）；通道網址可能變動，OAuth 用戶端的授權重新導向 URI 需要同步；開發用的 OAuth 用戶端 SHALL NOT 用於預備或正式環境。

#### Scenario: 回呼網址
- **WHEN** development 的 `BASE_URL` 為通道的 https 網址並啟用 Google 登入
- **THEN** 回呼網址以該網址組成

#### Scenario: 文件載明風險
- **WHEN** 檢視營運文件的通道小節
- **THEN** 載明暴露風險、存取控制建議與 OAuth 用戶端的分離
