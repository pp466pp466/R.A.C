## ADDED Requirements

### Requirement: done 事件帶修訂資訊
回合串流與重送的 `done` 事件（狀態為 `complete` 或 `ended`）SHALL 另帶該回合目前的 `revision` 與剩餘免費重新生成次數 `free_left`。這些欄位只是新增，既有欄位與事件順序不變；前端遇到缺少這些欄位的 `done` SHALL 仍能正常運作。

#### Scenario: 新回合的 done
- **WHEN** 一個新回合順利完成
- **THEN** `done` 的 `revision` 為 1，`free_left` 為設定的免費次數

#### Scenario: 重送
- **WHEN** 重送已完成的回合
- **THEN** `done` 帶該回合目前的 `revision` 與 `free_left`

### Requirement: 修訂串流使用相同的事件協定
修訂端點（見 `rpg-turn-revision`）的串流 SHALL 使用本規格的事件種類、格式、標頭、心跳、生成時間上限與錯誤處理；前端可沿用同一條接收路徑。修訂的 `error` 事件 SHALL 只使用既有代碼（認領被新的嘗試取代時為 `turn_superseded`），一律不帶原因細節。

#### Scenario: 沿用接收路徑
- **WHEN** 前端以既有的串流接收程式處理修訂的串流
- **THEN** `meta`、`token`、`ping`、`transition`、`state`、`done`、`error` 都被正確處理
