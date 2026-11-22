# Spec Delta

## ADDED Requirements

### Requirement: 每人每日的劇本檢舉上限
營運限制 SHALL 新增 `script_report_daily`（劇本與主角組）：每位玩家每個台灣日可送出的劇本檢舉筆數，整數，範圍 0 至 100，初始值 5（0 表示暫停接受檢舉）。遷移 SHALL 以 ORM 寫入初始值，重複執行不產生重複的鍵、不蓋掉已有的值。程式 SHALL 每次使用時從資料庫現讀，缺值或不合法時使用保底預設 5 並記錄只含鍵名的警告事件；只有部署者能以 `flask set-script-limit` 修改，並寫入 `limit.set` 稽核。

#### Scenario: 合法值
- **WHEN** 設定 `script_report_daily` 為 10
- **THEN** 寫入成功並留下 `limit.set` 稽核；下一個檢舉請求依 10 判斷

#### Scenario: 範圍外
- **WHEN** 設定為 101 或 -1
- **THEN** 被拒絕，原值不變

#### Scenario: 初始值
- **WHEN** 剛完成遷移
- **THEN** 讀到 5

#### Scenario: 已有限制的資料庫
- **WHEN** 資料庫已有先前遷移寫入的限制（部署者可能調整過），再升級到本次遷移
- **THEN** 原有限制的值不變，只新增這一個鍵

#### Scenario: 缺值
- **WHEN** 資料庫沒有這個鍵
- **THEN** 使用保底預設 5，不放行無上限
