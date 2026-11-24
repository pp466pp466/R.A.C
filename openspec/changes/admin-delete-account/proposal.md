# Proposal

## Why

管理員後台目前只能停權、解除、調整等級與遊戲幣、解除鎖定與暫停發佈，**沒有刪除帳號的功能**：要移除一個帳號（違規帳號、展示環境要清掉測試帳號）只能等使用者自己申請刪除，或寫一次性腳本直接動資料庫。使用者要求後台能刪帳號，並已核可規範（AGENTS.md 第 10、11 節，提交 `99022ff`）與三個決定：一次刪一個、立即抹除、被刪帳號的劇本一律全刪。

## What Changes

- 新增管理員操作「刪除帳號」：`POST /api/admin/users/<public_id>/delete`，要求管理員密碼與理由（1 至 200 字），成功回 `{ok: true}`。
  - 只能刪角色為 `user` 的帳號（管理員含自己 403 `admin_target_forbidden`）；目標不存在 404；狀態為 `deletion_pending` 的帳號**允許**刪除（使用者本來就要刪，管理員可直接抹除，不必等寬限期）。
  - **立即抹除**：走既有的單一抹除出口 `AccountErasureService.erase`（與未滿 18 歲抹除同一條路），不套用「匿名保留我已發佈的劇本」偏好，劇本全刪；寫入 `deletion_tombstones`（新原因 `admin_deleted`）。
  - 稽核動作 `user.delete`（新增列舉值）與抹除在**同一個交易**；稽核只存被刪帳號的對外識別碼、理由、原狀態，不存 email 或暱稱。
- 後台使用者面板新增「刪除帳號」按鈕與確認 Modal（沿用既有的危險操作確認元件：理由＋密碼，Modal 內寫明不可復原與連帶刪除的資料）；刪除成功後清除面板與各清單中的該帳號。
- 服務條款（`/terms`）第 4 節補一句「違反內容規範時管理員可刪除帳號」，並遞增條款版本。
- **不做**：使用者清單或批次刪除（維持「一次一個、以 email 查詢」，管理員可見範圍不變）、7 天寬限期或復原、通知被刪的使用者（email 抹除後也無從寄送）、新增資料表或欄位。
- 展示環境 rac001 的帳號清理是**部署之後的維運操作**，不屬於本 change 的程式範圍；執行前另行向使用者確認並先做 RDS 快照。

## Capabilities

### New Capabilities
（無：全部是對既有能力的擴充。）

### Modified Capabilities
- `admin-user-management`：新增「刪除帳號」需求（目標規則、密碼與理由、立即抹除、回應）；「危險操作須重新輸入密碼」納入刪除帳號。
- `admin-audit-log`：稽核動作列舉新增 `user.delete`，並規範其細節與同交易原子性。
- `account-deletion`：新增「管理員刪除帳號走同一個抹除出口」需求（刪除紀錄原因 `admin_deleted`、抹除範圍與使用者主動刪除相同、條件不成立時整個回復）。
- `script-anonymous-retention`：新增「管理員刪除不保留」需求（不讀偏好，劇本一律全刪）。
- `auth-pages`：新增「服務條款載明管理員可刪除帳號」需求（條款文字與版本）。

## Impact

- 後端：`app/models/admin_audit_log.py`（`ACTION_USER_DELETE`、`AUDIT_ACTIONS` 23→24）、`app/models/deletion_tombstone.py`（`REASON_ADMIN_DELETED`、`DELETION_REASONS`）、`app/repositories/user_repository.py`（條件式刪除一般使用者）、`app/services/account_erasure_service.py`（`erase_by_admin`）、`app/services/admin_service.py`（`delete_user`、注入抹除服務）、`app/services/registry.py`（組裝）、`app/observability/admin_events.py`（`DANGEROUS_ACTIONS`）、`app/controllers/admin_api_controller.py`（一條路由）、`app/services/legal.py` 與 `app/templates/legal/terms.html`。
- 前端：`app/static/js/admin/admin.js`（`CONFIRMS.delete`、按鈕、成功後清除）、`admin-format.js`（動作標籤與稽核細節）、`app/templates/admin/console.html`（按鈕）。
- **沒有資料庫結構變更、沒有遷移**：`admin_audit_logs.action` 與 `deletion_tombstones.reason` 都是無約束的字串欄位。仍須同步 `docs/project資料結構文件.md` 內這兩個欄位的列舉值（`tests/support/db_schema.py` 與守門測試會檢查）。
- 文件與測試：README 端點表與稽核動作數、設計文件（`docs/README.md` 對照表）、既有測試中寫死的稽核動作數（23）與路由數；簡報依 `deck-conflict-watch` 對照（只通知）。
- 風險：被刪帳號若正在串流生成回合，該請求會因外鍵失敗而中止（見 design R1）。
