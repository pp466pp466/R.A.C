# Spec Delta

## Purpose

定義全站共用的遊戲幣餘額標籤與其錢包狀態庫：標籤在哪些頁面出現、顯示什麼、載入中與失敗時如何呈現、管理員如何標示、手機寬度如何縮減，以及點擊後前往錢包頁。餘額的規則與 API 屬 `game-wallet` 與 `wallet-api`；錢包頁屬 `wallet-hub-page`。

## ADDED Requirements

### Requirement: 標籤顯示合計餘額並區分載入中與失敗
餘額標籤 SHALL 顯示遊戲幣合計（免費加付費）。尚未讀到時（載入中）與讀取失敗時 SHALL 顯示「—」，SHALL NOT 阻擋頁面其他內容。餘額 SHALL 以純文字渲染，SHALL NOT 使用 `v-html`。狀態庫 SHALL 只接受非負整數的餘額與布林的 `coin_exempt`，型別不對的欄位 SHALL 被忽略；讀取 SHALL NOT 拋出錯誤到頁面。

#### Scenario: 載入完成
- **WHEN** 頁面載入後 `GET /api/wallet` 回 `total` 為 170
- **THEN** 標籤顯示 170

#### Scenario: 載入中
- **WHEN** 錢包請求尚未回應
- **THEN** 標籤顯示「—」

#### Scenario: 讀取失敗
- **WHEN** `GET /api/wallet` 回 5xx、403 或網路錯誤
- **THEN** 標籤顯示「—」，頁面其他功能照常可用，主控台與日誌沒有輸出餘額或錯誤內容

#### Scenario: 型別不對的欄位
- **WHEN** 回應的 `total` 是字串或負數
- **THEN** 該欄位被忽略，標籤維持「—」或原本的值

### Requirement: 管理員顯示真實餘額加不消耗標示
錢包回應的 `coin_exempt` 為 `true` 時，標籤 SHALL 顯示真實的合計餘額，並附小標示「不消耗」；SHALL NOT 以「不限」取代數字。`coin_exempt` 缺值或不是布林時 SHALL 視為 `false`。畫面 SHALL 只讀這個欄位，不自行依角色推論。

#### Scenario: 餘額為 0 的管理員
- **WHEN** `coin_exempt` 為 `true`、`total` 為 0
- **THEN** 標籤顯示 0 與「不消耗」

#### Scenario: 一般玩家
- **WHEN** `coin_exempt` 為 `false` 或缺值
- **THEN** 標籤只顯示餘額，沒有「不消耗」標示

### Requirement: 標籤可點擊並前往錢包頁
標籤 SHALL 是指向 `/wallet` 的一般連結（`<a>`），具有無障礙名稱（例如「遊戲幣餘額 170，前往錢包」），SHALL NOT 使用 `data-bs-*` 互動屬性或 Bootstrap 的 JavaScript。載入失敗顯示「—」時 SHALL 仍可點擊。

#### Scenario: 點擊
- **WHEN** 玩家點擊標籤
- **THEN** 瀏覽器前往 `/wallet`

#### Scenario: 失敗時仍可前往
- **WHEN** 標籤顯示「—」
- **THEN** 仍是指向 `/wallet` 的連結

### Requirement: 同一頁只請求一次並共用狀態
同一個頁面內，標籤與頁面自己的錢包邏輯 SHALL 共用同一份錢包狀態；頁面載入時 SHALL 只呼叫一次 `GET /api/wallet`（並行的載入請求 SHALL 合併）。頁面取得新的錢包資料（回合結束的 `done.coins`、簽到成功回的錢包）時，SHALL 寫入共用狀態使標籤即時更新，SHALL NOT 為此另外發出請求。

#### Scenario: 一次請求
- **WHEN** 遊戲進行頁載入，標籤與頁面的錢包邏輯都需要餘額
- **THEN** 只有一個 `GET /api/wallet` 請求

#### Scenario: 回合結束更新
- **WHEN** 一輪以 `done.coins=140` 結束
- **THEN** 標籤顯示 140，沒有額外的錢包請求

#### Scenario: 簽到後更新
- **WHEN** 玩家簽到成功，回應帶新的錢包
- **THEN** 標籤顯示新的合計，沒有額外的錢包請求

### Requirement: 標籤出現的頁面
標籤 SHALL 出現在下列已登入且可正常使用的頁面頂列：`/game`、`/game/start/<版本>`、`/game/<存檔>`、`/cs`、`/scripts`、`/account`（放在頁內導覽列的右側）。標籤 SHALL NOT 出現在 `/login`、`/age`、`/appeal`、`/terms`、`/privacy`、`/admin` 與首頁；`/account` 在「等待刪除」等受限狀態下的簡化畫面 SHALL NOT 顯示標籤。`/wallet` 本身有完整的餘額區塊，SHALL NOT 另外放標籤。

#### Scenario: 六個頁面都有
- **WHEN** 渲染六個頁面的 HTML
- **THEN** 每一頁都含 `<ui-wallet-balance>`

#### Scenario: 不該出現的頁面
- **WHEN** 渲染 `/login`、`/age`、`/appeal`、`/terms`、`/privacy` 與首頁
- **THEN** 都不含 `<ui-wallet-balance>`

#### Scenario: 等待刪除的帳號頁
- **WHEN** 帳號處於等待刪除狀態開啟 `/account`
- **THEN** 不顯示標籤，也不發出錢包請求

### Requirement: 手機寬度縮減
視窗寬度未滿 576px 時，標籤 SHALL 只顯示「幣」與數字（隱藏「遊戲幣」字樣與「不消耗」以外的說明文字），SHALL NOT 造成頂列水平捲動；完整的無障礙名稱 SHALL 保留。

#### Scenario: 360px 寬度
- **WHEN** 視窗寬度為 360px 並顯示標籤
- **THEN** 標籤顯示為「幣 N」，頁面沒有水平捲軸

### Requirement: 狀態庫不保存也不記錄餘額
錢包狀態庫 SHALL 只保存在頁面記憶體中，SHALL NOT 寫入 `localStorage`、`sessionStorage`、IndexedDB 或 cookie，SHALL NOT 把餘額、帳本內容或錯誤訊息輸出到主控台。換頁後 SHALL 重新讀取，不沿用上一頁的數字。

#### Scenario: 換頁重新讀取
- **WHEN** 玩家從 `/game` 前往 `/cs`
- **THEN** `/cs` 載入時重新呼叫 `GET /api/wallet`，不顯示上一頁快取的餘額

#### Scenario: 沒有瀏覽器儲存
- **WHEN** 檢查狀態庫與標籤元件的原始碼與執行結果
- **THEN** 沒有任何瀏覽器儲存的讀寫與主控台輸出
