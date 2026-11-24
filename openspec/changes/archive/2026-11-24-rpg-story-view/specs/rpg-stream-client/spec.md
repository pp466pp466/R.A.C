# Spec Delta

## MODIFIED Requirements

### Requirement: 被擋回合與轉場
收到 `transition` 事件 SHALL 丟棄當前輪尚未完成的劇情文字，劇情卡改為只顯示一行轉場文字（`text`）。`will_retry` 為真時 SHALL 繼續等待重新生成的新內容，並重設劇情卡的分段與打字機；`will_retry` 為假而後續 `done` 的狀態為 `blocked` 時 SHALL 保留轉場文字、解鎖輸入並提示玩家換個行動。畫面 SHALL NOT 以錯誤樣式顯示轉場，且 SHALL NOT 顯示任何被擋的文字或被擋類別。

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
`token` 的片段 SHALL 併入當前輪的劇情文字，由分段規則（見 `rpg-story-pages`）切成段，並以共用的打字機緩衝區逐字渲染「當前顯示的那一段」；翻到另一段時 SHALL 重設緩衝區。`done` 之後 SHALL 把剩餘文字併入最後的段，並讓正在顯示的段在短時間內補完；重送（`meta` 標示 `replayed`）的單一完整 `token` SHALL 以同樣方式處理。前端 SHALL NOT 顯示或保存任何結構化 JSON 原文；`state` 事件只取白名單欄位（世界狀態摘要與通知）。分頁在背景時回來 SHALL NOT 一次噴出大量文字。

#### Scenario: 逐字渲染
- **WHEN** 收到一串 `token`
- **THEN** 當前段以打字機節奏逐字顯示，其餘已到達的內容成為可翻的後續段，`done` 之後正在顯示的段在數秒內補完

#### Scenario: 重送
- **WHEN** 以已完成的 `client_msg_id` 重送，收到 `meta(replayed)`、單一 `token` 與 `done`
- **THEN** 顯示相同的完整劇情（同樣分段），餘額不變

#### Scenario: 背景分頁回來
- **WHEN** 分頁在背景期間串流持續到達，玩家切回分頁
- **THEN** 當前段不會一次噴出大量文字
