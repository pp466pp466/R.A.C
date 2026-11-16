## ADDED Requirements

### Requirement: 每個回合的免費重新生成次數
RPG 組 SHALL 增加限制鍵 `rpg.free_regenerations`（每個回合前幾次重新生成免費，整數，0 至 20，初始值 2，由遷移寫入）。它 SHALL 與其他限制相同：每次使用時現讀資料庫、不使用行程快取、缺值或不合法時使用保底預設、只能由部署者以指令修改並寫入稽核。

#### Scenario: 合法的值
- **WHEN** 設定 `rpg.free_regenerations` 為 3
- **THEN** 寫入成功，下一次重新生成立即以 3 次為準

#### Scenario: 範圍外
- **WHEN** 設定為 21 或 -1
- **THEN** 被拒絕，原值不變

#### Scenario: 設為 0
- **WHEN** 設定為 0
- **THEN** 每次重新生成都收費
