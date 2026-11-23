# Spec Delta

## MODIFIED Requirements

### Requirement: 劇情時間軸與歷史補齊
頁面 SHALL 以 `since_seq` 增量讀取回合，並依 `seq` 遞增排列；`has_more` 為真時 SHALL 繼續讀取。`complete` 的回合含玩家行動與劇情；被擋或中斷的回合 SHALL 只有一行轉場或中斷說明，SHALL NOT 含任何部分內容。主畫面 SHALL 只以分段劇情卡顯示當前輪（見 `rpg-story-pages`），完整的時間軸 SHALL 只在「歷史」中閱讀。劇情與行動 SHALL 以純文字渲染（保留換行），SHALL NOT 使用 `v-html`。

#### Scenario: 載入歷史
- **WHEN** 存檔有 25 個完整回合
- **THEN** 歷史依序顯示第 0 幕與 25 輪，序號遞增；主畫面顯示第 25 輪

#### Scenario: 被擋回合
- **WHEN** 歷史含一個被擋的回合
- **THEN** 該處只有一行轉場說明，沒有被擋的文字

#### Scenario: 劇情含標記字元
- **WHEN** 劇情或玩家行動含 `<b>` 或 `<script>` 字樣
- **THEN** 畫面與歷史顯示原文字元，沒有被解析為 HTML

#### Scenario: 閱讀較早內容
- **WHEN** 玩家用 `‹` 回看當前輪較早的一段，這時新的片段到達；或玩家想看更早的回合
- **THEN** 畫面維持在該段、不自動翻頁；更早的回合只在歷史中閱讀，沒有「回到最新」的捲動按鈕
