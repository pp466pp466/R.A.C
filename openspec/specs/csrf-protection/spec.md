# csrf-protection Specification

## Purpose
定義跨站請求偽造（CSRF）的防護：所有會變更狀態的請求都必須帶有只有本站頁面才拿得到的權杖，並驗證請求的來源，使其他網站無法以使用者的登入狀態代為操作。

## Requirements

### Requirement: 所有非 GET 請求須通過 CSRF 檢查
除 `GET`、`HEAD`、`OPTIONS` 之外的每個請求，系統 SHALL 在進入業務處理之前檢查 CSRF 權杖，包含登入與註冊等未登入即可呼叫的端點；沒有任何端點可以豁免。檢查不通過時，系統 SHALL 回應 403 與繁體中文說明（API 為 JSON），且不執行任何業務操作。

#### Scenario: 沒有權杖
- **WHEN** 未帶權杖標頭的 POST 請求送到任一端點（含登入）
- **THEN** 回應 403，業務操作未執行

#### Scenario: 權杖不符
- **WHEN** 權杖標頭的值與瀏覽器持有的權杖不一致
- **THEN** 回應 403

#### Scenario: 權杖正確
- **WHEN** 請求帶有與瀏覽器持有權杖一致的標頭，且來源正確
- **THEN** 請求正常處理

#### Scenario: GET 不受檢查
- **WHEN** 發出 GET 請求
- **THEN** 不要求權杖

### Requirement: 權杖不可偽造且不可由其他網站取得
CSRF 權杖 SHALL 由伺服器以密鑰簽章產生；只有本站頁面能取得標頭值，瀏覽器端用於比對的值 SHALL 無法被 JavaScript 讀取。權杖 SHALL NOT 以網址參數傳遞。伺服器未簽發或簽章無效的權杖 SHALL 被拒絕。

#### Scenario: 自行編造的權杖
- **WHEN** 攻擊者在標頭與瀏覽器端放入自行編造、未經伺服器簽章的相同值
- **THEN** 請求被拒絕

#### Scenario: 權杖不在網址中
- **WHEN** 檢視任何頁面與 API 的網址
- **THEN** 權杖不出現在網址上

### Requirement: 驗證請求來源
對需要 CSRF 檢查的請求，系統 SHALL 驗證 `Origin` 標頭與本站的來源（協定、主機、連接埠）一致；沒有 `Origin` 時以 `Referer` 的來源代替；兩者皆缺或來源不符（包含 `null`）時 SHALL 拒絕。來源判斷在 staging 與 production SHALL 依入口層轉送的協定與主機。

#### Scenario: 來自其他網站
- **WHEN** 請求的 `Origin` 為其他網站，即使帶有正確的權杖
- **THEN** 回應 403

#### Scenario: 同源請求
- **WHEN** 請求的 `Origin` 與本站相同且權杖正確
- **THEN** 正常處理

#### Scenario: 沒有來源資訊
- **WHEN** 請求同時沒有 `Origin` 與 `Referer`
- **THEN** 回應 403

### Requirement: 權杖的發放與輪替
每個頁面 SHALL 在原始碼中提供目前有效的權杖供前端使用，並在瀏覽器尚無有效權杖時發放新的。登入成功與登出時，系統 SHALL 發放新的權杖，使登入前取得的權杖失效。CSRF 防護 SHALL NOT 改變登入 session cookie 內容。

#### Scenario: 第一次造訪
- **WHEN** 瀏覽器第一次開啟任一頁面
- **THEN** 頁面包含權杖，且瀏覽器收到對應的權杖 cookie

#### Scenario: 登入後輪替
- **WHEN** 使用者登入成功
- **THEN** 登入前持有的權杖不再可用

### Requirement: 前端預設帶權杖標頭
前端 SHALL 讓所有 axios 請求預設帶 `X-CSRF-Token` 標頭，值取自頁面。收到 CSRF 失敗的回應時，前端 SHALL 以繁體中文提示使用者重新整理頁面。

#### Scenario: 預設標頭
- **WHEN** 頁面中的任何 axios 請求送出
- **THEN** 請求帶有 `X-CSRF-Token`

#### Scenario: 權杖已失效
- **WHEN** 伺服器因權杖失效回應 403
- **THEN** 畫面以提示說明頁面已過期並請重新整理，介面恢復可操作狀態
