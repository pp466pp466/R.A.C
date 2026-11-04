# Spec Delta

## Purpose

定義存放在資料庫的營運限制（劇本數量上限、每日送審次數上限、連續被拒門檻）：固定的鍵與合法範圍、每次使用時現讀而不快取、缺值或不合法時的保底預設，以及只能由部署者以指令修改並留下稽核。讓調整數值不必改程式或重新部署。

## ADDED Requirements

### Requirement: 限制的鍵與合法範圍是固定的
限制 SHALL 只有這些鍵：`script_limit.free`、`script_limit.plus`、`script_limit.vip`（每位作者可擁有的劇本數，0 至 500）、`script_submit_daily.free`、`script_submit_daily.plus`、`script_submit_daily.vip`（每個台灣日的送審次數，0 至 100）、`script_reject_streak`（連續被自動拒絕幾次就暫停發佈，1 至 20）。值 SHALL 是整數。其他鍵與範圍外的值 SHALL 被拒絕寫入。

#### Scenario: 合法的鍵與值
- **WHEN** 設定 `script_limit.plus` 為 12
- **THEN** 寫入成功

#### Scenario: 範圍外
- **WHEN** 設定 `script_submit_daily.free` 為 1000
- **THEN** 被拒絕，原值不變

#### Scenario: 未知的鍵
- **WHEN** 設定 `script_limit.gold`
- **THEN** 被拒絕

### Requirement: 初始值由遷移寫入
遷移 SHALL 寫入初始值：劇本數上限 free 3、plus 10、vip 30；每日送審次數 free 3、plus 5、vip 10；`script_reject_streak` 為 3。遷移 SHALL 以 ORM 寫入，不使用原生 SQL；重複執行 SHALL 不產生重複的鍵。

#### Scenario: 初始值
- **WHEN** 剛完成遷移
- **THEN** 讀到的值就是上述初始值

#### Scenario: 鍵唯一
- **WHEN** 遷移升級、降級再升級
- **THEN** 每個鍵恰有一列

### Requirement: 每次使用時現讀，不使用行程快取
程式判斷限制時 SHALL 從資料庫讀取當時的值，SHALL NOT 以行程內的變數、模組常數或快取保存。資料庫中的值被修改後，下一個請求 SHALL 就使用新值，不需重啟，且多個 worker SHALL 得到相同的值。

#### Scenario: 修改立即生效
- **WHEN** 修改 `script_limit.free` 後，免費作者送出下一個建立請求
- **THEN** 該請求依新的上限判斷

#### Scenario: 兩個 worker
- **WHEN** 兩個 worker 在修改後各處理一個請求
- **THEN** 兩者讀到同一個值

### Requirement: 缺值或不合法時使用保底預設
資料庫沒有某個鍵、值不是整數或在範圍外時，程式 SHALL 使用內建的保底預設（與初始值相同），並記錄一筆只含鍵名的警告事件。SHALL NOT 因此放行無上限，也 SHALL NOT 拋出錯誤讓創作與送審全部失敗。

#### Scenario: 缺少某個鍵
- **WHEN** 資料庫沒有 `script_limit.vip` 這一列
- **THEN** 使用保底預設 30，並記錄警告事件

#### Scenario: 值損壞
- **WHEN** 某個值被改成範圍外的數字
- **THEN** 使用保底預設，事件只含鍵名

### Requirement: 只有部署者以指令修改並留下稽核
`flask set-script-limit <鍵> <值>` SHALL 驗證後寫入並顯示舊值與新值；`flask show-script-limits` SHALL 列出全部鍵的目前值。每次修改 SHALL 寫入 `limit.set` 稽核，管理員欄位為空，細節只含鍵與新舊數值。系統 SHALL NOT 提供任何 HTTP 端點修改限制。

#### Scenario: 修改並稽核
- **WHEN** 部署者執行 `flask set-script-limit script_limit.free 5`
- **THEN** 值更新，有一筆 `limit.set` 稽核含鍵、舊值與新值

#### Scenario: 預覽輸出
- **WHEN** 部署者執行 `flask show-script-limits`
- **THEN** 輸出全部七個鍵與其目前值

#### Scenario: 沒有網路端點
- **WHEN** 檢查所有已註冊的路由
- **THEN** 沒有任何路由能寫入限制
