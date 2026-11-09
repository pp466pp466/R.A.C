# Design

## Context

動機與範圍見 proposal.md，行為見六份規格。規範（AGENTS.md 第 6、7、10、11 節）已於 `51d4330` 修訂，探索結論在 `docs/RPG.md` 第 7.7 節與 `docs/report.md` U-31。

現況：
- **沒有任何錢包、帳本或簽到**；`UsageService`（客服的每日輪數與每分鐘訊息數）綁客服訊息，與這個 change 無關。
- 營運數值已有一套機制：`runtime_limits` 表（鍵加整數）、`RuntimeLimits`（每次現讀、缺值用保底預設、`set()` 驗證範圍並寫 `limit.set` 稽核）、`flask set-script-limit`／`show-script-limits`。目前的 `LIMIT_SPECS` 只有劇本與主角生成的十個鍵。
- 年齡確認在 `AuthService.verify_age`（補填成功後呼叫 `UserRepository.complete_age_verification_if_unset`，回傳是否真的完成）。`AuthService` 目前不依賴任何錢包元件。
- 台灣日界有現成的函式（`quota_policy.taipei_day_bounds`、`age_policy.TAIPEI`），資料庫存 UTC。
- 管理員的高風險操作有現成的樣式：`AdminService._confirm`（重新輸入密碼、失敗寫 `reauth.failed`）加 `_record`（稽核與動作同交易）；`/admin` 的 `admin.js` 有通用的確認對話框流程。
- 抹除一個人靠 `ON DELETE CASCADE`；`tests/architecture/test_account_erasure_coverage.py` 要求每張指向 `users` 的資料表登記。
- 遷移的寫法已確立（0013、0014）：只新增、`op.bulk_insert` 補設定、只插入缺少的鍵、降級可回復。

限制：AGENTS.md 第 4 節（純 ORM、不寫原生 SQL、字串用 Unicode、expand／contract）、第 9 節（不依賴行程內狀態，多 worker 一致）、第 10 節（高風險操作、稽核）、第 11 節（個資不進日誌、隨帳號刪除、納入匯出）。

## Goals / Non-Goals

**Goals:**
- 餘額在任何並行情況下都不為負、不超扣、不重複入帳；同一個冪等鍵的操作無論重送多少次結果相同。
- 錢包不依賴任何資料庫專屬功能（不用 `SELECT … FOR UPDATE`、不用原生 SQL），SQLite 與 SQL Server 行為一致。
- RC-2 只要呼叫 `hold`、`settle`、`refund`，不必知道桶、上限或逾時。

**Non-Goals:**
- 儲值、訂閱、付費幣的取得與退款規則（W2）；批次與到期；模型目錄與定價（W3，這裡只留模型鍵）。
- 玩家畫面（`player-hub`）、RPG 回合引擎與 SSE（RC-2）。
- 排程的逾時預扣清理、贈幣的農號防堵。

## Decisions

### D1. 資料模型與遷移 `0015_game_wallet`（只新增）
- `wallets`：`id`、`user_id`（`ON DELETE CASCADE`、唯一）、`free_balance`、`paid_balance`（皆 `Integer`、預設 0、`CHECK >= 0`）、`version`（預設 0，每次餘額異動加 1）、`created_at`、`updated_at`。
- `wallet_entries`：`id`、`user_id`（`ON DELETE CASCADE`）、`seq`（該使用者的第幾筆，等於寫入當下錢包的 `version`；`(user_id, seq)` 唯一）、`kind`（`Unicode(20)`）、`idempotency_key`（`Unicode(200)`、唯一）、`free_delta`、`paid_delta`、`free_after`、`paid_after`、`session_public_id`（`Unicode(36)`，可為空）、`ref_key`（`Unicode(200)`，可為空：退回列指向它所退回的預扣的冪等鍵）、`resolution`（`Unicode(10)`，可為空：只有預扣列使用，`settled` 或 `refunded`）、`created_at`。
- **用 `seq` 而不是內部整數編號分頁與排序**：記錄頁的游標是「該使用者的第幾筆」，不是全站遞增的主鍵，所以回應不洩漏任何內部編號；日後同步也可以用 `since_seq`（沿用客服的做法）。
- **存檔以對外識別碼（UUID）存放，不存內部編號**：AGENTS.md 第 11 節原文是「關聯的存檔內部編號」，使用者核可後（2026-11-09）已改為「關聯的存檔識別碼」；理由是帳本要比存檔活得久，而內部整數編號在 SQLite 會被重用而錯指別人的存檔（用量帳本的 `conversation_id` 就為此多了一整套清除流程）；UUID 沒有這個問題，也不是內部編號。
- **沒有批次、沒有到期欄位**：幣不過期（使用者決定），兩個桶加帳本就夠；W2 做「付費幣按購買退款」時再以 expand 加欄位。
- `CHECK (free_balance >= 0 AND paid_balance >= 0)` 是第二道防線（主要由條件式更新保證）；以 SQLAlchemy 的 `CheckConstraint` 表達，不是原生 SQL，兩個資料庫都支援。
- `runtime_limits` 以 `op.bulk_insert` 補五列（只插入缺少的鍵，沿用 0013、0014 的寫法）。
- 降級：刪兩張表與五個鍵。只在還沒有真實資料時使用，有資料時以備份還原（與前幾次遷移相同）。
- 替代方案：把餘額只由帳本加總得出（沒有 `wallets` 表）——每次查詢要掃全部帳本，且沒有可做 CAS 的單點；把兩個桶放 JSON 欄位——AGENTS.md 第 4 節不得依 JSON 內容做條件式更新，也無法在資料庫層擋負數。

### D2. 並行：以版本號做 CAS，不用資料庫專屬的鎖
所有改變餘額的操作走同一個內部迴圈（上限 5 次，之後拋出可重試的 `WalletBusy`，回應 503）：
1. 讀錢包（含 `version`）。
2. 依操作計算新餘額與帳本列（扣款拆分、簽到入帳量、退款原路）。
3. 在一個 SAVEPOINT 內：`UPDATE wallets SET … , version = version + 1 WHERE id = ? AND version = ?`；影響列數為 0 代表別人先改了 → 結束 SAVEPOINT，回到 1；為 1 才寫帳本列。
4. 帳本列的唯一鍵衝突（同一個冪等鍵並行）→ SAVEPOINT 回復（餘額更新一併復原），回傳既有的帳本列。

**SQLite 的 SAVEPOINT 陷阱**（實作時發現）：pysqlite 在第一個寫入之前不會開始交易，SAVEPOINT 若是交易裡的第一個陳述式就成為最外層交易，`RELEASE` 等於 `COMMIT`，外層請求之後回復時救不回來（SQL Server 沒有這個問題）。所以 `WalletRepository` 進入 SAVEPOINT 之前一律先做一次不影響任何列的條件式 UPDATE（`_savepoint()`），測試以外層回復驗證兩個資料庫行為一致；不改全站的 SQLite 引擎設定，那屬於另一個 change。

為什麼可行：兩個資料庫的條件式 `UPDATE` 都會對該列加寫鎖，後到者在前者提交後重新評估 `WHERE`，`version` 已變所以影響 0 列（SQLite 由寫入序列化與 `busy_timeout` 保證）。操作開始時先以冪等鍵查帳本，已有就直接回傳既有結果。**不是行程內鎖**：多個 worker 之間靠資料庫的列鎖與唯一約束，符合第 9 節。
替代方案：`SELECT … FOR UPDATE`（SQLite 沒有，會讓雙軌行為分歧）；只靠 `CHECK >= 0` 而不做 CAS（能擋負數，但擋不住「兩個請求各算出同一個拆分」造成的免費與付費桶錯配）。

### D3. 預扣、結算、退回
- **冪等鍵由呼叫端決定**，W1 只要求長度在 200 內；RC-2 的建議格式是 `turn:<存檔識別碼>:<client_msg_id>:<revision>:<嘗試序號>`。**同一個冪等鍵永遠是同一筆預扣**，失敗退回後要再預扣必須換新的鍵（RC-2 以嘗試序號區分），所以「淨扣一次」由呼叫端的鍵設計保證，W1 保證的是「同一個鍵只扣一次」。
- `hold(current, key, session_public_id, model_key='standard')`：幣價取 `coin.turn_cost.<model_key>`（模型鍵須在白名單內，否則 `ValidationError`）；拆分為「先免費、不足的部分付費」；帳本種類 `turn_hold`，`free_delta`／`paid_delta` 為負。回傳 `Hold(key, cost, free_taken, paid_taken, existed)`。餘額不足拋出 `InsufficientCoins(balance, needed)`（409 `insufficient_coins`，附合計餘額與所需，不洩漏兩桶的拆分以外的東西）。
- `settle(current, key)`：只做一件事——條件式 `UPDATE wallet_entries SET resolution='settled' WHERE user_id=? AND idempotency_key=? AND kind='turn_hold' AND resolution IS NULL`。影響 1 列成功；影響 0 列時查現況：已 `settled` 視為重送成功；已 `refunded` 拋出 `HoldAlreadyRefunded`；不存在或不屬於該使用者拋出 `NotFound`。餘額與版本都不動（不需要 CAS）。
- `refund(current, key)`：先把預扣列的 `resolution` 條件式從空值設為 `refunded`（搶到才繼續，這就是「與結算互斥」的唯一裁判），再走 D2 的迴圈入帳（`turn_refund` 列，冪等鍵 `refund:<預扣的鍵>`，`ref_key` 指向預扣，免費與付費各退預扣的拆分）。退回**不受免費幣上限限制**。同一交易內完成，所以不會出現「標記了卻沒入帳」。已 `refunded` 重送成功；已 `settled` 拋出 `HoldAlreadySettled`。
- 為什麼結算結果放在預扣列上而不是另寫帳本列：結算沒有餘額變動，寫一列只會產生零增減的雜訊與額外的 `seq`；一個只能從空值改一次的欄位，已足夠表達並且可由條件式更新裁判。代價是帳本不再「嚴格不可改」，規格已明寫這個唯一例外。

### D4. 逾時預扣：下一次存取時退回，不設排程
`_prepare(current)`（所有公開方法的第一步）依序：建立錢包（若沒有）→ 補發贈幣（若符合且未發）→ 退回逾時預扣。逾時預扣的查詢：該使用者 `kind='turn_hold' AND resolution IS NULL AND created_at < now - timeout`，每次最多處理 20 筆（有界），逐筆走 `refund`。逾時秒數取設定 `coin.hold_timeout_seconds`（初始 180，大於 RC-2 的 60 秒生成上限加狀態呼叫）。理由：預扣的受害者只有該使用者自己的餘額，他回來時才需要看到正確的數字；不回來的使用者不影響任何人。省下一個排程指令與平台排程設定。限制：長期不回來的使用者，管理員在後台看到的餘額會少那筆預扣——規格已接受，檢視餘額時**不**觸發清理（後台只讀）。

### D5. 贈幣與簽到
- **贈幣**：`ensure_signup_grant(current)` 條件為「角色是玩家、生日已存在（即完成年齡確認）、帳本沒有 `signup:<使用者內部編號>` 這個鍵」。發放量 = `min(設定的贈幣數, max(0, 上限 - 免費餘額))`，經 D2 的迴圈寫 `signup_grant` 列。呼叫點兩處：①`AuthService.verify_age` 在 `complete_age_verification_if_unset` 回傳成功後，透過注入的回呼 `on_age_verified`（registry 組裝，模式同 `erasure`）呼叫，**包在 SAVEPOINT 與 try/except 內**：失敗只記錄事件、不影響年齡確認；②`_prepare`（既有玩家與失敗補發）。冪等鍵含使用者內部編號但**帳本不對外**，所以不洩漏。
- **簽到**：日期取 `taipei_day_bounds(now)` 的台北日期字串，鍵 `checkin:<使用者內部編號>:<YYYY-MM-DD>`。`checkin(current, confirm_overflow)` 在 D2 的迴圈**內**重算入帳量（`min(簽到數, max(0, 上限 - 免費餘額))`）：入帳量小於簽到數且沒有確認 → 拋出 `CheckinOverflow(would_gain, would_lose)`，**在寫入任何東西之前**；有確認則入帳（可為 0，當日視為已簽到）。預覽（`view` 內的 `checkin` 區塊）用同一個純函式計算，所以「預覽與實際入帳」不會分歧；確認時以當下餘額重算，不信任預覽。
- 設定值（贈幣數、簽到數、上限、逾時、幣價）每次操作開始時**現讀一次**並在該次迴圈內固定使用，不在重試之間改變。

### D6. 設定機制：擴充 `runtime_limits`，不另建資料表
`LimitSpec` 新增 `group`（`script` 或 `wallet`）。`LIMIT_SPECS` 加五個 `wallet` 鍵（範圍見規格）；`RuntimeLimits` 新增 `coin_turn_cost(model_key)`、`coin_signup_grant()`、`coin_checkin_grant()`、`coin_free_cap()`、`coin_hold_timeout()`。`set(key, value, *, group)` 與 `rows(group)` 加分組參數；新指令 `set-wallet-setting`／`show-wallet-settings` 傳 `wallet`，既有兩個指令傳 `script`，鍵屬於另一組時以繁體中文錯誤指出該用哪個指令。理由：機制（每次現讀、保底預設、稽核、範圍驗證）完全相同，另建表只是複製；分組是為了讓既有指令與測試的輸出（十個鍵）不變。保底預設與遷移初始值由既有的一致性測試比對。
- 模型鍵白名單 `WALLET_MODEL_KEYS = ('standard',)` 定義在錢包 Service 的模組層（常數，不是設定）：新增模型是程式與設定表一起新增的事件（W3）。

### D7. API 與 Service 分層
- `wallet_api_controller.py`（`/api/wallet`）只讀請求、呼叫 `WalletService`、回 JSON；`require_login()` 預設拒絕受限登入狀態（鎖定、待確認年齡、等待刪除），不另做 `require_chat_access`（這不是 AI 功能）。回應前綴 `/api/wallet/` 加進 `cache_headers.py` 的禁止快取清單。`POST` 由既有的 `before_request` 統一檢查 CSRF。
- 新錯誤類別（`app/services/errors.py`）：`InsufficientCoins`（409）、`CheckinOverflow`（409，帶預覽）、`AlreadyCheckedIn`（409）、`HoldAlreadySettled`、`HoldAlreadyRefunded`、`WalletBusy`（503）；訊息一律繁體中文。
- `WalletRepository`（所有方法帶 `user_id`）是唯一寫入 `wallets`、`wallet_entries` 的地方，由新增的架構測試掃描原始碼保證（比照 `test_script_state_writes.py`）。管理員的唯讀餘額查詢走 `AdminRepository` 的專用方法。

### D8. 管理員調整與稽核
`AdminService.adjust_wallet(admin, public_id, *, bucket, delta, reason, password)`：`require_admin` → 驗證欄位（`bucket`、非零整數 `delta`、理由 1 至 200 字，沿用 `_reason`）→ `_manageable_target`（只能操作一般使用者）→ `_confirm`（密碼）→ `WalletService.adjust(target, bucket, delta)`（D2 的迴圈，冪等鍵 `admin:<隨機 UUID>`，每次調整都是新的一筆）→ `_record(wallet.adjust, detail={bucket, delta, balance_after})`，**與調整在同一個交易**（稽核與動作同時成立）。餘額會低於 0 拋出 `InsufficientCoins`。`_user_view` 多帶兩個餘額（`AdminRepository` 的唯讀查詢；沒有錢包時為 0，**不建立錢包**）。`/admin` 使用者詳情新增「調整遊戲幣」：沿用 `admin.js` 現有的確認對話框（新增 `wallet` 種類：餘額種類下拉、增減量、理由、密碼），格式化函式放 `admin-format.js` 並有 Node 測試。

### D9. 記錄頁的顯示與匯出
- 記錄查詢排除「已退回的預扣」與所有 `turn_refund` 列（淨額為零），其餘依 `seq` 由新到舊；游標是 `seq`（`before_seq`）。`WalletMessages.label(kind, resolution)` 提供繁體中文說明（`turn_hold` 且 `resolution` 為空 → 「處理中」，已結算 → 「冒險回合」）。
- 匯出走唯讀的 `export_wallet(user_id)`：沒有錢包回傳 `None`，匯出端據此輸出 0 與空帳本，**不建立錢包**；帳本含全部列（含退回），不含冪等鍵與 `ref_key`。

### D10. 日誌
`app/observability/wallet_events.py`（模式同 `script_events`）：函式的參數只有 `user_id`、整數與列舉，不接收任何文字參數，所以不可能把冪等鍵、存檔識別碼或理由寫進日誌；`tests` 掃描完整流程的日誌輸出。

## Risks / Trade-offs

- **讀取路徑會寫入**（`GET /api/wallet` 可能補發贈幣、退回逾時預扣）→ 規格已明寫；每次最多處理有限筆；兩者都冪等，重複請求不會重複入帳。這也代表錢包查詢不是純粹的唯讀請求，不能用唯讀連線。
- **CAS 在高競爭下可能耗盡重試**（同一位玩家同時有多個請求）→ 重試 5 次後回 503 `WalletBusy`；玩家的並行本來就被「同一存檔同時只處理 1 則」與前端按鈕禁用限制，實際競爭很低。
- **贈幣農號**（每天 200 幣）→ 已由使用者決定列為監控項目、不設關卡；帳本可彙總「每日發放量」與「達上限比例」，本 change 的任務包含一個唯讀的彙總查詢方法供日後使用，但不新增指令或畫面。
- **回呼失敗造成已確認年齡卻沒有贈幣** → 由 `_prepare` 補發（同一個冪等鍵，不會重複）。
- **`seq` 等於 `version`**：只要每次異動都經過 D2 的迴圈，序號就連續無洞；結算標記不改 `version`，所以不產生洞。若日後有人繞過迴圈直接改餘額，序號會出現缺口但不會重複，且架構測試禁止繞過。
- **預扣逾時的管理員檢視落差**（D4）→ 接受；調整餘額前管理員可請玩家重新整理。
- **不批次的代價**：W2 若要做「按購買退款」要補結構 → expand 階段加欄位即可，已知且接受。
- **AGENTS.md 措辭差異**（存檔識別碼 vs 內部編號）→ 見 D1，任務最後一組請使用者核可。

## Migration Plan

1. 備份資料庫（本機開發的 SQL Server `project` 由使用者自行備份、`COPY_ONLY` 加 `CHECKSUM`，與前幾次相同）。
2. `alembic upgrade head`（`0014` → `0015`）：新增兩張表與五個設定；舊版程式不認識它們，不受影響（expand）。
3. 部署新版程式。既有已完成年齡確認的玩家在第一次存取錢包時補發贈幣，不需要資料遷移。
4. 回復：`alembic downgrade` 刪兩張表與五個鍵（錢包資料一併消失，只在沒有真實資料時使用）；程式回到舊版時，沒有任何舊程式讀取這些表。
5. 使用者升級本機 `project` 資料庫之後，依專案慣例同步 `docs/project資料結構文件.md`（字典、外鍵、migration 歷程）並在第 14 節追加異動紀錄。

## Open Questions

- 贈幣農號的監控彙總（每日發放量、新帳號數、達上限比例）要在 `player-hub` 之後做成後台頁面，還是只用資料庫查詢？不影響本 change 的行為與任務，等有真實資料再決定。
