# Spec Delta

## Purpose

定義 `/wallet` 錢包頁：路由與存取規則、餘額與付費／贈送拆分、「免費」分頁（每日簽到與倒數、註冊贈幣說明）、「儲值」分頁的示意界線、帳本「紀錄」檢視與使用說明。餘額、簽到與記錄的伺服器規則屬 `game-wallet` 與 `wallet-api`；頂列的餘額標籤屬 `shared-wallet-balance`。

## ADDED Requirements

### Requirement: 錢包頁的路由與存取規則
`GET /wallet` SHALL 只渲染頁面外殼，SHALL NOT 在初始狀態內放餘額、帳本、email、生日或年齡；資料 SHALL 由前端呼叫既有的錢包 API 取得。路由 SHALL 要求已登入，受限登入狀態 SHALL 沿用既有的導向（被鎖定導向 `/appeal`、待確認年齡導向 `/age`、等待刪除導向 `/account`），未登入導向登入頁。回應 SHALL 禁止快取。

#### Scenario: 已登入玩家
- **WHEN** 已完成年齡確認的玩家開啟 `/wallet`
- **THEN** 回應 200 的頁面外殼，初始狀態沒有餘額與帳本

#### Scenario: 未登入
- **WHEN** 沒有登入狀態開啟 `/wallet`
- **THEN** 導向登入頁

#### Scenario: 受限狀態
- **WHEN** 被鎖定、待確認年齡或等待刪除的帳號開啟 `/wallet`
- **THEN** 依序導向 `/appeal`、`/age`、`/account`

#### Scenario: 不被快取
- **WHEN** 檢查 `/wallet` 的回應標頭
- **THEN** `Cache-Control` 為 `no-store`

### Requirement: 頂部顯示餘額與付費贈送拆分
頁面頂部 SHALL 顯示合計餘額，並分別顯示「付費」與「贈送」（對應 `paid_balance` 與 `free_balance`），以及「紀錄」入口。管理員（`coin_exempt` 為真）SHALL 另顯示「不消耗」說明並照實顯示餘額。讀取失敗時 SHALL 顯示「—」與「重試」，SHALL NOT 顯示不確定的數字。所有文字 SHALL 以純文字渲染。

#### Scenario: 一般玩家
- **WHEN** 玩家免費餘額 850、付費 0
- **THEN** 顯示合計 850、「付費 0｜贈送 850」

#### Scenario: 管理員
- **WHEN** `coin_exempt` 為 `true`
- **THEN** 顯示真實餘額與「不消耗」說明

#### Scenario: 讀取失敗
- **WHEN** 錢包請求失敗
- **THEN** 顯示「—」與「重試」，按重試重新請求

### Requirement: 分頁與紀錄檢視由 Vue 狀態接管
頁面 SHALL 有「免費」與「儲值」兩個分頁，預設顯示「免費」；分頁與「紀錄」檢視的切換 SHALL 完全由 Vue 的響應式狀態控制（`v-if` 或 `v-show`），SHALL NOT 使用 Bootstrap 的 JavaScript 或 `data-bs-*`。分頁 SHALL 具有 `tablist`、`tab`、`tabpanel` 角色與鍵盤可操作。進入「紀錄」時 SHALL 隱藏分頁列並提供「返回」。

#### Scenario: 預設分頁
- **WHEN** 開啟 `/wallet`
- **THEN** 顯示「免費」分頁

#### Scenario: 切換分頁
- **WHEN** 玩家選「儲值」
- **THEN** 顯示儲值分頁，不重新載入頁面，沒有新的 API 請求

#### Scenario: 進入與離開紀錄
- **WHEN** 玩家按「紀錄」再按「返回」
- **THEN** 先顯示紀錄檢視（分頁列隱藏），返回後回到先前的分頁

### Requirement: 免費分頁的每日簽到
簽到卡 SHALL 依錢包的 `checkin.claimable` 呈現：可領取時顯示簽到數與「領取」；會使免費幣超過上限時，點擊後 SHALL 顯示預覽（會入帳與作廢多少）並由玩家選「取消」（不消耗當日簽到、不送第二次請求）或「領取」（以 `confirm_overflow: true` 呼叫）；今日已簽到時 SHALL 改顯示倒數。簽到進行中按鈕 SHALL 禁用，`finally` 恢復；成功後 SHALL 更新共用餘額並顯示 Toast。簽到行為 SHALL 與遊戲進行頁一致（同一個共用簽到狀態機）。

#### Scenario: 一般簽到
- **WHEN** 玩家可領取且餘額未超過上限，按「領取」
- **THEN** 餘額增加、顯示 Toast、簽到卡改為已簽到與倒數

#### Scenario: 超過上限
- **WHEN** 伺服器回 409 `checkin_overflow` 與預覽
- **THEN** 顯示會作廢的幣數；選「取消」不送第二次請求，選「領取」才帶 `confirm_overflow`

#### Scenario: 今日已簽到
- **WHEN** 伺服器回 409 `already_checked_in`，或錢包說 `claimable` 為 `false`
- **THEN** 簽到卡顯示今日已簽到與倒數，餘額不變

#### Scenario: 重複點擊
- **WHEN** 簽到請求進行中再次按下
- **THEN** 只送出一個請求

### Requirement: 簽到倒數以伺服器時間為準且不空轉
今日已簽到時，簽到卡 SHALL 顯示距離 `checkin.next_reset_at` 的剩餘時間，格式為 `時:分:秒`，每秒更新；剩餘時間 SHALL 以目前時間與 `next_reset_at` 相減計算（背景分頁被節流後恢復時仍正確）。倒數歸零時 SHALL 重新讀取一次錢包；若重新讀取後仍不可簽到（例如本機時鐘不準），SHALL NOT 連續重複讀取（兩次讀取間至少相隔 60 秒）。離開頁面或進入其他檢視時 SHALL 清除計時器。

#### Scenario: 倒數顯示
- **WHEN** 距離下次簽到還有 16 小時 43 分 26 秒
- **THEN** 顯示 `16:43:26` 並逐秒遞減

#### Scenario: 歸零重讀
- **WHEN** 倒數歸零
- **THEN** 重新讀取錢包一次；若變為可簽到則顯示簽到按鈕

#### Scenario: 時鐘不準
- **WHEN** 本機時鐘比伺服器快，重新讀取後仍不可簽到
- **THEN** 60 秒內不再重複請求

### Requirement: 註冊贈幣卡只說明不提供領取
免費分頁 SHALL 有註冊贈幣卡，說明「完成年齡確認時自動發放一次」，SHALL NOT 提供領取按鈕，SHALL NOT 顯示贈幣的數量（錢包 API 沒有此欄位，畫面不自行寫死）。管理員 SHALL 改顯示「管理員帳號不發放註冊贈幣」。卡片或說明另 SHALL 顯示免費幣上限（取自 `free_cap`）。

#### Scenario: 一般玩家
- **WHEN** 玩家開啟免費分頁
- **THEN** 註冊贈幣卡說明自動發放，沒有按鈕與數量

#### Scenario: 管理員
- **WHEN** `coin_exempt` 為 `true`
- **THEN** 卡片說明管理員帳號不發放註冊贈幣

### Requirement: 儲值分頁只是示意
儲值分頁 SHALL 以幣包卡片呈現儲值的樣貌，並在分頁頂端與每張卡片上標明「示意，非正式價格」；每個購買按鈕 SHALL 停用並顯示「即將推出」。分頁 SHALL NOT 發出任何請求，SHALL NOT 提供任何增加餘額的路徑。示意的幣包資料 SHALL 集中在單一前端資料模組，SHALL NOT 寫入資料庫、設定表或 API 回應，也 SHALL NOT 散落在樣板內。

#### Scenario: 標示
- **WHEN** 玩家開啟儲值分頁
- **THEN** 頂端有「示意，非正式價格」的說明，每張卡片也有「示意」標示

#### Scenario: 按鈕停用
- **WHEN** 玩家嘗試點擊任一張卡片的購買按鈕
- **THEN** 按鈕已停用，沒有任何請求送出、餘額不變

#### Scenario: 資料只在一處
- **WHEN** 檢查專案的樣板、API 與設定
- **THEN** 示意幣包的數字只出現在那個前端資料模組

### Requirement: 紀錄檢視顯示帳本
「紀錄」檢視 SHALL 以 `GET /api/wallet/entries` 由新到舊列出帳本，每筆顯示繁體中文的說明（取自 API）、免費與付費各自的增減量（正數帶「+」）與時間（台灣時間）；尚未結算的預扣 SHALL 顯示「處理中」。有下一頁游標時 SHALL 顯示「載入更多」，並以 `before` 帶游標；沒有記錄時 SHALL 顯示空狀態說明；讀取失敗 SHALL 顯示錯誤說明與「重試」且保留已載入的清單。內容 SHALL 以純文字渲染。

#### Scenario: 一般記錄
- **WHEN** 玩家註冊後簽到一次並完成一輪
- **THEN** 清單由新到舊為「冒險回合 −30」「每日簽到 +200」「註冊贈送 +200」

#### Scenario: 載入更多
- **WHEN** 回應帶 `next_before`，玩家按「載入更多」
- **THEN** 以該游標請求下一頁並接在清單後面

#### Scenario: 沒有記錄
- **WHEN** 回應的記錄為空
- **THEN** 顯示空狀態說明

#### Scenario: 載入更多失敗
- **WHEN** 下一頁請求失敗
- **THEN** 已載入的清單保留，顯示錯誤說明與「重試」

#### Scenario: 管理員的零金額預扣
- **WHEN** 管理員完成一輪
- **THEN** 清單顯示該輪的記錄與 0 的增減量

### Requirement: 使用說明
頁面 SHALL 有使用說明區，只陳述已定案的規則：先扣贈送幣再扣付費幣、幣不過期、贈送幣有上限、每輪消耗的幣數（取自 `turn_cost.standard`，管理員顯示不消耗）。說明 SHALL NOT 承諾任何價格、儲值方式、退款或訂閱內容。

#### Scenario: 說明內容
- **WHEN** 玩家檢視使用說明
- **THEN** 只有已定案的規則，沒有價格、付款方式與退款承諾

### Requirement: 錢包頁遵守前端規範
錢包頁 SHALL 使用 Vue 3 全域變數模式，SHALL NOT 使用 `.vue` 檔案、外部 CDN 或外部字型；資源 SHALL 以 `url_for('static', ...)` 引入；Modal 與 Toast SHALL 使用共用的自製元件並由 Vue 狀態控制；一般 API 以離線的 axios 呼叫；SHALL NOT 使用 Bootstrap 的 JavaScript 或 `data-bs-*`，SHALL NOT 使用純白文字。頁面 SHALL 在 360px 寬度下沒有水平捲軸。寫入類的請求 SHALL 只有「簽到」一個。

#### Scenario: 架構守門
- **WHEN** 執行前端規則的架構測試
- **THEN** 錢包頁的樣板與腳本沒有違規

#### Scenario: 只有一個寫入
- **WHEN** 檢查錢包頁模組發出的請求
- **THEN** 非 GET 的請求恰好只有 `POST /api/wallet/checkin`

#### Scenario: 手機版面
- **WHEN** 視窗寬度為 360px
- **THEN** 沒有水平捲軸，卡片單欄排列
