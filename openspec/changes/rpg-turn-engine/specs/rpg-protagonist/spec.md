# Spec Delta

## ADDED Requirements

### Requirement: 主角有來歷
主角角色卡 SHALL 有「來歷」欄位，值為 `native`（本地人）或 `transmigrator`（穿越者），缺值時預設為 `native`；其他值 SHALL 以 `protagonist.origin` 的欄位錯誤拒絕。劇本角色 SHALL NOT 有來歷欄位。來歷是列舉，不是自由文字，SHALL NOT 經過文字審查，也 SHALL NOT 因主角年齡或內容模式而受限。遷移之前建立的存檔沒有來歷，SHALL 視為本地人。來歷 SHALL 隨角色卡保存、匯出，並在每個回合的世界狀態中帶給模型。

#### Scenario: 缺值
- **WHEN** 開存檔的角色卡沒有來歷
- **THEN** 驗證通過，存檔的來歷為本地人

#### Scenario: 穿越者
- **WHEN** 角色卡的來歷為 `transmigrator`
- **THEN** 驗證通過，存檔記錄穿越者

#### Scenario: 不合法的值
- **WHEN** 來歷為 `ghost`
- **THEN** 回應 400，欄位錯誤指出 `protagonist.origin`，沒有開出存檔

#### Scenario: 劇本角色
- **WHEN** 劇本的角色帶有來歷欄位
- **THEN** 劇本驗證拒絕該欄位

#### Scenario: 舊存檔
- **WHEN** 讀取遷移之前建立的存檔的主角
- **THEN** 來歷視為本地人

### Requirement: 主角年齡是劇中年齡，開存檔時寫入，之後依規則更正
主角的年齡 SHALL 是劇中角色的年齡，與使用者由生日算出的年齡無關。年齡 SHALL 於開存檔時由角色卡寫入；之後 SHALL 與 NPC 適用相同的更正規則（模型提議、Service 驗證、可往任何方向、每次更正通知玩家並附更正前的年齡，見 `rpg-world-state`）。更正 SHALL 只寫入世界狀態的年齡紀錄，存檔上的原始角色卡與開存檔時的角色年齡 SHALL NOT 被修改；玩家 SHALL NOT 能直接寫入年齡。

#### Scenario: 與使用者年齡無關
- **WHEN** 17 歲的使用者建立年齡為 30 的主角
- **THEN** 驗證通過，使用者的內容上限不因主角年齡而改變

#### Scenario: 更正不改動原始角色卡
- **WHEN** 世界狀態把主角的年齡由 30 更正為 28
- **THEN** 年齡紀錄為 28，存檔的角色卡與開存檔時的角色年齡仍是 30

#### Scenario: 玩家不能直接寫入
- **WHEN** 檢查玩家可呼叫的 API
- **THEN** 沒有任何端點能直接修改主角的年齡

## REMOVED Requirements

### Requirement: 主角年齡是劇中年齡，開存檔後不可更改
**Reason**: AGENTS.md 第 7 節已於 RA-21 修訂：年齡由模型提議、Service 驗證後可往任何方向更正，每次更正通知玩家並附更正前的年齡；只在確定未成年與性內容綁定時由輸出審查擋下。
**Migration**: 由本檔新增的「主角年齡是劇中年齡，開存檔時寫入，之後依規則更正」取代；原始角色卡與存檔上的 `character_age` 仍是開存檔時的年齡且不被修改。
