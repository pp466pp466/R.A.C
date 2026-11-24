# Design

## Context

動機與範圍見 `proposal.md`；行為契約見 `specs/` 的各個 delta。規範來源是 AGENTS.md 第 10 節「管理員刪除帳號」與第 11 節（提交 `99022ff`，使用者已核可）。

現況（已讀程式確認）：

- `AdminService`（`app/services/admin_service.py`）已有一套危險操作的固定做法：`require_admin` → 驗證輸入（`_reason`）→ 找目標（`_manageable_target`）→ `_confirm`（重新輸入密碼；失敗寫 `reauth.failed` 並以 `persist_on_error` 保存）→ 改資料 → `_record` 寫稽核（與動作同一個交易）。`_manageable_target` 對管理員 403 `admin_target_forbidden`、對 `deletion_pending` 回 409。
- 全站只有**一個**抹除出口 `AccountErasureService.erase(user_id, delete=…, reason=…, now=…, retain_scripts=…)`：在一個交易內做「清檢舉與來源參照、清帳本對話編號、條件式刪除、寫 `deletion_tombstones`、**提交**」，條件不成立時整個回復。使用者自助、閒置 12 個月、未滿 18 歲、逾時未確認、還原後重抹都走它；`tests/architecture/test_account_erasure_coverage.py` 要求新資料表登記抹除範圍。對話、存檔、錢包、劇本等靠資料庫的 `ON DELETE CASCADE`；帳本與稽核靠 `SET NULL`。
- 抹除出口的刪除條件由呼叫者以 `UserRepository` 的條件式 ORM `DELETE` 傳入（如 `delete_unconfirmed_player`：`role != admin`）。
- 後台前端的危險操作共用 `CONFIRMS` 規格表與一個確認 Modal（理由、密碼、錯誤顯示、送出後立即清空密碼）；稽核動作的中文標籤與細節在 `admin-format.js`。
- `admin_audit_logs.action` 與 `deletion_tombstones.reason` 是 `Unicode` 字串欄位，**沒有資料庫層的列舉約束**，列舉只在程式（`AUDIT_ACTIONS`、`DELETION_REASONS`）與守門測試。

## Goals / Non-Goals

**Goals:**
- 管理員能在後台一次刪一個玩家帳號，立即且不可復原地抹除，與其他抹除路徑的結果一致（同一出口、同一份範圍、同樣留刪除紀錄）。
- 稽核與抹除原子：有抹除就有稽核，抹除失敗則兩者都不存在。
- 不放寬管理員的可見範圍（不新增使用者清單）。

**Non-Goals:**
- 批次刪除、使用者清單、復原、7 天寬限期、通知被刪使用者。
- 新資料表、欄位或遷移；改變其他抹除路徑的行為。
- 展示環境 rac001 的實際清理（部署後的維運操作，另行確認）。

## Decisions

### D1：新增 `AccountErasureService.erase_by_admin`，不另寫刪除流程
沿用單一出口：`erase_by_admin(user_id, *, now)` = `erase(user_id, delete=users.delete_player_by_admin, reason=REASON_ADMIN_DELETED, now=now, retain_scripts=False)`，與 `erase_underage` 並列。`retain_scripts` 不傳（預設 `False`，全刪），符合規範「管理員刪除一律全刪」。
- 替代：在 `AdminService` 內自己刪使用者（會繞過劇本與檢舉的無動作外鍵處理、帳本對話編號清除，已被 `account-deletion` 規格與架構測試排除）。**否決。**

### D2：條件式刪除 `UserRepository.delete_player_by_admin(user_id)`
`DELETE FROM users WHERE id = :id AND role != 'admin'`（以 ORM 表達式，`synchronize_session=False`，與 `delete_unconfirmed_player` 同型），回傳 `rowcount == 1`。**不限制狀態**：`active`、`suspended`、`locked`、`deletion_pending` 都可刪。管理員絕不刪（資料庫層的最後一道防線，Service 層另有 403）。
- 替代：沿用 `delete_unconfirmed_player`（多了 `birth_date IS NULL` 條件，已確認年齡的帳號刪不掉）。**否決。**

### D3：服務層流程與稽核的原子性
`AdminService.delete_user(admin, public_id, *, reason, password)`：
1. `require_admin`；`_reason(reason)`；`_find_user`（404）；`role != user` → `AdminTargetForbidden`（**不**用 `_manageable_target`，因為它會擋 `deletion_pending`，而這裡允許）。
2. `_confirm(admin, password, ACTION_USER_DELETE, target_user=user)`（失敗沿用 `reauth.failed` 路徑）。
3. `_record(admin, ACTION_USER_DELETE, target_user=user, reason=…, detail={"from_status": user.status})`（`changed=False`，先不記日誌）。
4. `erasure.erase_by_admin(user.id, now=clock())`。`erase` 內部**提交**：稽核列與抹除一起進同一個交易；回傳 `False`（條件不成立，例如在檢查與刪除之間被改成管理員）時 `erase` 已回復整個交易，稽核列也一併消失，服務層拋 `Conflict(code="state_changed")`。
5. 成功後才呼叫 `admin_events.log_action`（日誌在提交之後，內容只有管理員編號與動作名稱）。
- 取捨：稽核必須在 `erase` 提交之前寫入（`erase` 在內部提交），所以順序是「先 `_record` 再 `erase_by_admin`」，與其他操作「改資料後 `_record`」相反；`AdminAuditRepository.add` 只 `add`（不提交）才成立，實作時以測試釘住「抹除失敗則沒有稽核列」。

### D4：稽核內容與日誌
- 動作 `user.delete`（`AUDIT_ACTIONS` 23→24、`DANGEROUS_ACTIONS` 加入）；`target_user_public_id` 是被刪者的對外識別碼（不設外鍵，使用者刪除後紀錄仍成立）；`reason` 是管理員填的理由；`detail_json` 只有 `{"from_status": <狀態列舉>}`。**不存** email、暱稱、劇本或任何使用者文字。
- 結構化日誌沿用 `admin.action`（原因欄位是動作名稱），不新增事件；不記 email。

### D5：刪除紀錄原因 `admin_deleted`
`REASON_ADMIN_DELETED = "admin_deleted"`（13 字元，欄位 `Unicode(30)`），加入 `DELETION_REASONS`。還原後的重抹（`deletion_replay`）以編號重抹、不看原因，行為不變。**不需要遷移**（欄位無約束）。`tests/support/db_schema.py` 的列舉對照與 `docs/project資料結構文件.md` 要同步新值。

### D6：前端
- `userActions.delete = manageable`（角色為 `user`；含 `deletion_pending`、`suspended`、鎖定的帳號）。
- `CONFIRMS.delete`：`needsReason: true`；標題「永久刪除這個帳號？」；介紹文字寫明**不可復原**，列出連帶刪除的資料（登入身分、對話、存檔與記事簿、遊戲幣、全部劇本含已上架者、檢舉與申訴），並說明帳號持有的劇本會一併消失；確認鈕「確認永久刪除」，使用 `admin-btn--danger` 樣式；沿用密碼欄與錯誤顯示。
- 成功後：清空 `user`（面板）、從 `locked`、`suspended`、`appeals` 清單中移除該帳號的列（以 `public_id` 比對，沿用 `unlock` 與 `lift` 的 `removeXxxFromLists` 做法），並把 `flagged`、`feedback`、`overview`、`audit` 標為過期（下次進入時重新載入，因為被刪帳號的對話已一併消失）；顯示 Toast「已刪除帳號。」。成功回應沒有 `user`，所以 `submitConfirm` 要新增 `kind === 'delete'` 的分支，不能落入預設分支的 `user.value = data.user`。
- 稽核分頁的動作標籤加 `'user.delete': '刪除帳號'`，細節顯示「原狀態：…」（沿用 `user.suspend` 的格式函式）。
- 不新增使用者清單；入口仍是 email 查詢與警告清單。

### D7：服務條款
`terms.html` 第 4 節（使用規範）加一條：「違反內容規範或本條款時，管理員得刪除您的帳號與其全部資料，刪除後無法復原。」遞增 `CURRENT_TERMS_VERSION`（依既有慣例，每次政策頁文字變動就遞增 `draft-N`，現為 `draft-10` → `draft-11`）；隱私權政策不需要改（刪除範圍已說明）。既有使用者不需要重新同意（與過去修改條款的慣例相同；實作時以 `git log -- app/services/legal.py` 再確認慣例）。

### D8：測試策略
- Service 單元測試（假 Repository 或 SQLite 實例）：各種目標（一般、`deletion_pending`、管理員、自己、不存在）、密碼錯誤、理由缺漏、稽核與抹除原子（用注入失敗的 `erase` 驗證稽核不存在）、劇本全刪不看偏好、刪除紀錄寫入、帳本匿名化。
- API 測試：路由、CSRF、非管理員 404、回應形狀。
- SQL Server 軌：抹除含劇本與檢舉的帳號（既有的 `erasure` 雙軌測試擴充一條管理員路徑）。
- 前端：`admin-format.js` 的標籤與細節（Node）、`admin.js` 的 `CONFIRMS.delete` 規格與成功後清除（沿用既有後台測試的方式）。
- 既有守門：稽核動作數（`tests/test_admin_audit_repository.py` 的 23）、`test_readme` 的端點表與動作清單、`test_design_docs`、`tests/test_project_db_schema_doc.py`、`tests/test_agents_skill_sync.py` 已更新。
- 變異驗證於最後一批：條件式刪除的管理員保護、目標角色判斷、稽核原子順序、劇本不保留。

## Risks / Trade-offs

- **R1：被刪帳號正在生成回合**：串流中的請求之後寫入回合會因外鍵失敗而中止，只影響那一個請求（該玩家的帳號已不存在）。接受；不為此加鎖。實作時確認該路徑的例外被既有的串流收尾處理（finally 解鎖、關閉上游串流）吞下並記錄，不留下孤兒預扣（錢包已隨帳號刪除）。
- **R2：不可復原**：沒有寬限期與復原。緩解：密碼加理由兩道確認、Modal 明確警告、稽核留下「誰、何時、刪了誰」；展示環境的清理前先做 RDS 快照（維運步驟，不在程式內）。
- **R3：全站只剩管理員時的操作**：沒有保護「至少保留一位玩家」之類的限制，這是刻意的（目的之一就是清空展示環境）。
- **R4：稽核先於抹除寫入的順序**依賴 `AdminAuditRepository.add` 不提交；若日後有人改成自行提交，原子性會破功——以 D8 的測試釘住。
- **R5：同一位管理員的並行重複請求**：第二個請求在 `_find_user` 找不到而得 404，無害。
