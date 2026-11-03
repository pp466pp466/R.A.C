# admin-user-management Specification

## Purpose
定義管理員如何找到並處理單一使用者：以信箱精確查詢、停權與解除、調整會員等級，以及哪些帳號可以被操作、危險操作如何要求管理員重新輸入密碼。每個操作都可追溯，且管理員看到的使用者資料只有最小範圍。

## Requirements

### Requirement: 以信箱精確查詢使用者（次要入口）
管理員平常 SHALL 透過違反條例的警告找到使用者；以信箱查詢 SHALL 只用於客服反應的情形。`POST /api/admin/users/lookup` SHALL 接受 `email`，以與註冊相同的正規化（含 Gmail 的別名規則）做精確比對，找到時回傳該使用者的最小資料，找不到時回應 404。系統 SHALL NOT 提供列出全部使用者、模糊搜尋或依其他欄位搜尋的端點。信箱 SHALL 放在請求本文，SHALL NOT 出現在網址。每次查詢 SHALL 寫入一筆 `user.lookup` 稽核紀錄，細節註明途徑為信箱。

#### Scenario: 找到使用者
- **WHEN** 管理員以某位使用者註冊時的信箱查詢
- **THEN** 回應該使用者的最小資料，並新增一筆 `user.lookup` 稽核（途徑為信箱）

#### Scenario: 別名寫法
- **WHEN** 管理員查詢的 Gmail 位址與註冊時只差「.」或「+標籤」
- **THEN** 找到同一位使用者

#### Scenario: 找不到
- **WHEN** 管理員查詢一個沒有帳號的信箱
- **THEN** 回應 404，且不洩漏任何其他資訊

#### Scenario: 沒有瀏覽路徑
- **WHEN** 管理員以 GET 呼叫使用者清單、或以部分信箱搜尋
- **THEN** 這些端點不存在（404）

### Requirement: 以對外識別碼檢視使用者
`GET /api/admin/users/<public_id>` SHALL 回傳同一份最小資料，供從被標記或意見回饋的清單進入；不存在的識別碼 SHALL 得到 404。每次檢視 SHALL 寫入 `user.lookup` 稽核，細節註明途徑為識別碼（與以信箱查詢區分，管理員因此可以看出哪些查詢是例外的入口）。

#### Scenario: 從清單進入
- **WHEN** 管理員以被標記清單中的使用者識別碼檢視
- **THEN** 回應最小資料並寫入稽核（途徑為識別碼）

### Requirement: 使用者資料的最小範圍
管理員看到的使用者資料 SHALL 只含：對外識別碼、顯示名稱、email、角色、狀態、會員等級、信箱是否已驗證、建立時間、最後登入時間，以及該使用者今日的用量（已用輪數、上限、對話數、對話暫停的截止時間）。回應 SHALL NOT 含密碼雜湊、`token_version`、權杖或任何對話內容，且 SHALL NOT 含任何與年齡有關的個別資料：不含生日、年齡區間，也不含由它們推得的年齡群組（成年或未成年）、年齡或是否已完成年齡確認。管理員對年齡的了解 SHALL 只來自總覽的各區間人數彙總。

#### Scenario: 欄位範圍
- **WHEN** 管理員檢視一位已完成年齡確認的使用者
- **THEN** 回應只有上述欄位，沒有生日、年齡區間、年齡群組與密碼雜湊；回應中任何位置都找不到 `birth_date` 與 `age_range` 的值

### Requirement: 只能操作一般使用者
停權、解除停權與調整等級 SHALL 只適用於角色為 `user` 的帳號。對管理員帳號（包含管理員自己）的操作 SHALL 被拒絕並得到 403 與代碼 `admin_target_forbidden`；對狀態為 `deletion_pending` 的帳號 SHALL 得到 409。目標不存在 SHALL 得到 404。拒絕時 SHALL NOT 改變任何資料。

#### Scenario: 對管理員停權
- **WHEN** 管理員對另一位管理員或自己送出停權
- **THEN** 回應 403 `admin_target_forbidden`，狀態與稽核都沒有改變

#### Scenario: 對待刪除帳號
- **WHEN** 管理員對狀態為 `deletion_pending` 的使用者調整等級
- **THEN** 回應 409，沒有改變

### Requirement: 停權
`POST /api/admin/users/<public_id>/suspend` SHALL 要求原因（清理後 1 至 200 字）與管理員的密碼；目標狀態為 `active` 時 SHALL 改為 `suspended` 並使其 `token_version` 加 1，使既有的登入狀態立即失效；目標已是 `suspended` 時 SHALL 得到 409。停權後該使用者 SHALL 無法登入（回應「帳號已停用」）。SHALL 寫入 `user.suspend` 稽核（含原因與原狀態）。

#### Scenario: 停權成功
- **WHEN** 管理員以正確的密碼與原因停權一位使用中的使用者
- **THEN** 該使用者狀態為 `suspended`、既有登入狀態失效、無法再登入，並有一筆 `user.suspend` 稽核

#### Scenario: 缺少原因
- **WHEN** 停權請求沒有原因或原因超過 200 字
- **THEN** 回應 400 並指出是原因欄位，使用者狀態不變

#### Scenario: 重複停權
- **WHEN** 對已停權的使用者再次停權
- **THEN** 回應 409，沒有新的稽核

### Requirement: 解除停權
`POST /api/admin/users/<public_id>/unsuspend` SHALL 要求管理員的密碼；只有狀態為 `suspended` 的使用者可被解除，結果一律為 `active`。其他狀態 SHALL 得到 409。SHALL 寫入 `user.unsuspend` 稽核。

#### Scenario: 解除後可登入
- **WHEN** 管理員以正確的密碼解除一位停權的使用者
- **THEN** 狀態為 `active`，該使用者可以再次以 Google 登入

#### Scenario: 信箱未驗證
- **WHEN** 被解除的使用者信箱驗證時間為空（遷移前遺留的帳號）
- **THEN** 狀態仍為 `active`，不會變成其他狀態

#### Scenario: 非停權狀態
- **WHEN** 對狀態為 `active` 的使用者解除停權
- **THEN** 回應 409，沒有新的稽核

### Requirement: 調整會員等級
`POST /api/admin/users/<public_id>/tier` SHALL 接受 `tier`（`free`、`plus`、`vip`）與管理員的密碼，改變使用者的等級並寫入 `user.change_tier` 稽核（含調整前後的等級）。等級不在範圍內 SHALL 得到 400；與目前相同 SHALL 得到 409。調整 SHALL 立即反映在該使用者的額度上。

#### Scenario: 升級
- **WHEN** 管理員把 free 使用者調整為 plus
- **THEN** 使用者的每日輪數上限立即為 100，稽核記錄 free 到 plus

#### Scenario: 不合法的等級
- **WHEN** 請求的 `tier` 是 `gold`
- **THEN** 回應 400，等級與稽核都沒有改變

### Requirement: 危險操作須重新輸入密碼
停權、解除停權、調整等級與刪除對話 SHALL 在請求中帶管理員自己的密碼並由伺服器驗證；缺少或不正確時 SHALL 得到 400 並指出是密碼欄位，且不執行任何操作。不正確的密碼 SHALL 與登入失敗共用失敗計數與鎖定（連續 5 次鎖定 15 分鐘），並寫入 `reauth.failed` 稽核（含嘗試的動作），即使回應是錯誤。沒有設定密碼的管理員 SHALL 得到 400 與代碼 `no_password`。

#### Scenario: 密碼不正確
- **WHEN** 管理員以錯誤的密碼停權使用者
- **THEN** 回應 400 指出密碼欄位，使用者不變，並有一筆 `reauth.failed` 稽核

#### Scenario: 連續失敗
- **WHEN** 管理員連續 5 次送出錯誤的密碼
- **THEN** 帳號被鎖定 15 分鐘，之後即使密碼正確也被拒絕直到鎖定到期

#### Scenario: 沒有設定密碼
- **WHEN** 只以 Google 登入而沒有密碼的管理員送出停權
- **THEN** 回應 400 `no_password`，不執行
