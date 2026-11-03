# Spec Delta

## Purpose

定義管理員的登入：每次都先通過 Google，再通過密碼，兩者都通過才建立登入 session；管理員不能只靠密碼登入。中間狀態只存在短效的簽章 cookie，不能存取任何受保護的資源。密碼驗證那一步提供「忘記密碼」寄信重設。

## ADDED Requirements

### Requirement: 管理員先 Google 再密碼
角色為 `admin` 的帳號通過 Google 登入後，系統 SHALL NOT 建立登入 session，而是核發短效（10 分鐘）的簽章中間狀態 cookie（只含使用者編號與用途），並導向密碼驗證頁。密碼驗證成功才建立一般的登入 session 並清除中間狀態；中間狀態 SHALL NOT 能存取任何需要登入的端點。管理員的 Google 身分尚未綁定時，第一次以同信箱的 Google 登入 SHALL 綁定（沿用既有的合併規則），之後同樣要再驗證密碼。

#### Scenario: Google 通過後尚未登入
- **WHEN** 管理員通過 Google 但尚未輸入密碼
- **THEN** 呼叫 `/api/admin/overview` 得到 401，沒有登入 session

#### Scenario: 兩者都通過
- **WHEN** 管理員通過 Google 並輸入正確的密碼
- **THEN** 建立登入 session，可進入 `/admin`

#### Scenario: 中間狀態過期
- **WHEN** 中間狀態 cookie 超過 10 分鐘才送回密碼
- **THEN** 回應 401，需要重新以 Google 登入

### Requirement: 管理員不能只靠密碼登入
管理員帳號透過密碼登入端點登入 SHALL 一律失敗，回應與帳號不存在或密碼錯誤相同的通用訊息，且不計入該帳號的密碼失敗次數。

#### Scenario: 管理員用密碼端點
- **WHEN** 以管理員的正確信箱與密碼呼叫密碼登入端點
- **THEN** 回應通用的登入失敗訊息，沒有登入 session

### Requirement: 密碼驗證步驟的失敗限制
密碼驗證沿用既有規則：同一帳號連續失敗 5 次鎖定 15 分鐘（到期自動解除，計數存放於資料庫），失敗一律回應相同的通用訊息。成功時 SHALL 重設失敗計數。

#### Scenario: 連續失敗
- **WHEN** 管理員在密碼驗證步驟連續輸入錯誤 5 次
- **THEN** 該帳號鎖定 15 分鐘，之後的嘗試即使密碼正確也回應通用訊息

### Requirement: 密碼驗證步驟的忘記密碼
密碼驗證頁 SHALL 提供「忘記密碼」按鈕；按下後系統 SHALL 寄重設信到該管理員的信箱，使用與營運告警相同的寄信設定。此端點 SHALL 只接受持有有效中間狀態 cookie 的請求，匿名請求一律 404。重設權杖沿用既有規則（32 位元組隨機值、只存雜湊、單次使用、1 小時失效、連結的 GET 只顯示確認頁、由 POST 完成；成功後 `token_version` 加 1 並寄通知信）。寄信設定缺少時 SHALL 不顯示按鈕。

#### Scenario: 按下忘記密碼
- **WHEN** 持有中間狀態的管理員按下「忘記密碼」
- **THEN** 該管理員的信箱收到一封重設信，回應不洩漏任何其他資訊

#### Scenario: 匿名請求
- **WHEN** 沒有中間狀態 cookie 的請求呼叫忘記密碼端點
- **THEN** 回應 404，沒有寄信

#### Scenario: 重設後須重新登入
- **WHEN** 管理員完成密碼重設
- **THEN** 既有的 session 與中間狀態失效，需要重新以 Google 登入
