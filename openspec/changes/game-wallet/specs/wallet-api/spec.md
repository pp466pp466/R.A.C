# Spec Delta

## Purpose
定義玩家端的錢包 Web API：查詢餘額與簽到狀態、兩步驟的每日簽到、遊戲幣記錄；以及這些端點的登入、CSRF、快取與錯誤行為。玩家無法透過任何 HTTP 端點花費幣或增加餘額（簽到除外）。

## ADDED Requirements

### Requirement: 查詢錢包
`GET /api/wallet` SHALL 回傳目前玩家的免費餘額、付費餘額、合計、免費幣上限、每輪幣價（現在只有 `standard`），以及簽到狀態：今日是否可簽到、簽到數、若現在領取會入帳多少與會作廢多少、下次可簽到的時間（台北時間下一個 0 點，ISO 8601）。回應 SHALL NOT 包含內部編號。查詢 SHALL 先處理該使用者的逾時預扣與既有玩家的贈幣補發。

#### Scenario: 新玩家的錢包
- **WHEN** 剛完成年齡確認的玩家查詢錢包
- **THEN** 免費餘額 200、付費餘額 0、合計 200，今日可簽到，簽到預覽為入帳 200、作廢 0

#### Scenario: 今日已簽到
- **WHEN** 玩家今日已簽到後查詢
- **THEN** 回報今日不可簽到，並給出下次可簽到的時間

#### Scenario: 接近上限的預覽
- **WHEN** 免費餘額 900 的玩家今日尚未簽到並查詢
- **THEN** 預覽為入帳 100、作廢 100

#### Scenario: 回應不含內部編號
- **WHEN** 檢查回應內容
- **THEN** 沒有使用者或帳本的內部整數編號

### Requirement: 每日簽到端點
`POST /api/wallet/checkin` SHALL 執行每日簽到。入帳量等於簽到數時 SHALL 直接成功並回傳新的錢包狀態。簽到會使一部分幣作廢時，請求本文 SHALL 帶 `confirm_overflow: true` 才執行；沒有帶時 SHALL 回 409 與代碼 `checkin_overflow`，並附上預覽（會入帳與會作廢的數量），不寫入任何東西。今日已簽到 SHALL 回 409 與代碼 `already_checked_in`。`confirm_overflow` 不是布林值 SHALL 回 400。

#### Scenario: 直接成功
- **WHEN** 免費餘額 100 的玩家簽到
- **THEN** 回應 200，免費餘額 300

#### Scenario: 需要確認
- **WHEN** 免費餘額 900 的玩家簽到而沒有 `confirm_overflow`
- **THEN** 回應 409 `checkin_overflow`，附預覽「入帳 100、作廢 100」，餘額不變，今日仍可簽到

#### Scenario: 確認後領取
- **WHEN** 同一位玩家以 `confirm_overflow: true` 簽到
- **THEN** 回應 200，免費餘額 1000

#### Scenario: 今日已簽到
- **WHEN** 玩家今日已簽到又送出簽到
- **THEN** 回應 409 `already_checked_in`

#### Scenario: 不合法的確認欄位
- **WHEN** `confirm_overflow` 是字串 `"yes"`
- **THEN** 回應 400，餘額不變

### Requirement: 遊戲幣記錄
`GET /api/wallet/entries` SHALL 依時間由新到舊回傳目前玩家的記錄，以游標分頁（每頁預設 20、至多 100）。每筆 SHALL 含：種類、**繁體中文的說明文字**（例如「註冊贈送」「每日簽到」「冒險回合」「管理員調整」）、免費與付費各自的增減量、時間；尚未結算的預扣 SHALL 顯示為「處理中」。**已退回的預扣及其退回列 SHALL NOT 出現在記錄**（淨額為零、玩家沒有感知差異；個人資料匯出仍包含全部）。回應 SHALL NOT 含內部編號、冪等鍵或其他玩家的資料。

#### Scenario: 一般記錄
- **WHEN** 玩家註冊後簽到一次並完成一輪冒險
- **THEN** 記錄由新到舊依序為「冒險回合 -30」「每日簽到 +200」「註冊贈送 +200」

#### Scenario: 退回的預扣不顯示
- **WHEN** 玩家有一筆失敗而被退回的預扣
- **THEN** 記錄中沒有這筆預扣與它的退回

#### Scenario: 處理中的預扣
- **WHEN** 玩家有一筆尚未結算的預扣
- **THEN** 記錄顯示「處理中」與扣款量

#### Scenario: 分頁
- **WHEN** 玩家有 45 筆記錄並以預設每頁取得
- **THEN** 第一頁 20 筆並附下一頁的游標，第三頁 5 筆且沒有下一頁游標

#### Scenario: 不含他人資料
- **WHEN** 玩家取得記錄
- **THEN** 所有筆數都屬於自己

### Requirement: 端點需要登入且遵守既有的存取規則
上述端點 SHALL 只對已登入的玩家開放；未登入 SHALL 得到 401。受限登入狀態（帳號鎖定、等待刪除、尚未完成年齡確認）SHALL 依既有的預設拒絕規則被擋下（403 與對應代碼）。`POST` SHALL 通過 CSRF 檢查。所有回應 SHALL 禁止快取。錯誤訊息 SHALL 為繁體中文。

#### Scenario: 未登入
- **WHEN** 沒有登入狀態呼叫 `GET /api/wallet`
- **THEN** 回應 401

#### Scenario: 尚未完成年齡確認
- **WHEN** 尚未完成年齡確認的玩家呼叫 `GET /api/wallet`
- **THEN** 回應 403 與代碼 `age_confirmation_required`，沒有發放任何幣

#### Scenario: 被鎖定
- **WHEN** 被鎖定的玩家呼叫 `POST /api/wallet/checkin`
- **THEN** 回應 403 與代碼 `account_locked`

#### Scenario: 缺少 CSRF
- **WHEN** `POST /api/wallet/checkin` 沒有 CSRF token
- **THEN** 被拒絕，餘額不變

#### Scenario: 不可快取
- **WHEN** 檢查這些端點的回應標頭
- **THEN** 都禁止快取

### Requirement: 玩家無法經由 HTTP 花費或增加餘額
除了每日簽到，系統 SHALL NOT 提供任何玩家端 HTTP 端點可以增加、扣除、預扣、結算或退回餘額。預扣、結算與退回只能由伺服器端的回合流程呼叫。

#### Scenario: 路由盤點
- **WHEN** 檢查所有已註冊的玩家端路由
- **THEN** 只有 `POST /api/wallet/checkin` 會改變餘額
