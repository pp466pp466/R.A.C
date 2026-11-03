# Spec Delta

## MODIFIED Requirements

### Requirement: 管理員不能只靠密碼登入
系統 SHALL NOT 提供任何只憑信箱與密碼就能建立登入 session 的端點：`POST /api/auth/login` 不存在（回應 404），管理員唯一的登入路徑是「先 Google、再密碼」。管理員的密碼只用於 Google 通過之後的密碼驗證步驟與高風險操作的重新輸入。

#### Scenario: 管理員用密碼端點
- **WHEN** 以管理員的正確信箱與密碼呼叫 `POST /api/auth/login`
- **THEN** 回應 404，沒有登入 session，也不計入該帳號的密碼失敗次數

#### Scenario: 管理員只持有密碼驗證端點
- **WHEN** 沒有中間狀態 cookie 的請求呼叫 `POST /api/auth/admin-password`
- **THEN** 回應 401，沒有登入 session
