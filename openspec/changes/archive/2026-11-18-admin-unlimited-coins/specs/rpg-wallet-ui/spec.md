# Spec Delta

## MODIFIED Requirements

### Requirement: 頂列顯示餘額並隨回合更新
遊戲頁頂列 SHALL 顯示遊戲幣合計；載入時呼叫 `GET /api/wallet`，每次 `done` 後 SHALL 以 `done.coins` 更新而不另外請求。餘額 SHALL 以純文字渲染，載入失敗時 SHALL 顯示「—」而不阻擋閱讀。送出前若餘額已知且低於每輪幣價，SHALL 在輸入區說明本輪需要的幣數並提供簽到入口，但 SHALL NOT 阻止玩家嘗試送出（餘額以伺服器為準）。**錢包回應的 `coin_exempt` 為 `true`（管理員）時，SHALL NOT 顯示上述餘額不足的提示。**

#### Scenario: 載入時顯示
- **WHEN** 玩家開啟遊戲頁
- **THEN** 頂列顯示目前的合計餘額

#### Scenario: 一輪結束
- **WHEN** 一輪以 `done.coins=170` 結束
- **THEN** 餘額顯示 170，沒有額外的錢包請求

#### Scenario: 錢包讀取失敗
- **WHEN** `GET /api/wallet` 失敗
- **THEN** 餘額顯示「—」，時間軸與輸入仍可使用

#### Scenario: 餘額為 0 的管理員
- **WHEN** `coin_exempt` 為 `true` 且餘額為 0 的管理員開啟遊戲頁
- **THEN** 頂列顯示餘額 0，輸入區沒有餘額不足的提示，送出行動不受影響
