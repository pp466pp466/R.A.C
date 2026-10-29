# Spec Delta

## ADDED Requirements

### Requirement: 前端區分用量相關的拒絕並不自動重送無法立即成功的訊息
前端 SHALL 依錯誤代碼處理用量拒絕：`rate_limited` 與 `system_busy` 為暫時性，訊息保留並隔一段時間自動重送；`daily_quota_exceeded`、`chat_suspended` 與 `conversation_limit_reached` 在短時間內不會成功，訊息 SHALL 標記為無法送出、不自動重送，並在訊息下方顯示伺服器回覆的繁體中文說明，讓使用者可以重試或移除。

#### Scenario: 每分鐘限制
- **WHEN** 送出訊息得到 429 `rate_limited`
- **THEN** 訊息保留為待送，稍後以同一個識別碼自動重送

#### Scenario: 今日額度用完
- **WHEN** 送出訊息得到 429 `daily_quota_exceeded`
- **THEN** 訊息標記為無法送出且不自動重送，訊息下方顯示額度已用完的說明

#### Scenario: 對話被暫停
- **WHEN** 送出訊息得到 403 `chat_suspended`
- **THEN** 訊息標記為無法送出，頁面顯示暫停與恢復時間的提示，且輸入區不再接受送出直到截止時間

### Requirement: Sidebar 顯示今日剩餘輪數
`/cs` 的 Sidebar 底部 SHALL 在會員等級標籤旁顯示今日剩餘的對話輪數，資料取自 `GET /api/cs/usage`，並在每則回覆之後更新。取得失敗或離線時 SHALL 隱藏這一行而不影響其他功能。

#### Scenario: 顯示剩餘
- **WHEN** free 會員今日已用 3 輪，開啟 `/cs`
- **THEN** Sidebar 顯示今日剩餘 27 輪

#### Scenario: 取得失敗
- **WHEN** 用量查詢失敗
- **THEN** 該行不顯示，對話與送出照常運作
