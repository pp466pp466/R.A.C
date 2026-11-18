# Spec Delta

## MODIFIED Requirements

### Requirement: 回合開始時以含嘗試序號的冪等鍵預扣遊戲幣
新回合與重送的回合 SHALL 以冪等鍵 `turn:{存檔識別碼}:{client_msg_id}:{attempt}` 預扣一輪的幣價；`attempt` 從 1 起，每次因失敗或中斷而重新開始同一個 `client_msg_id` 時加 1。已 `complete` 的回合重送 SHALL 直接回傳既有結果，不碰錢包。餘額不足 SHALL 回應 409 與代碼 `insufficient_coins`（帶餘額與所需數量），且 SHALL NOT 留下新的回合列。**管理員帳號（角色為 admin 且啟用中）的預扣金額為 0，SHALL NOT 因餘額不足被拒絕**，其餘流程（鍵、`attempt`、結算、退回）不變。

#### Scenario: 新回合
- **WHEN** 餘額足夠的玩家送出新行動
- **THEN** 錢包預扣一輪的幣價，回合列的 `attempt` 為 1

#### Scenario: 中斷後重送
- **WHEN** 一個已中斷（預扣已退回）的 `client_msg_id` 被重送
- **THEN** `attempt` 加 1、使用新的冪等鍵重新預扣，沒有出現免費生成

#### Scenario: 已完成的回合重送
- **WHEN** 重送已 `complete` 的 `client_msg_id`
- **THEN** 回傳既有結果，錢包沒有任何異動

#### Scenario: 餘額不足
- **WHEN** 合計餘額低於一輪的幣價
- **THEN** 回應 409 與代碼 `insufficient_coins`，資料庫中沒有這個回合列

#### Scenario: 餘額為 0 的管理員送出行動
- **WHEN** 餘額為 0 的管理員送出新行動
- **THEN** 回合正常開始並完成，回合列的 `attempt` 為 1，帳本有一筆金額為 0 的預扣，結算後餘額仍為 0，`done.coins` 為 0

#### Scenario: 管理員中斷後重送
- **WHEN** 管理員的回合中斷後以同一個 `client_msg_id` 重送
- **THEN** `attempt` 加 1、使用新的冪等鍵預扣 0 幣，流程與一般玩家相同
