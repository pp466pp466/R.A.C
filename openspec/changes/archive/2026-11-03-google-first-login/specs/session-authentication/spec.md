# Spec Delta

## MODIFIED Requirements

### Requirement: 登入狀態使用簽章 Cookie
登入成功後，系統 SHALL 以簽章的 session cookie 維持登入，cookie 內容 SHALL 只包含使用者內部編號與 `token_version`（不放簽發時間或其他欄位），SHALL NOT 使用 JWT。cookie SHALL 設定 `HttpOnly`、`SameSite=Lax`；有效期為閒置 30 天、每次使用就續期，且 SHALL NOT 設絕對上限：每個辨識出登入者的回應 SHALL 以新的簽章與新的到期時間重新發出 cookie，沒有碰過登入狀態的回應（例如靜態檔、存活檢查）SHALL NOT 重發。staging 與 production SHALL 另設 `Secure` 並使用 `__Host-` 前綴名稱。登入 SHALL 使用全新的 session 內容，不沿用登入前的任何內容。撤銷 SHALL 只靠 `token_version` 與帳號狀態（cookie 被竊取後只要持續被使用就一直有效，這是已接受的取捨）。

#### Scenario: 登入成功
- **WHEN** 使用者通過登入
- **THEN** 回應設定了符合上述屬性的 session cookie，內容只含使用者編號與 `token_version`

#### Scenario: 正式環境的 cookie
- **WHEN** 在 production 登入成功
- **THEN** cookie 名稱以 `__Host-` 開頭，並帶有 `Secure`、`HttpOnly`、`SameSite=Lax`，且沒有 Domain 屬性

#### Scenario: 每次使用就續期
- **WHEN** 登入 20 天後的使用者再次請求需要登入的頁面
- **THEN** 回應重新發出 cookie，新的到期時間是回應當下起算的 30 天，之後第 45 天仍視為已登入

#### Scenario: 超過七天
- **WHEN** 登入已超過 7 天、但期間持續使用的 cookie 被送回
- **THEN** 仍視為已登入（不再有 7 天的絕對期限）

#### Scenario: 閒置超過 30 天
- **WHEN** 最後一次使用在 31 天前的 cookie 被送回
- **THEN** 系統視為未登入

#### Scenario: 沒有碰登入狀態的回應
- **WHEN** 帶著有效登入 cookie 請求 `/healthz` 或靜態檔
- **THEN** 回應沒有重發 cookie，也沒有查詢資料庫

#### Scenario: 被竄改的 cookie
- **WHEN** 送回內容被修改或簽章無效的 cookie
- **THEN** 系統視為未登入

#### Scenario: 沒有絕對上限
- **WHEN** 使用者連續每天使用超過 100 天
- **THEN** 仍維持登入，直到登出、`token_version` 改變或帳號被停用
