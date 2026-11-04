# Spec Delta

## MODIFIED Requirements

### Requirement: 限制的鍵與合法範圍是固定的
限制 SHALL 只有這些鍵：`script_limit.free`、`script_limit.plus`、`script_limit.vip`（每位作者可擁有的劇本數，0 至 500）、`script_submit_daily.free`、`script_submit_daily.plus`、`script_submit_daily.vip`（每個台灣日的送審次數，0 至 100）、`protagonist_suggest_daily.free`、`protagonist_suggest_daily.plus`、`protagonist_suggest_daily.vip`（每個台灣日的主角生成與重新生成次數，0 至 100）、`script_reject_streak`（連續被自動拒絕幾次就暫停發佈，1 至 20）。值 SHALL 是整數。其他鍵與範圍外的值 SHALL 被拒絕寫入。

#### Scenario: 合法的鍵與值
- **WHEN** 設定 `script_limit.plus` 為 12
- **THEN** 寫入成功

#### Scenario: 範圍外
- **WHEN** 設定 `script_submit_daily.free` 為 1000
- **THEN** 被拒絕，原值不變

#### Scenario: 未知的鍵
- **WHEN** 設定 `script_limit.gold`
- **THEN** 被拒絕

#### Scenario: 生成次數的鍵
- **WHEN** 設定 `protagonist_suggest_daily.vip` 為 50
- **THEN** 寫入成功；設定為 101 則被拒絕

### Requirement: 初始值由遷移寫入
遷移 SHALL 寫入初始值：劇本數上限 free 3、plus 10、vip 30；每日送審次數 free 3、plus 5、vip 10；每日主角生成次數 free 5、plus 15、vip 40；`script_reject_streak` 為 3。遷移 SHALL 以 ORM 寫入，不使用原生 SQL；重複執行 SHALL 不產生重複的鍵。

#### Scenario: 初始值
- **WHEN** 剛完成遷移
- **THEN** 讀到的值就是上述初始值

#### Scenario: 鍵唯一
- **WHEN** 遷移升級、降級再升級
- **THEN** 每個鍵恰有一列

#### Scenario: 已有舊限制的資料庫
- **WHEN** 資料庫已有先前遷移寫入的七個限制，再升級到本次遷移
- **THEN** 原有七個限制的值不變，並新增三個生成次數的鍵

### Requirement: 只有部署者以指令修改並留下稽核
`flask set-script-limit <鍵> <值>` SHALL 驗證後寫入並顯示舊值與新值；`flask show-script-limits` SHALL 列出全部鍵的目前值。每次修改 SHALL 寫入 `limit.set` 稽核，管理員欄位為空，細節只含鍵與新舊數值。系統 SHALL NOT 提供任何 HTTP 端點修改限制。

#### Scenario: 修改並稽核
- **WHEN** 部署者執行 `flask set-script-limit script_limit.free 5`
- **THEN** 值更新，有一筆 `limit.set` 稽核含鍵、舊值與新值

#### Scenario: 預覽輸出
- **WHEN** 部署者執行 `flask show-script-limits`
- **THEN** 輸出全部十個鍵與其目前值

#### Scenario: 沒有網路端點
- **WHEN** 檢查所有已註冊的路由
- **THEN** 沒有任何路由能寫入限制
