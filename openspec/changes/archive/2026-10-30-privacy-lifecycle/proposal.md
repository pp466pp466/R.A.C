# Proposal

## Why

隱私權政策已經向使用者承諾「匯出我的資料」「刪除帳號（7 天寬限）」與各類資料的保存期限，但程式只做了兩件事：閒置 12 個月的帳號清除，以及使用者主動刪除單一對話。其餘（`deletion_pending` 狀態、匯出、對話／帳本／權杖／稽核／失敗原文的期限清除）都還沒有，而且資料模型文件與營運文件都標明「由 C-4c 實作」。

客服對外開放前必須補齊，否則政策頁的承諾與實際行為不符，也沒有任何機制讓使用者行使個資法的查詢、複製與刪除權。這也是 RPG 系列（RC-1 起）的前提：RPG 會產生更多個人內容（存檔、角色描述，甚至成熟分級的紀錄），刪除與匯出必須先有一個所有資料表都遵守的共同出口，而不是每個新功能各做一套。

另有一個既有缺口要一併補上：`purge-inactive-accounts` 刪除使用者時，沒有先把用量帳本（`llm_call_logs`）的 `conversation_id` 清為空值。該欄位刻意不設外鍵，SQLite 重用整數編號時會錯指別人的新對話；`llm_call_logs` 的模型註解與資料模型文件都寫明「帳號刪除流程須先做同樣的清除」，目前的閒置清除沒有做到。

## What Changes

- **自助刪除帳號**：登入使用者可申請刪除，帳號進入 `deletion_pending`（立即停用、既有登入狀態全部失效），7 天寬限期滿由排程指令真正刪除。有密碼的帳號須重新輸入密碼，只靠 Google 登入的帳號須輸入固定的確認文字；管理員帳號不可自助刪除。
- **寬限期內撤銷**：`deletion_pending` 的帳號登入（密碼或 Google）成功後只得到**受限的登入狀態**，只能看到帳號頁上的「撤銷刪除」與登出，其餘頁面與 API 一律拒絕。撤銷後回到申請前的狀態（使用中或待驗證信箱）。停權中的帳號無法登入，因此不在自助刪除的範圍內，仍由 12 個月閒置清除處理。
- **執行刪除的排程指令**：`flask execute-account-deletions`，寬限期滿的帳號被刪除，並寫入刪除紀錄（原因 `user_requested`）。閒置清除與這支指令共用同一個「抹除一個人的資料」流程：先把帳本的對話編號清為空值、再刪使用者（對話、訊息、第三方身分、權杖以資料庫串聯刪除）。
- **匯出個人資料**：登入使用者可匯出自己的帳號資料（含生日與年齡區間）與全部對話。JSON 為 UTF-8；CSV 為 UTF-8-sig，因為帳號與訊息是兩種形狀，CSV 以 zip 包含兩個檔案。匯出不含密碼雜湊、權杖、`token_version` 與內部整數編號。
- **期限清除**：`flask purge-expired-data`，一次處理五類資料——對話與訊息（最後活動後 12 個月）、`llm_call_logs`（12 個月）、失敗的模型原文 `error_detail`（14 天，清為空值、保留該筆帳本）、`auth_tokens`（到期後 7 天）、`admin_audit_logs`（24 個月）。支援預覽模式，輸出只有筆數。
- **帳號頁**：新增「匯出我的資料」「刪除帳號」與寬限期中的「撤銷刪除」介面。
- **隱私權政策頁**：移除「匯出與刪除帳號功能將於後續版本提供」的註記，改述實際行為；政策版本同步更新。
- 資料庫新增兩個可為空的欄位 `users.deletion_requested_at`、`users.status_before_deletion`（migration `0008`，expand 方式，新舊程式都能運作）。
- **不在本 change**：監控與告警（5xx、延遲、Gemini 錯誤、登入失敗激增、寄信失敗）依使用者決定拆為 C-4d `ops-alerting`；備份與還原演練屬 C-5。

## Capabilities

### New Capabilities
- `account-deletion`: 使用者自助申請刪除、寬限期、受限登入與撤銷、期滿由排程指令抹除全部個人資料、刪除紀錄；管理員帳號不可自助刪除。
- `personal-data-export`: 使用者匯出自己的帳號資料與全部對話（JSON 與 CSV 的格式、編碼、欄位範圍與不外洩的欄位）。
- `data-retention`: 各類資料的保存期限與清除（對話、用量帳本、失敗原文、權杖、管理稽核），以排程指令執行、可預覽、可重複執行、輸出不含個資。

### Modified Capabilities
- `account-retention`: 閒置清除改走共用的抹除流程（含對話與帳本匿名化的順序要求）；刪除紀錄的原因增加 `user_requested`。
- `session-authentication`: `deletion_pending` 帳號不再被拒絕登入，而是得到只能撤銷刪除的受限登入狀態；`suspended` 仍拒絕。
- `google-sign-in`: 同上，Google 登入遇到 `deletion_pending` 帳號的結果。
- `access-control`: 需要登入的端點預設拒絕受限登入狀態，只有明確允許的端點可用。
- `auth-pages`: 帳號設定頁增加匯出、刪除帳號與撤銷刪除；隱私權政策頁的相關敘述。
- `service-observability`: 新增刪除、撤銷、匯出與期限清除的事件（只含固定列舉與內部編號）。

## Impact

- **新增**：`app/services/account_erasure_service.py`（單一抹除出口）、`account_deletion_service.py`、`personal_export_service.py`、`data_retention_service.py`；對應的 Repository 方法；`/api/account/*`（刪除申請、撤銷、匯出）；`app/observability` 事件；兩個 `flask` 指令；`app/static/js/auth/account.js` 與 `account.html` 的新區塊；migration `0008`。
- **修改**：`AuthService`（`login`、`resolve_session`）、`GoogleSignInService`、`app/security/session.py`（受限登入狀態的辨識）、`AccessPolicy`（受限狀態）、`admin_service`（待刪除的處理不變）、`privacy.html`、`legal.py` 的政策版本、`account_retention_service.py`。
- **文件**：資料模型、帳號與安全、系統架構、營運與部署、路線圖（新增 C-4d）、README；`tests/test_design_docs.py` 會檢查路由、指令與 migration 版本。
- **相依與風險**：刪除是不可逆操作，抹除流程以「每個帳號一個交易」與刪除紀錄保證重跑安全；受限登入狀態是新的授權維度，必須由架構測試證明「預設拒絕」（任何未明確允許的 API 端點對受限狀態都回 403）。未動到 AGENTS.md 的規範，只是補齊第 10、11 節已訂的行為。
