# Spec Delta

## Purpose

定義遊戲畫面內的遊戲幣呈現：頂列餘額、每輪結束的更新、餘額不足時的處理，以及最小的每日簽到入口（預覽、超過免費幣上限的確認、已簽到）。幣的規則、扣款與簽到的後端行為屬 `game-wallet` 與 `wallet-api`。

## ADDED Requirements

### Requirement: 頂列顯示餘額並隨回合更新
遊戲頁頂列 SHALL 顯示遊戲幣合計；載入時呼叫 `GET /api/wallet`，每次 `done` 後 SHALL 以 `done.coins` 更新而不另外請求。餘額 SHALL 以純文字渲染，載入失敗時 SHALL 顯示「—」而不阻擋閱讀。送出前若餘額已知且低於每輪幣價，SHALL 在輸入區說明本輪需要的幣數並提供簽到入口，但 SHALL NOT 阻止玩家嘗試送出（餘額以伺服器為準）。

#### Scenario: 載入時顯示
- **WHEN** 玩家開啟遊戲頁
- **THEN** 頂列顯示目前的合計餘額

#### Scenario: 一輪結束
- **WHEN** 一輪以 `done.coins=170` 結束
- **THEN** 餘額顯示 170，沒有額外的錢包請求

#### Scenario: 錢包讀取失敗
- **WHEN** `GET /api/wallet` 失敗
- **THEN** 餘額顯示「—」，時間軸與輸入仍可使用

### Requirement: 餘額不足時有路可走
串流回 409 `insufficient_coins` 時，遊戲頁 SHALL 開啟自製 Modal，說明目前餘額與每輪需要的幣數、沒有扣款，並提供「每日簽到」與「關閉」。玩家的行動文字 SHALL 保留。簽到成功後 SHALL 更新餘額並關閉或改為提示可再次送出；SHALL NOT 自動重送。

#### Scenario: 餘額不足
- **WHEN** 餘額為 10 的玩家送出行動
- **THEN** 開啟 Modal 說明餘額與所需幣數、沒有扣款，行動文字仍在輸入框

#### Scenario: 簽到後可再送
- **WHEN** 玩家在 Modal 簽到成功
- **THEN** 餘額更新、顯示可再次送出的提示，行動沒有被自動送出

### Requirement: 最小簽到入口與超過上限的確認
簽到 SHALL 呼叫 `POST /api/wallet/checkin`。伺服器回 409 `checkin_overflow` 時 SHALL 顯示預覽（會入帳多少、會作廢多少），由玩家選「取消」（不消耗當日簽到）或「領取」（以 `confirm_overflow: true` 再次呼叫）。回 409 `already_checked_in` SHALL 顯示今日已簽到與下次可簽到的時間（台灣時間）。簽到進行中按鈕 SHALL 禁用，`finally` 恢復。簽到入口 SHALL NOT 提供儲值、訂閱或記錄頁（屬後續的 change）。

#### Scenario: 一般簽到
- **WHEN** 玩家按簽到，餘額未超過上限
- **THEN** 餘額增加並顯示 Toast

#### Scenario: 超過上限
- **WHEN** 伺服器回 `checkin_overflow` 與預覽
- **THEN** 顯示會作廢的幣數，選「取消」不送第二次請求，選「領取」才帶 `confirm_overflow`

#### Scenario: 今日已簽到
- **WHEN** 伺服器回 `already_checked_in`
- **THEN** 顯示今日已簽到與下次可簽到時間，餘額不變

#### Scenario: 重複點擊
- **WHEN** 簽到請求進行中玩家再次按下
- **THEN** 只送出一個請求
