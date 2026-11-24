## ADDED Requirements

### Requirement: 刪除帳號
`POST /api/admin/users/<public_id>/delete` SHALL 要求原因（清理後 1 至 200 字）與管理員的密碼，成功時 SHALL 立即且不可復原地抹除該帳號與其全部資料，並回應 `{"ok": true}`。抹除 SHALL 走全站唯一的抹除出口（見 `account-deletion`），不套用「匿名保留我已發佈的劇本」偏好，被刪帳號的劇本 SHALL 一律全刪。成功時 SHALL 寫入 `user.delete` 稽核（含原因與原狀態），且稽核與抹除 SHALL 在同一個交易；缺少或不合格的原因 SHALL 得到 400 並指出原因欄位，且 SHALL NOT 改變任何資料。刪除之後該帳號的既有登入狀態 SHALL 立即無效，且同一個信箱 SHALL 能再以 Google 登入建立新帳號。

#### Scenario: 刪除成功
- **WHEN** 管理員以正確的密碼與原因刪除一位使用中的玩家
- **THEN** 該帳號與其對話、存檔、遊戲幣、劇本、第三方身分都不存在，有一筆原因為 `admin_deleted` 的刪除紀錄，有一筆 `user.delete` 稽核，回應 `{"ok": true}`

#### Scenario: 缺少原因
- **WHEN** 刪除請求沒有原因或原因超過 200 字
- **THEN** 回應 400 並指出是原因欄位，帳號不變、沒有稽核

#### Scenario: 非管理員
- **WHEN** 一般玩家或未登入者呼叫刪除端點
- **THEN** 得到與其他後台端點相同的 404，沒有任何改變

#### Scenario: 刪除後的登入狀態
- **WHEN** 一位玩家被刪除，之後他原本的瀏覽器仍帶著舊的登入 cookie 發出請求
- **THEN** 請求被視為未登入

#### Scenario: 同一信箱重新註冊
- **WHEN** 被刪除的玩家之後再以同一個 Google 帳號登入並勾選條款與年滿 18 歲
- **THEN** 建立一個全新的帳號，不帶有舊帳號的任何資料

### Requirement: 刪除帳號只適用於一般玩家且一次一個
刪除帳號的目標 SHALL 只能是角色為 `user` 的帳號：對管理員帳號（包含管理員自己）SHALL 得到 403 與代碼 `admin_target_forbidden`，目標不存在 SHALL 得到 404；狀態為 `active`、`suspended`、被鎖定或 `deletion_pending` 的帳號 SHALL 都可刪除（`deletion_pending` 不必等寬限期）。每次請求 SHALL 只刪除一個帳號；系統 SHALL NOT 提供列出使用者的端點或批次刪除。拒絕時 SHALL NOT 改變任何資料，也 SHALL NOT 寫入 `user.delete` 稽核。

#### Scenario: 刪除待刪除中的帳號
- **WHEN** 管理員刪除一位狀態為 `deletion_pending`、寬限期尚未屆滿的玩家
- **THEN** 該帳號立即被抹除，不必等到寬限期結束

#### Scenario: 對管理員刪除
- **WHEN** 管理員對另一位管理員或自己送出刪除
- **THEN** 回應 403 `admin_target_forbidden`，帳號與稽核都沒有改變

#### Scenario: 目標不存在
- **WHEN** 管理員對不存在的識別碼送出刪除
- **THEN** 回應 404，沒有任何改變

#### Scenario: 沒有清單與批次
- **WHEN** 檢視後台的所有使用者端點
- **THEN** 沒有列出全部使用者的端點，也沒有一次刪除多個帳號的端點


### Requirement: 後台使用者面板的刪除帳號操作
後台使用者面板 SHALL 在目標角色為 `user` 時顯示「刪除帳號」按鈕（含狀態為 `suspended`、被鎖定與 `deletion_pending` 的帳號），目標為管理員時 SHALL NOT 顯示。按下後 SHALL 開啟確認視窗，視窗 SHALL 明確說明刪除不可復原與連帶刪除的資料（登入身分、對話、存檔與記事簿、遊戲幣、全部劇本含已上架者、檢舉與申訴），並要求填寫原因與輸入管理員密碼；密碼 SHALL 在送出的當下清空，不論結果。刪除成功後 SHALL 關閉視窗、顯示提示、清除使用者面板，並使該帳號在鎖定、暫停發佈與申訴清單中的列消失，其餘清單（被標記的對話、意見回饋、總覽、稽核）SHALL 在下次進入時重新載入。

#### Scenario: 管理員帳號沒有按鈕
- **WHEN** 管理員查詢到另一位管理員
- **THEN** 面板不顯示刪除按鈕，並顯示管理員帳號無法在後台操作的說明

#### Scenario: 確認視窗的警告
- **WHEN** 管理員按下「刪除帳號」
- **THEN** 確認視窗寫明不可復原與連帶刪除的資料，並有原因與密碼欄位

#### Scenario: 刪除成功後的畫面
- **WHEN** 刪除成功
- **THEN** 視窗關閉、出現「已刪除帳號。」提示、使用者面板清空，該帳號不再出現在鎖定清單

#### Scenario: 密碼錯誤
- **WHEN** 管理員在確認視窗輸入錯誤的密碼
- **THEN** 視窗保持開啟並在密碼欄位顯示錯誤，帳號不變

## MODIFIED Requirements

### Requirement: 危險操作須重新輸入密碼
停權、解除停權、調整等級、調整遊戲幣餘額、刪除對話與刪除帳號 SHALL 在請求中帶管理員自己的密碼並由伺服器驗證；缺少或不正確時 SHALL 得到 400 並指出是密碼欄位，且不執行任何操作。不正確的密碼 SHALL 與登入失敗共用失敗計數與鎖定（連續 5 次鎖定 15 分鐘），並寫入 `reauth.failed` 稽核（含嘗試的動作），即使回應是錯誤。沒有設定密碼的管理員 SHALL 得到 400 與代碼 `no_password`。

#### Scenario: 密碼不正確
- **WHEN** 管理員以錯誤的密碼停權使用者
- **THEN** 回應 400 指出密碼欄位，使用者不變，並有一筆 `reauth.failed` 稽核

#### Scenario: 連續失敗
- **WHEN** 管理員連續 5 次送出錯誤的密碼
- **THEN** 帳號被鎖定 15 分鐘，之後即使密碼正確也被拒絕直到鎖定到期

#### Scenario: 沒有設定密碼
- **WHEN** 只以 Google 登入而沒有密碼的管理員送出停權
- **THEN** 回應 400 `no_password`，不執行

#### Scenario: 調整遊戲幣也要密碼
- **WHEN** 管理員以錯誤的密碼調整遊戲幣餘額
- **THEN** 回應 400 指出密碼欄位，餘額不變，並有一筆 `reauth.failed` 稽核含嘗試的動作

#### Scenario: 刪除帳號也要密碼
- **WHEN** 管理員以錯誤的密碼刪除一位玩家
- **THEN** 回應 400 指出密碼欄位，帳號不變，並有一筆 `reauth.failed` 稽核含嘗試的動作 `user.delete`
