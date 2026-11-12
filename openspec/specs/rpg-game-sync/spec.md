# rpg-game-sync Specification

## Purpose

定義 RPG 遊戲頁的本機暫存與同步：IndexedDB 作為離線閱讀快取與單筆在途回合、以 `since_seq` 增量補齊、衝突時以後端為準、暫存不可用時退回記憶體，以及不同帳號之間的隔離。RPG 每一輪都要連線生成並消耗遊戲幣，所以沒有自動排隊重送。

## Requirements

### Requirement: 後端為權威，本機只是閱讀快取
遊戲頁 SHALL 把已取得的回合（序號、`client_msg_id`、輪數、狀態、行動、劇情）、第 0 幕與世界狀態摘要暫存於 IndexedDB，供離線時閱讀。連線正常時 SHALL 以 `since_seq` 向後端增量讀取，並以後端的內容覆寫本機同序號的快取；本機 SHALL NOT 產生新的 AI 回應或自行指派序號。離線時玩家可閱讀快取的歷史，送出 SHALL 被禁用並說明需要連線。被擋與中斷的回合 SHALL NOT 在快取中保存任何部分內容。

#### Scenario: 離線閱讀
- **WHEN** 玩家曾載入某存檔，之後斷線重新開啟該存檔
- **THEN** 時間軸顯示快取的歷史，送出被禁用並說明需要連線

#### Scenario: 增量補齊
- **WHEN** 本機最大序號為 40，後端已有序號 41 與 42
- **THEN** 請求帶 `since_seq=40`，兩個回合被補上，順序以 `seq` 為準

#### Scenario: 後端覆寫快取
- **WHEN** 後端同序號的回合內容與本機不同
- **THEN** 顯示並保存後端的內容

#### Scenario: 被擋回合不保存內容
- **WHEN** 快取一個被擋的回合
- **THEN** 只保存序號與狀態，沒有任何文字

### Requirement: 單筆在途回合與冪等重送
送出回合前，前端 SHALL 先把 `client_msg_id` 與行動文字存為「在途回合」（每個存檔最多一筆）。收到 `done`、或確認後端已有該 `client_msg_id` 的完整回合時 SHALL 移除；重新整理、斷線或前端中止後再開啟頁面，SHALL 先以 `since_seq` 補齊，若在途回合仍未完成，提示玩家並提供「以同一編號重送」與「放棄」。重送 SHALL 使用原本的 `client_msg_id`，已完成者由後端重播、不重複扣幣。前端 SHALL NOT 自動重送。

#### Scenario: 重新整理後補齊
- **WHEN** 串流中途玩家重新整理，而後端已完成該回合
- **THEN** 頁面載入後以 `since_seq` 補到該回合，在途回合被移除，沒有提示重送

#### Scenario: 後端沒有完成
- **WHEN** 重新整理後補齊仍找不到該 `client_msg_id` 的完整回合
- **THEN** 顯示在途回合的行動文字，提供「重送」與「放棄」，不自動送出

#### Scenario: 重送不重複扣幣
- **WHEN** 玩家按重送，且後端該回合其實已完成
- **THEN** 收到 `replayed` 的重播與 `done`，餘額不再減少

#### Scenario: 只有一筆
- **WHEN** 存檔已有一筆在途回合
- **THEN** 送出新行動被禁用，直到在途回合完成或被放棄

### Requirement: 暫存不可用時退回記憶體
IndexedDB 無法開啟、被禁止或配額用盡時，遊戲頁 SHALL 退回記憶體暫存並照常運作；此時重新整理後不保證保存在途回合，頁面 SHALL 不因此出錯。所有 IndexedDB 與 `localStorage` 的讀寫 SHALL 包在 try/catch 內。

#### Scenario: 隱私模式
- **WHEN** IndexedDB 開啟失敗
- **THEN** 遊戲頁仍可載入與遊玩，歷史來自後端

#### Scenario: 寫入失敗
- **WHEN** 寫入快取時拋出配額錯誤
- **THEN** 不影響畫面，也不顯示錯誤

### Requirement: 帳號隔離與清理
資料庫名稱 SHALL 含使用者的對外識別碼，同一瀏覽器中不同帳號的快取互相看不到。快取 SHALL 只含遊戲頁需要的欄位，SHALL NOT 含記事簿全文、email、生日或年齡區間；存檔不存在（404）時 SHALL 清除該存檔的快取與在途回合。快取與在途回合 SHALL NOT 寫入主控台或日誌。

#### Scenario: 兩個帳號
- **WHEN** 同一瀏覽器先後以兩個帳號開啟不同的存檔
- **THEN** 彼此讀不到對方的快取

#### Scenario: 存檔已被刪除
- **WHEN** 載入時快照回 404
- **THEN** 該存檔的快取被清除，顯示找不到存檔

#### Scenario: 不含記事簿
- **WHEN** 檢查 IndexedDB 的全部內容
- **THEN** 沒有記事簿全文、email、生日與年齡區間
