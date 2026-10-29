# Spec Delta

## MODIFIED Requirements

### Requirement: Service 的業務錯誤統一對應 HTTP 回應
Service 層 SHALL 以統一的業務錯誤類型表達失敗（輸入不合法、未登入、無權限、找不到、衝突、過於頻繁、服務暫時無法使用）；端點 SHALL 統一將其對應為 400、401、403、404、409、429、503 與繁體中文訊息，API 回傳 JSON 錯誤（含機器可讀的代碼，輸入不合法時另含各欄位的說明），頁面回傳簡潔的錯誤頁。業務錯誤 SHALL NOT 帶有堆疊追蹤、SQL 或機密。

#### Scenario: 輸入不合法
- **WHEN** Service 因輸入不合法拒絕請求
- **THEN** API 回應 400，JSON 含錯誤代碼、繁體中文訊息與各欄位的說明

#### Scenario: 找不到
- **WHEN** Service 表示資源不存在或無權存取
- **THEN** 回應 404 與一般的「找不到」訊息

#### Scenario: 服務暫時無法使用
- **WHEN** Service 表示所依賴的外部服務目前不可用（例如沒有設定模型供應商）
- **THEN** API 回應 503，JSON 含代碼 `service_unavailable` 與繁體中文訊息，不含任何內部細節
