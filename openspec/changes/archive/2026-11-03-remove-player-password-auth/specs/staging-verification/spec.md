# Spec Delta

## MODIFIED Requirements

### Requirement: 冒煙驗收腳本
系統 SHALL 提供冒煙驗收腳本，對一個預備環境網址依序檢查並逐項輸出通過、失敗或未驗證：`/healthz` 與 `/readyz` 回 200；首頁回應帶有 HSTS 與其餘四個安全標頭且沒有 `Content-Security-Policy`；session 與 CSRF cookie 使用 `__Host-` 前綴、帶 `Secure`、不含 `Domain`、session 為 `HttpOnly`；沒有 CSRF 權杖的 POST 被拒絕；已移除的 `POST /api/auth/login` 與 `/api/auth/register` 得到 404；未登入存取需要登入的 API 得到 401。需要登入的項目（開新對話、一般會員存取管理員 API 得到 404 而與不存在的端點相同、不透露後台存在）SHALL 只在提供預先簽出的測試玩家 session cookie（以環境變數傳入，SHALL NOT 作為命令列參數）時執行，沒有提供時 SHALL 標為「未驗證」而不是通過。腳本 SHALL 預設不呼叫任何模型；需要時 SHALL 以明確的同意參數才送出一則訊息並檢查回覆不是降級文案。腳本輸出 SHALL 只含項目名稱與結果，SHALL NOT 含 cookie、權杖、密碼或回應內容。

#### Scenario: 全部通過
- **WHEN** 對設定正確的預備環境執行腳本，並提供有效的測試玩家 cookie
- **THEN** 每一項輸出通過，結束代碼為 0

#### Scenario: 沒有測試 cookie
- **WHEN** 沒有提供測試玩家 cookie
- **THEN** 需要登入的項目輸出「未驗證」並說明原因，其餘項目照常檢查，結束代碼不因此非零

#### Scenario: 缺少標頭
- **WHEN** 目標的首頁回應沒有 HSTS
- **THEN** 該項輸出失敗並指出標頭名稱，結束代碼非零

#### Scenario: 預設不呼叫模型
- **WHEN** 沒有提供同意參數
- **THEN** 腳本不送出任何對話訊息，輸出說明略過模型呼叫

#### Scenario: 輸出不含機密
- **WHEN** 檢視完整輸出
- **THEN** 沒有 cookie 值、CSRF 權杖、密碼或回應本文

### Requirement: 預備環境不使用真實個資
預備環境與壓測環境 SHALL NOT 含真實使用者的個資：自動化建立的帳號只能是保留測試網域（`.invalid`）的測試帳號，資料庫 SHALL NOT 由正式環境複製而來。部署流程 SHALL NOT 提供把正式資料還原到預備環境的步驟。人工驗收所用的真實 Google 帳號屬於驗收者本人，資料於驗收後清除。

#### Scenario: 測試帳號網域
- **WHEN** 壓測與冒煙驗收簽出測試玩家
- **THEN** 信箱都屬於保留的 `.invalid` 網域

#### Scenario: 沒有複製正式資料的步驟
- **WHEN** 檢視預備環境的所有部署與還原步驟
- **THEN** 沒有任何一步以正式環境的備份或資料庫為來源

### Requirement: Google 登入在預備環境以人工驗收
預備環境 SHALL 有專用的 Google OAuth 用戶端（網頁應用程式型；授權重新導向 URI 為預備環境網址加 `/auth/google/callback`），SHALL NOT 重用開發用的用戶端，也 SHALL NOT 重用 Firebase 自動建立的用戶端。因為真實的 Google 登入需要使用者本人的 Google 帳號，驗收 SHALL 由使用者人工進行並記錄：新使用者未勾選條款被擋、勾選條款後首次登入（建立沒有密碼的帳號並導向年齡確認頁）、年齡確認（含不一致）、登出後的一鍵再次登入（辨識為同一個帳號、不重複建立）、帳號鎖定與申訴、管理員先 Google 再密碼的登入。沒有人工驗收時 SHALL 標為「未驗證」，SHALL NOT 以自動化測試或預期結果代替。

#### Scenario: 專用用戶端
- **WHEN** 檢視預備環境的 Google 設定
- **THEN** 用戶端識別碼與開發環境及 Firebase 自動建立的不同，授權重新導向 URI 只有預備環境的回呼網址

#### Scenario: 首次與再次登入
- **WHEN** 使用者以真實 Google 帳號在預備環境登入兩次
- **THEN** 第一次在勾選條款後建立沒有密碼的帳號並導向年齡確認頁，補填後進入客服頁；第二次不必勾選條款，辨識為同一個帳號而不重複建立

#### Scenario: 沒做就標未驗證
- **WHEN** 使用者不在場而沒有進行人工驗收
- **THEN** 驗收紀錄標為「未驗證」，上線前關卡不得因此標為已通過
