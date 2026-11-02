# staging-verification Specification

## Purpose

定義預備環境如何被驗收：以可重複執行的冒煙腳本檢查安全標頭、cookie、CSRF、登入流程與權限邊界，預備環境不含真實個資，並規定驗收紀錄與上線前關卡的狀態只在有依據時更新。

## Requirements

### Requirement: 冒煙驗收腳本
系統 SHALL 提供冒煙驗收腳本，對一個預備環境網址依序檢查並逐項輸出通過或失敗：`/healthz` 與 `/readyz` 回 200；首頁回應帶有 HSTS 與其餘四個安全標頭且沒有 `Content-Security-Policy`；session 與 CSRF cookie 使用 `__Host-` 前綴、帶 `Secure`、不含 `Domain`、session 為 `HttpOnly`；沒有 CSRF 權杖的 POST 被拒絕；註冊（使用保留的測試信箱網域）、登入與開新對話成功；一般會員存取管理員 API 得到 404（與不存在的端點相同，不透露後台存在；admin-console 規格與 AGENTS.md 第 10 節）；未登入存取需要登入的 API 得到 401。腳本 SHALL 預設不呼叫任何模型；需要時 SHALL 以明確的同意參數才送出一則訊息並檢查回覆不是降級文案。腳本輸出 SHALL 只含項目名稱與結果，SHALL NOT 含 cookie、權杖、密碼或回應內容。

#### Scenario: 全部通過
- **WHEN** 對設定正確的預備環境執行腳本
- **THEN** 每一項輸出通過，結束代碼為 0

#### Scenario: 缺少標頭
- **WHEN** 目標的首頁回應沒有 HSTS
- **THEN** 該項輸出失敗並指出標頭名稱，結束代碼非零

#### Scenario: 預設不呼叫模型
- **WHEN** 沒有提供同意參數
- **THEN** 腳本不送出任何對話訊息，輸出說明略過模型呼叫

#### Scenario: 輸出不含機密
- **WHEN** 檢視完整輸出
- **THEN** 沒有 cookie 值、CSRF 權杖、密碼或回應本文

### Requirement: 腳本只對預備環境執行
冒煙驗收腳本 SHALL 沿用負載測試的位址守門：目標 SHALL 是 https；主機名稱含 `production` 或位於已列出的正式網域時 SHALL 拒絕且不接受任何確認參數可繞過；非 https 的位址 SHALL 拒絕。

#### Scenario: 正式環境
- **WHEN** 目標網址的主機名稱含 `production`
- **THEN** 腳本拒絕並不送出任何請求

#### Scenario: 非加密連線
- **WHEN** 目標網址是 http
- **THEN** 腳本拒絕並說明預備環境必須是 https

### Requirement: 預備環境不使用真實個資
預備環境與壓測環境 SHALL NOT 含真實使用者的個資：帳號只能來自保留測試網域的測試帳號（`.invalid`），資料庫 SHALL NOT 由正式環境複製而來。部署流程 SHALL NOT 提供把正式資料還原到預備環境的步驟。

#### Scenario: 測試帳號網域
- **WHEN** 冒煙驗收腳本與壓測註冊帳號
- **THEN** 信箱都屬於保留的 `.invalid` 網域

#### Scenario: 沒有複製正式資料的步驟
- **WHEN** 檢視預備環境的所有部署與還原步驟
- **THEN** 沒有任何一步以正式環境的備份或資料庫為來源

### Requirement: Google 登入在預備環境以人工驗收
預備環境 SHALL 有專用的 Google OAuth 用戶端（網頁應用程式型；授權重新導向 URI 為預備環境網址加 `/auth/google/callback`），SHALL NOT 重用開發用的用戶端，也 SHALL NOT 重用 Firebase 自動建立的用戶端。因為真實的 Google 登入需要使用者本人的 Google 帳號，驗收 SHALL 由使用者人工進行並記錄：首次登入（勾選條款並提供生日與年齡區間後建立帳號、沒有密碼）、第二次登入（辨識為同一個帳號、不重複建立；目前登入頁仍要求重新填寫條款與生日，「一次點擊」屬獨立的後續 change）、以及同信箱既有帳號的合併。沒有人工驗收時 SHALL 標為「未驗證」，SHALL NOT 以自動化測試或預期結果代替。

#### Scenario: 專用用戶端
- **WHEN** 檢視預備環境的 Google 設定
- **THEN** 用戶端識別碼與開發環境及 Firebase 自動建立的不同，授權重新導向 URI 只有預備環境的回呼網址

#### Scenario: 首次與再次登入
- **WHEN** 使用者以真實 Google 帳號在預備環境登入兩次
- **THEN** 第一次在同意條款並提供年齡確認後建立沒有密碼的帳號並登入，第二次登入辨識為同一個帳號而不重複建立（目前登入頁仍要求重新勾選條款並填寫生日與年齡區間）

#### Scenario: 沒做就標未驗證
- **WHEN** 使用者不在場而沒有進行人工驗收
- **THEN** 驗收紀錄標為未驗證並說明原因

### Requirement: 驗收紀錄只記有依據的結果
預備環境的驗收結果 SHALL 記錄在 README 的驗收紀錄：日期、對應的 commit、每一項的結果與數字，以及未完成或未驗證的項目。上線前關卡的狀態 SHALL 只在有對應紀錄時更新為「進行中」或「已通過」，且 SHALL 記錄資料來源的位置；沒有實際執行的項目 SHALL 標為未驗證，SHALL NOT 以預期結果代替。

#### Scenario: 關卡有依據才更新
- **WHEN** 某一道關卡的狀態改為已通過
- **THEN** 同一列載明日期與驗收紀錄的位置，該紀錄存在於 README

#### Scenario: 沒做的不算做了
- **WHEN** 某個驗證因為缺少前提（例如沒有同意模型呼叫）而沒有執行
- **THEN** 紀錄標為未驗證並說明原因
