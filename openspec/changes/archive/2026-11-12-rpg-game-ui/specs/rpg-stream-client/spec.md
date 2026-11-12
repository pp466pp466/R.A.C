# Spec Delta

## Purpose

定義前端接收 RPG 串流（回合與第 0 幕）的協定：以原生 `fetch` 搭配 `ReadableStream` 解析 Server-Sent Events、事件處理、逾時與中止、被擋回合與轉場、開始前失敗的錯誤對照，以及與打字機的接線。後端的事件格式與端點屬 `rpg-turn-stream`。

## ADDED Requirements

### Requirement: 以 fetch 與 ReadableStream 接收事件
前端 SHALL 以原生 `fetch`（`POST`、JSON 本文、帶 `X-CSRF-Token` 與 `credentials: same-origin`）呼叫串流端點，從 `response.body` 讀取位元組，以 UTF-8 串流解碼後依空行切出事件，解析 `event:` 與單行 `data:` JSON。事件 SHALL 能跨越任意的位元組邊界（包含切在多位元組字元中間），不完整的尾端 SHALL 留待下一塊。解析不出的事件或未知的事件名稱 SHALL 被忽略而不中斷串流。

#### Scenario: 事件被切成多塊
- **WHEN** 一個 `token` 事件的位元組被切成三塊到達，且切點落在一個中文字元中間
- **THEN** 解析出完整的一個事件，文字沒有亂碼

#### Scenario: 一塊含多個事件
- **WHEN** 單一資料塊含 `meta`、兩個 `token` 與 `ping`
- **THEN** 事件依序被處理

#### Scenario: 未知事件
- **WHEN** 收到前端不認得的事件名稱
- **THEN** 被忽略，後續事件照常處理

### Requirement: 以 done 為完整接收的唯一依據
前端 SHALL 只在收到 `done` 事件時視為這一輪完整結束；連線在 `done` 之前結束（不論有無例外）SHALL 視為中斷：丟棄尚未顯示完的片段、顯示繁體中文的中斷說明，並提醒玩家餘額與歷史以伺服器為準。收到 `error` 事件 SHALL 顯示其繁體中文訊息，並依 `done` 或連線結束處理收尾。`done` 的 `coins` SHALL 更新餘額顯示，`status` 為 `ended` SHALL 把存檔標示為已完結。

#### Scenario: 正常結束
- **WHEN** 事件序列為 `meta`、若干 `token`、`state`、`done`
- **THEN** 這一輪視為完成，輸入解鎖，餘額為 `done.coins`

#### Scenario: 沒有 done 就斷線
- **WHEN** 連線在最後一個 `token` 之後結束，沒有 `done`
- **THEN** 視為中斷，顯示中斷說明並保留玩家的行動文字，不把片段當成完整回應

#### Scenario: 結局回合
- **WHEN** `done` 的結束狀態為 `ended`
- **THEN** 存檔顯示為已完結，輸入禁用

### Requirement: 閒置與總逾時，並以 AbortController 中止
串流 SHALL 設兩個計時器：任何收到的位元組（含 `ping`）重設 45 秒的閒置計時器；從送出起算的總逾時為 180 秒。任一逾時、玩家按取消、切換存檔或離開頁面（`pagehide`）時 SHALL 以 `AbortController` 中止請求。無論成功、失敗或中斷，`finally` SHALL 清除計時器、恢復輸入並解除載入狀態。前端中止不代表後端中止；因此中止後 SHALL 提示玩家這一輪可能已在伺服器完成，並以 `since_seq` 補齊。逾時數值 SHALL 來自頁面初始狀態，不寫死於串流模組。

#### Scenario: 閒置逾時
- **WHEN** 連續 45 秒沒有收到任何位元組
- **THEN** 請求被中止，顯示逾時說明，輸入恢復

#### Scenario: ping 維持連線
- **WHEN** 模型 40 秒沒有輸出但每 15 秒收到 `ping`
- **THEN** 不會因閒置而中止

#### Scenario: 總逾時
- **WHEN** 串流在 180 秒時仍未收到 `done`
- **THEN** 請求被中止並視為中斷

#### Scenario: 玩家取消
- **WHEN** 玩家在串流進行中按取消
- **THEN** 請求被中止、計時器清除，不顯示成錯誤，並以 `since_seq` 補齊

#### Scenario: 離開頁面
- **WHEN** 串流進行中觸發 `pagehide`
- **THEN** 請求被中止

### Requirement: 被擋回合與轉場
收到 `transition` 事件 SHALL 丟棄目前尚未完成的氣泡文字，改為一行轉場文字（`text`）。`will_retry` 為真時 SHALL 繼續等待重新生成的新氣泡，並重設氣泡的打字機；`will_retry` 為假而後續 `done` 的狀態為 `blocked` 時 SHALL 保留轉場文字、解鎖輸入並提示玩家換個行動。畫面 SHALL NOT 以錯誤樣式顯示轉場，且 SHALL NOT 顯示任何被擋的文字或被擋類別。

#### Scenario: 自動重生成功
- **WHEN** 事件序列為 `token`、`transition(will_retry=true)`、新的 `token`、`done(complete)`
- **THEN** 第一段文字被丟棄，畫面依序出現一行轉場文字與新的完整回應

#### Scenario: 重生仍被擋
- **WHEN** 第二次嘗試也以 `transition(will_retry=false)` 與 `done(blocked)` 結束
- **THEN** 顯示轉場文字，輸入解鎖並提示換個行動，該回合不留任何劇情

#### Scenario: 轉場不是錯誤
- **WHEN** 顯示轉場文字
- **THEN** 沒有錯誤色彩的提示框或 Toast

### Requirement: 打字機渲染與不顯示結構化原文
`token` 的片段 SHALL 推入共用的打字機緩衝區逐字渲染，`done` 之後 SHALL 呼叫完成並在短時間內補完；重送（`meta` 標示 `replayed`）的單一完整 `token` SHALL 以同樣方式渲染。前端 SHALL NOT 顯示或保存任何結構化 JSON 原文；`state` 事件只取白名單欄位（世界狀態摘要與通知）。分頁在背景時回來 SHALL NOT 一次噴出大量文字。

#### Scenario: 逐字渲染
- **WHEN** 收到一串 `token`
- **THEN** 文字以打字機節奏顯示，`done` 之後在數秒內補完

#### Scenario: 重送
- **WHEN** 以已完成的 `client_msg_id` 重送，收到 `meta(replayed)`、單一 `token` 與 `done`
- **THEN** 顯示相同的完整劇情，餘額不變

### Requirement: 開始前的失敗以對照表顯示
串流端點在開始前回傳的一般 JSON 錯誤 SHALL 以固定的繁體中文對照顯示：401 導向登入；403 依代碼（`account_locked`、`age_confirmation_required` 與其他）顯示帳號提示；409 `insufficient_coins` 開啟遊戲幣 Modal、`turn_in_progress` 說明另一個回合進行中、`session_ended` 標示已完結、其餘版本衝突依後端訊息並重讀快照；400 顯示逐欄位說明；429 與 503（含系統忙碌、故事引擎無法使用）顯示稍後再試。這些失敗 SHALL 發生在預扣之前，畫面 SHALL 說明沒有扣除遊戲幣，並保留玩家的行動文字。錯誤訊息 SHALL NOT 含玩家文字或內部細節。

#### Scenario: 餘額不足
- **WHEN** 伺服器回 409 `insufficient_coins`
- **THEN** 開啟遊戲幣 Modal，行動文字保留，沒有時間軸的新項目

#### Scenario: 另一個回合進行中
- **WHEN** 伺服器回 409 `turn_in_progress`
- **THEN** 顯示「另一個回合還在進行」的說明，並稍後以 `since_seq` 補齊

#### Scenario: 登入已失效
- **WHEN** 伺服器回 401
- **THEN** 導向登入頁並帶回到目前網址的返回位置
