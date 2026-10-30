# Proposal

## Why

AGENTS.md 第 11 節要求七項告警（5xx 比率、對話延遲 p95、模型錯誤與 429 比率、每日 token 用量、登入失敗激增、寄信失敗、服務存活），管道為電子郵件或 webhook。目前**一項都沒有**：模型錯誤、用量、鎖定都只是日誌事件或資料庫裡的紀錄，沒有人會被通知。`usage.global_budget_warning` 甚至刻意「每個請求都記一筆、由告警層去重」，而告警層不存在。

這在個人測試階段無妨，但客服對外開放前，金鑰被撤銷、模型變慢、免費層配額快用完、有人在試探登入，都必須有人在使用者抱怨之前知道。C-4c 已把個資生命週期做完，告警是 C-5 上線前的最後一塊營運基礎。

## What Changes

- **新增 `flask check-alerts [--dry-run]`**：由平台排程器每 5 分鐘觸發（C-5 設定；不使用行程內計時器），評估下列**只靠資料庫就量得到**的告警，狀態有變化時寄電子郵件：
  - **全站用量**達上限的 80%（警告）與 100%（嚴重）：token 與呼叫次數各自評估，取代「每個請求都記一筆警告、由告警層去重」。
  - **模型錯誤率**：近 5 分鐘的對話呼叫中，429、逾時、伺服器錯誤與網路錯誤的比率超過 10%（樣本至少 10 次）。
  - **模型設定錯誤**：近 5 分鐘出現任何一次設定錯誤（400、403；多半是金鑰被撤銷或設定壞了）。
  - **對話延遲**：近 15 分鐘完成的對話呼叫的 p95 超過 10 秒（樣本至少 20 次）。
  - **登入失敗激增**：目前被鎖定的帳號達 5 個（鎖定 15 分鐘，所以就是「近 15 分鐘新增的鎖定」）。
- **告警有狀態**：新增資料表 `alert_states`（migration `0009`）。進入告警時寄一封、嚴重度升高時再寄、持續中每 6 小時提醒一次、恢復時寄「已恢復」；不會每個請求或每次檢查都寄。寄信失敗不標記為已通知，下次檢查重試，指令以非零代碼結束。
- **收件人與設定**：`ALERT_EMAIL_TO`（逗號分隔，至多 5 個地址），沿用現有的 SMTP 寄信器與 `MAIL_*` 設定；staging 與 production 沒有設定收件人或寄信設定時啟動失敗。
- **日誌事件**：`alert.firing`、`alert.resolved`、`alert.delivery_failed`、`alert.check_failed`（只有告警代碼、嚴重度與整數；不含收件人地址）。
- **不在本 change（依使用者決定）**：5xx 比率、服務存活、寄信失敗由平台以日誌或探測為基礎的告警負責（C-5 設定，條件寫進營運文件）；不新增登入失敗或寄信失敗的歷史資料表；不做 webhook；不做後台的告警頁面。
- 營運文件新增「告警條件一覽」：本指令的五項與平台的三項各自的條件、來源與歸屬。

## Capabilities

### New Capabilities
- `operational-alerts`: 告警的評估條件與門檻、狀態與去重（進入、升級、提醒、恢復）、電子郵件的通知內容與失敗處理、排程指令與預覽、日誌與輸出不含個資。

### Modified Capabilities
- `app-configuration`: 新增告警收件人設定 `ALERT_EMAIL_TO` 的格式與上限，以及 staging 與 production 的必填與寄信設定相依。
- `service-observability`: 新增告警事件（`alert.firing`、`alert.resolved`、`alert.delivery_failed`、`alert.check_failed`）只含固定列舉與整數的要求。

## Impact

- **新增**：`app/models/alert_state.py` 與 migration `0009`；`app/repositories/alert_state_repository.py`；`app/services/alert_rules.py`（純函式：門檻與判斷）、`app/services/alert_service.py`（評估、狀態轉換、寄信）；`app/observability/alert_events.py`；`flask check-alerts`；`LlmCallLogRepository` 與 `UserRepository` 各增加唯讀的彙總查詢。
- **修改**：`app/config.py`（`AlertSettings`、寄信設定的相依）、`.env.example`、`app/services/registry.py`、`app/cli.py`、`tests/support/config_env.py`（staging 與 production 的測試環境補上收件人）。
- **文件**：營運與部署（告警條件一覽、指令、事件）、資料模型（`alert_states`、migration `0009`）、系統架構、路線圖、README；`tests/test_design_docs.py` 會檢查資料表、指令與 migration 版本。
- **風險**：告警檢查本身失敗（資料庫不通）就無法通知，這正是「服務存活」要由平台外部探測的原因；告警信件不得含任何個資或對話內容（只有告警名稱、數值與門檻）。未動到 AGENTS.md，只是補齊第 11 節已訂的告警。
