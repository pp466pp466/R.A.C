## ADDED Requirements

### Requirement: 最後一輪以覆寫重抓
每次回合或修訂串流結束後，以及載入頁面時，前端 SHALL 從最後一個完成回合的前一個序號起增量重抓，並以後端的內容覆寫本機同序號的快取（包含 `revision`、行動與劇情），使其他裝置的修訂與過期的 IndexedDB 內容被修正。回合在快取中的比對 SHALL 納入 `revision`。後端的 `since_seq` 協定 SHALL 不變。

#### Scenario: 其他裝置修訂後
- **WHEN** 本機快取的最後一輪是 `revision` 1，後端已是 `revision` 2
- **THEN** 重抓後快取與畫面是 `revision` 2 的內容

#### Scenario: 沒有變化
- **WHEN** 後端與快取一致
- **THEN** 畫面沒有重繪，快取不變

### Requirement: 修訂的在途紀錄
修訂開始前，前端 SHALL 存一筆在途紀錄，含種類（`regenerate` 或 `edit`）、`client_msg_id` 與 `expected_revision`，編輯另含新行動，重新生成 SHALL NOT 含分類與理由。每個存檔仍最多一筆在途紀錄，且修訂進行中不得開始新回合。重新整理後 SHALL 以重抓的結果判定：快取的回合 `revision` 大於 `expected_revision` 視為成功，否則視為沒有完成；兩者都 SHALL 清除紀錄，且 SHALL NOT 自動重送或提供「重送」。

#### Scenario: 理由不進暫存
- **WHEN** 重新生成進行中檢查在途紀錄
- **THEN** 紀錄有種類與 `expected_revision`，沒有分類與理由

#### Scenario: 判定為成功
- **WHEN** 重新整理後重抓到的回合 `revision` 大於紀錄的 `expected_revision`
- **THEN** 紀錄被清除，沒有任何提示或重送

#### Scenario: 判定為沒有完成
- **WHEN** 重新整理後重抓到的回合 `revision` 等於 `expected_revision`
- **THEN** 紀錄被清除並通知玩家上次沒有完成，沒有自動重送
