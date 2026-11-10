# Spec Delta

## ADDED Requirements

### Requirement: 記事簿抓取不影響串流的事件序列與結束
回合完成後的記事簿抓取（見 `rpg-notebook-capture`）SHALL NOT 改變串流的事件種類與順序，`done` 仍是最後一個事件；串流 SHALL 在 `done` 送出之後隨即結束，SHALL NOT 等待抓取完成，也 SHALL NOT 為抓取新增事件或心跳。抓取在回應關閉之後才執行。

#### Scenario: 事件序列不變
- **WHEN** 第 11 輪順利完成並觸發記事簿抓取
- **THEN** 事件依序仍為 `meta`、若干 `token`、`state`、`done`，`done` 是最後一個事件，沒有任何記事簿相關事件

#### Scenario: 抓取很慢
- **WHEN** 抓取的模型呼叫需要十秒以上
- **THEN** 客戶端在 `done` 之後很快看到連線結束，不需要中止連線
