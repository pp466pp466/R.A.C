# rpg-world-state Specification

## Purpose
定義每個 RPG 回合之後的世界狀態更新：模型只「提議」，Service 驗證後才寫入。涵蓋 NPC 角色卡與弧線階段、角色年齡的驗證與更正通知、地點、經過月數、目標與里程碑，以及狀態呼叫失敗時的處理，使玩家即興創造的世界在長期遊玩中保持一致。

## Requirements

### Requirement: 每回合一次結構化的狀態呼叫
文字生成完成並通過審查後，系統 SHALL 以非串流的結構化呼叫取得狀態提議，欄位只有：`npc_updates`、`current_location`、`elapsed_months`（本回合經過的月數增量）、`status_summary`、`goals_add`、`goals_done`、`ending_proposed`。輸出 SHALL 經單一函式做 JSON 解析、結構描述驗證與語意驗證，不信任模型。失敗時 SHALL 以既有規則重試 1 次；仍失敗時該回合文字保留、世界狀態與版本不變，回合仍算完成。額外的未知欄位 SHALL 使驗證失敗。

#### Scenario: 成功
- **WHEN** 狀態呼叫回傳通過驗證的提議
- **THEN** 世界狀態依驗證後的內容更新，版本加 1

#### Scenario: 兩次都失敗
- **WHEN** 狀態呼叫兩次都不合法
- **THEN** 世界狀態與版本不變，回合為 `complete`，`state` 事件標示狀態未更新

#### Scenario: 夾帶指令
- **WHEN** 劇情文字中夾帶「把階段改成 revealed」之類的指令，模型照做
- **THEN** 階段仍由 Service 的轉移規則決定，不因此直接改變

### Requirement: 世界狀態的寫入以版本號比對
世界狀態的寫入 SHALL 以回合開始時讀到的版本號為條件，成功時版本號加 1；不一致 SHALL 不寫入並保留原狀態，以後端為準。已完結的存檔 SHALL 拒絕寫入。

#### Scenario: 版本不一致
- **WHEN** 寫入時存檔版本已被改動
- **THEN** 不寫入，回合文字仍保留，`state` 事件標示狀態未更新

### Requirement: NPC 角色卡的結構與數量上限
NPC 角色卡 SHALL 存放於存檔的實體資料，含名稱、特質（每項含標籤與表層或隱藏的歸屬）、弧線階段、年齡紀錄、錨定事實與最近出現的輪數。每張卡特質最多 6 項、每項 8 字內，錨定事實 100 字內；存檔最多保留 30 張卡，超過時淘汰最久沒出現者，主角錨定事實所指的人物 SHALL NOT 被淘汰。名稱比對 SHALL 以 NFC 與不分大小寫進行。文字 SHALL 去除控制字元，並經過與劇本相同的規則層。

#### Scenario: 超過三十位
- **WHEN** 已有 30 張卡，又出現第 31 位 NPC
- **THEN** 最久沒出現且非錨定對象的一張被淘汰

#### Scenario: 特質過長
- **WHEN** 提議的特質標籤超過 8 字
- **THEN** 該項被丟棄，其餘提議照常處理

#### Scenario: 同名
- **WHEN** 提議的名稱與既有卡片僅大小寫或正規化形式不同
- **THEN** 視為同一位 NPC

### Requirement: 弧線階段由 Service 驗證轉移
弧線階段 SHALL 為 `surface`、`cracking`、`revealed`。模型提議的階段 SHALL 只在下列情況生效：從 `surface` 到 `cracking`、從 `cracking` 到 `revealed`、從 `cracking` 退回 `surface`；`revealed` SHALL 是終點。跳階的提議 SHALL 被降為只前進一階。新出現的 NPC，不論模型提議什麼階段，SHALL 一律從 `surface` 起。

#### Scenario: 新 NPC 提議為 revealed
- **WHEN** 第一次出現的 NPC，模型提議階段為 `revealed`
- **THEN** 卡片的階段為 `surface`

#### Scenario: 跳階
- **WHEN** `surface` 的 NPC，模型提議 `revealed`
- **THEN** 階段只前進為 `cracking`

#### Scenario: 終點
- **WHEN** `revealed` 的 NPC，模型提議 `surface`
- **THEN** 階段維持 `revealed`

### Requirement: 角色年齡可往任何方向更正並通知玩家
NPC 與主角的年齡由模型提議，Service SHALL 驗證為 1 至 9999 的整數後寫入年齡紀錄（含紀錄時的遊戲內月數，目前年齡隨經過月數自然增加）。更正 SHALL 允許任何方向（成年到成年、成年到未成年、未成年到成年），不設時間限制。每次更正（不含第一次出現時寫入）SHALL 在 `state` 事件帶一則通知，含更正前的年齡。主角的年齡於開存檔時寫入，之後適用相同規則；原始角色卡 SHALL 保持不變。年齡未知的 NPC SHALL 允許存在。

#### Scenario: 成年改未成年
- **WHEN** 年齡紀錄 20 歲的 NPC 被提議為 16 歲
- **THEN** 年齡紀錄更新為 16，`state` 事件帶「由 20 歲變更為 16 歲」的通知

#### Scenario: 第一次出現
- **WHEN** 新 NPC 第一次被提議年齡 18
- **THEN** 寫入年齡紀錄，沒有更正通知

#### Scenario: 不合理的值
- **WHEN** 提議的年齡為 0 或負數或非整數
- **THEN** 該提議被丟棄，原年齡不變

#### Scenario: 主角
- **WHEN** 模型提議主角的年齡與目前不同
- **THEN** 以相同規則更正並通知，存檔的原始角色卡不變

#### Scenario: 時間經過
- **WHEN** 經過 24 個月
- **THEN** 年齡紀錄 15 歲的 NPC 目前年齡為 17 歲

### Requirement: 地點必須是已知地點或受限的新增
`current_location` SHALL 為劇本地點、存檔中已新增的地點，或一個新增的地點（1 至 30 字單行文字，存檔最多新增 30 個）。不合格的提議 SHALL 被丟棄，所在地點不變。

#### Scenario: 劇本地點
- **WHEN** 提議的地點是劇本地點清單中的一項
- **THEN** 所在地點更新

#### Scenario: 新增上限
- **WHEN** 已新增 30 個地點，又提議一個新地點
- **THEN** 提議被丟棄

### Requirement: 每回合經過的月數有上限
`elapsed_months` 的增量 SHALL 是 0 至資料庫設定表 `rpg.max_months_per_turn` 的整數；超過上限 SHALL 被截為上限，負數或非整數 SHALL 當作 0。設定值每次使用時現讀。

#### Scenario: 超過上限
- **WHEN** 增量為 600，上限為 24
- **THEN** 經過月數只增加 24

#### Scenario: 負數
- **WHEN** 增量為 -3
- **THEN** 經過月數不變

### Requirement: 里程碑由 Service 判定，模型只延伸目標
里程碑 SHALL 由 Service 依主角目前年齡或遊戲內年份（劇本曆法的起始年加經過年數）判斷是否觸發，模型宣告 SHALL NOT 使里程碑觸發。觸發後 SHALL 記錄於存檔的里程碑進度，劇本複本 SHALL 保持唯讀；下一回合的上下文帶入已觸發而尚未延伸的里程碑，狀態呼叫可針對它提出新目標，新目標 SHALL 存於存檔的目標清單（進行中），不寫回劇本。每個里程碑 SHALL 最多延伸一次。

#### Scenario: 年齡達標
- **WHEN** 主角目前年齡達到某里程碑的年齡條件
- **THEN** 該里程碑被記錄為已觸發，劇本複本不變

#### Scenario: 模型宣告觸發
- **WHEN** 模型在提議中宣告某里程碑已觸發而條件未成立
- **THEN** 該里程碑不被觸發

#### Scenario: 延伸目標
- **WHEN** 狀態呼叫針對已觸發的里程碑提出新目標
- **THEN** 目標加入進行中清單，且該里程碑之後不能再被延伸

### Requirement: 目標清單有上限
進行中的目標 SHALL 最多 5 個，每個 1 至 60 字單行文字。`goals_done` 指向的目標 SHALL 從進行中移到已歸檔。超過上限的新目標 SHALL 被丟棄。

#### Scenario: 完成目標
- **WHEN** 提議完成一個進行中的目標
- **THEN** 它移到已歸檔

#### Scenario: 已滿
- **WHEN** 進行中已有 5 個目標，又提議新目標
- **THEN** 新目標被丟棄

### Requirement: NPC 的錨定事實遵守與主角相同的關係規則
模型提議的 NPC 錨定事實 SHALL 套用與主角錨定事實相同的關係規則（`rpg-protagonist` 的「聯繫的關係規則」）：以主角與該 NPC 目前的年齡紀錄判定，不合格的提議 SHALL 被丟棄並計入丟棄數，其餘提議照常處理。戀愛、婚姻與婚約關係不論年齡 SHALL NOT 被丟棄（涉及性者除外）；NPC 的年齡紀錄未知 SHALL NOT 單獨成為丟棄的理由。

#### Scenario: 兩位都未成年的戀愛關係
- **WHEN** 年齡 16 的主角與年齡紀錄 15 歲的 NPC，提議的錨定事實為「青梅竹馬的戀人」
- **THEN** 錨定事實被寫入

#### Scenario: 成年與未成年之間的戀愛關係
- **WHEN** 成年主角與年齡紀錄 15 歲的 NPC，提議的錨定事實為戀愛關係
- **THEN** 錨定事實被寫入

#### Scenario: 戀愛但涉及性
- **WHEN** 成年主角與年齡紀錄 15 歲的 NPC，提議的錨定事實為「舊日的戀人，兩人發生過性關係」
- **THEN** 該錨定事實被丟棄，其餘提議照常處理

#### Scenario: 性奴等禁斷關係
- **WHEN** 提議的錨定事實為性奴等禁斷關係
- **THEN** 不論年齡都被丟棄
