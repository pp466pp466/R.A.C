# Spec Delta

## ADDED Requirements

### Requirement: Service 的業務錯誤統一對應 HTTP 回應
Service 層 SHALL 以統一的業務錯誤類型表達失敗（輸入不合法、未登入、無權限、找不到、衝突、過於頻繁）；端點 SHALL 統一將其對應為 400、401、403、404、409、429 與繁體中文訊息，API 回傳 JSON 錯誤（含機器可讀的代碼，輸入不合法時另含各欄位的說明），頁面回傳簡潔的錯誤頁。業務錯誤 SHALL NOT 帶有堆疊追蹤、SQL 或機密。

#### Scenario: 輸入不合法
- **WHEN** Service 因輸入不合法拒絕請求
- **THEN** API 回應 400，JSON 含錯誤代碼、繁體中文訊息與各欄位的說明

#### Scenario: 找不到
- **WHEN** Service 表示資源不存在或無權存取
- **THEN** 回應 404 與一般的「找不到」訊息

### Requirement: API 請求本文的格式與大小
需要請求本文的 API SHALL 只接受內容類型為 JSON 且最外層為物件的本文，否則回應 415 或 400 的 JSON 錯誤。請求本文超過 64 KB SHALL 回應 413 的 JSON 錯誤。

#### Scenario: 非 JSON 的本文
- **WHEN** 以表單內容類型呼叫需要 JSON 的 API
- **THEN** 回應 415 的 JSON 錯誤

#### Scenario: JSON 不是物件
- **WHEN** 請求本文為合法 JSON 但最外層是陣列
- **THEN** 回應 400 的 JSON 錯誤

#### Scenario: 本文過大
- **WHEN** 請求本文大於 64 KB
- **THEN** 回應 413 的 JSON 錯誤

## MODIFIED Requirements

### Requirement: 層級依賴方向單向
依賴方向 SHALL 為：頁面與 API 端點（含 HTTP 安全輔助，例如 CSRF、登入 cookie、轉址檢查）→ Service → Repository → 資料模型。端點與 HTTP 安全輔助 SHALL NOT 直接存取資料庫工作階段、Repository 或資料模型，需要資料時 SHALL 呼叫 Service；Repository SHALL NOT 依賴 Service 或端點；Service SHALL NOT 依賴 HTTP 請求物件。

#### Scenario: 端點直接使用 Repository
- **WHEN** 對端點程式碼執行層級依賴檢查，而端點直接匯入 Repository
- **THEN** 檢查失敗並指出違規的檔案

#### Scenario: 依賴方向正確
- **WHEN** 所有層級只依照規定方向依賴
- **THEN** 檢查通過

#### Scenario: HTTP 安全輔助直接讀資料庫
- **WHEN** 對 HTTP 安全輔助程式碼執行層級依賴檢查，而它直接匯入資料模型或資料庫工作階段
- **THEN** 檢查失敗並指出違規的檔案
