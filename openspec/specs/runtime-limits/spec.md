# runtime-limits Specification

## Purpose
定義存放在資料庫的營運限制（劇本數量上限、每日送審次數上限、連續被拒門檻）：固定的鍵與合法範圍、每次使用時現讀而不快取、缺值或不合法時的保底預設，以及只能由部署者以指令修改並留下稽核。讓調整數值不必改程式或重新部署。

## Requirements

### Requirement: 限制的鍵與合法範圍是固定的
限制 SHALL 只有這些鍵，分成三組。**劇本與主角組**：`script_limit.free`、`script_limit.plus`、`script_limit.vip`（每位作者可擁有的劇本數，0 至 500）、`script_submit_daily.free`、`script_submit_daily.plus`、`script_submit_daily.vip`（每個台灣日的送審次數，0 至 100）、`protagonist_suggest_daily.free`、`protagonist_suggest_daily.plus`、`protagonist_suggest_daily.vip`（每個台灣日的主角生成與重新生成次數，0 至 100）、`script_reject_streak`（連續被自動拒絕幾次就暫停發佈，1 至 20）。**遊戲幣組**：`coin.turn_cost.standard`（標準模型一輪的幣價，1 至 10000）、`coin.signup_grant`（首次註冊贈幣，0 至 100000）、`coin.checkin_grant`（每日簽到，0 至 100000）、`coin.free_cap`（免費幣上限，0 至 1000000）、`coin.hold_timeout_seconds`（預扣逾時秒數，60 至 1800）。**RPG 組**：`rpg.max_months_per_turn`（每回合遊戲內經過月數的增量上限，0 至 240）。值 SHALL 是整數。其他鍵與範圍外的值 SHALL 被拒絕寫入。

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

#### Scenario: 遊戲幣的鍵
- **WHEN** 設定 `coin.checkin_grant` 為 100
- **THEN** 寫入成功；設定 `coin.free_cap` 為 1000001 則被拒絕

#### Scenario: 幣價的下限
- **WHEN** 設定 `coin.turn_cost.standard` 為 0
- **THEN** 被拒絕，原值不變

#### Scenario: RPG 的鍵
- **WHEN** 設定 `rpg.max_months_per_turn` 為 36
- **THEN** 寫入成功；設定為 241 則被拒絕

### Requirement: 初始值由遷移寫入
遷移 SHALL 寫入初始值：劇本數上限 free 3、plus 10、vip 30；每日送審次數 free 3、plus 5、vip 10；每日主角生成次數 free 5、plus 15、vip 40；`script_reject_streak` 為 3；`coin.turn_cost.standard` 為 30、`coin.signup_grant` 為 200、`coin.checkin_grant` 為 200、`coin.free_cap` 為 1000、`coin.hold_timeout_seconds` 為 180；`rpg.max_months_per_turn` 為 24。遷移 SHALL 以 ORM 寫入，不使用原生 SQL；重複執行 SHALL 不產生重複的鍵，且 SHALL NOT 蓋掉資料庫中已有的值。

#### Scenario: 初始值
- **WHEN** 剛完成遷移
- **THEN** 讀到的值就是上述初始值

#### Scenario: 鍵唯一
- **WHEN** 遷移升級、降級再升級
- **THEN** 每個鍵恰有一列

#### Scenario: 已有舊限制的資料庫
- **WHEN** 資料庫已有先前遷移寫入的十五個限制（部署者可能調整過其中某些值），再升級到本次遷移
- **THEN** 原有十五個限制的值不變，並新增 RPG 的一個鍵

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
劇本與主角組的限制 SHALL 由 `flask set-script-limit <鍵> <值>` 修改、由 `flask show-script-limits` 列出；遊戲幣組 SHALL 由 `flask set-wallet-setting <鍵> <值>` 修改、由 `flask show-wallet-settings` 列出；RPG 組 SHALL 由 `flask set-rpg-setting <鍵> <值>` 修改、由 `flask show-rpg-settings` 列出。修改指令 SHALL 驗證後寫入並顯示舊值與新值；顯示指令 SHALL 列出該組全部鍵的目前值與是否為保底預設。每個指令 SHALL 只接受自己那一組的鍵：鍵屬於另一組時 SHALL 被拒絕並指出該用哪個指令。每次修改 SHALL 寫入 `limit.set` 稽核，管理員欄位為空，細節只含鍵與新舊數值。系統 SHALL NOT 提供任何 HTTP 端點修改限制。

#### Scenario: 修改並稽核
- **WHEN** 部署者執行 `flask set-script-limit script_limit.free 5`
- **THEN** 值更新，有一筆 `limit.set` 稽核含鍵、舊值與新值

#### Scenario: 預覽輸出
- **WHEN** 部署者執行 `flask show-script-limits`
- **THEN** 輸出劇本與主角組的全部十個鍵與其目前值

#### Scenario: 修改遊戲幣設定
- **WHEN** 部署者執行 `flask set-wallet-setting coin.checkin_grant 300`
- **THEN** 值更新，有一筆 `limit.set` 稽核含鍵、舊值與新值

#### Scenario: 預覽遊戲幣設定
- **WHEN** 部署者執行 `flask show-wallet-settings`
- **THEN** 輸出五個遊戲幣鍵與其目前值

#### Scenario: 修改 RPG 設定
- **WHEN** 部署者執行 `flask set-rpg-setting rpg.max_months_per_turn 12`
- **THEN** 值更新，有一筆 `limit.set` 稽核含鍵、舊值與新值

#### Scenario: 預覽 RPG 設定
- **WHEN** 部署者執行 `flask show-rpg-settings`
- **THEN** 輸出 RPG 組的鍵與其目前值

#### Scenario: 用錯指令
- **WHEN** 部署者以 `flask set-script-limit coin.free_cap 500` 修改遊戲幣設定
- **THEN** 被拒絕，訊息指出應使用 `set-wallet-setting`，值不變

#### Scenario: RPG 鍵用錯指令
- **WHEN** 部署者以 `flask set-wallet-setting rpg.max_months_per_turn 12`
- **THEN** 被拒絕，訊息指出應使用 `set-rpg-setting`，值不變

#### Scenario: 沒有網路端點
- **WHEN** 檢查所有已註冊的路由
- **THEN** 沒有任何路由能寫入限制
