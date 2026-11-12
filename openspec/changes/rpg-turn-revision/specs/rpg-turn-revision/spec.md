## Purpose
定義玩家重新生成與編輯「最後一輪」的後端行為：哪一輪可以動、端點與請求驗證、費用與免費次數、預扣與退回、世界狀態回滾與原子替換、並行與逾時接手、失敗處理，以及理由回饋的保存與隱私。畫面屬 `rpg-revision-ui`。

## ADDED Requirements

### Requirement: 兩個修訂串流端點
系統 SHALL 提供 `POST /api/game/sessions/<id>/turns/<client_msg_id>/regenerate/stream`（本文 `expected_revision`、`category`、`reason`）與 `POST /api/game/sessions/<id>/turns/<client_msg_id>/edit/stream`（本文 `expected_revision`、`action`），回應為 `text/event-stream`，標頭與心跳同回合串流。端點 SHALL 要求登入、通過 CSRF 與 Origin 檢查與帳號與年齡閘門；非擁有者、存檔不存在、該 `client_msg_id` 不存在 SHALL 得到相同的 404 JSON。開始前的失敗 SHALL 以一般 JSON 錯誤回應，不開啟事件串流。

#### Scenario: 非擁有者
- **WHEN** 使用者 B 對使用者 A 的最後一輪送出重新生成
- **THEN** 得到與不存在相同的 404 JSON，沒有預扣、沒有認領

#### Scenario: 沒有 CSRF 權杖
- **WHEN** 沒帶 CSRF 權杖送出
- **THEN** 被拒絕，沒有任何寫入

#### Scenario: 回合編號不存在
- **WHEN** 送出的 `client_msg_id` 不屬於該存檔
- **THEN** 得到 404 JSON

### Requirement: 只有最後一輪可以被修訂
「最後一輪」SHALL 是該存檔最後一個 `complete` 的回合，且其後沒有未逾時的生成中回合或修訂。只有進行中的存檔、且該回合有回合前快照者可被修訂。被擋、中斷與生成中的嘗試 SHALL NOT 算作最後一輪（它們沒有完成的內容）；結局那一輪（存檔已完結）、第 0 幕與更早的回合 SHALL NOT 可被修訂。不可修訂 SHALL 回 409 `not_revisable`，並以 `reason` 說明（`ended`、`not_last`、`in_progress`、`no_snapshot`）。

#### Scenario: 下一輪被擋後仍可修訂上一輪
- **WHEN** 最後一個完成的回合之後，只有一個被擋或中斷的嘗試
- **THEN** 那個完成的回合仍可重新生成或編輯

#### Scenario: 更早的回合
- **WHEN** 對不是最後一個完成的回合送出修訂
- **THEN** 回 409 `not_revisable`（`not_last`），沒有任何寫入

#### Scenario: 已完結的存檔
- **WHEN** 存檔因結局而完結，玩家對結局那一輪送出修訂
- **THEN** 回 409 `not_revisable`（`ended`）

#### Scenario: 遷移之前建立的回合
- **WHEN** 回合沒有回合前快照
- **THEN** 回 409 `not_revisable`（`no_snapshot`）

#### Scenario: 下一輪正在生成
- **WHEN** 最後一個完成的回合之後有未逾時的生成中回合
- **THEN** 回 409 `not_revisable`（`in_progress`）

### Requirement: 以 expected_revision 偵測並行並認領
回合 SHALL 有 `revision`（建立時為 1）。修訂請求 SHALL 帶 `expected_revision`，與後端不一致 SHALL 回 409 `revision_conflict`，以後端為準、不寫入。認領 SHALL 是單一條件式更新（回合為 `complete`、`revision` 相符、沒有未逾時的認領）；成功時嘗試序號加 1、記錄認領時間。同一回合已有未逾時的認領 SHALL 回 409 `turn_in_progress`。認領逾時（與遊戲幣預扣逾時相同，現讀自設定表）者 SHALL 可被新的請求接手，舊的執行者之後的替換 SHALL 失敗並以 `turn_superseded` 結束。

#### Scenario: 兩個裝置同時按
- **WHEN** 兩個裝置以同一個 `expected_revision` 同時送出重新生成
- **THEN** 只有一個認領成功，另一個得到 409（`turn_in_progress` 或 `revision_conflict`），只有一筆預扣

#### Scenario: 已被另一個裝置修訂完成
- **WHEN** 裝置甲完成重新生成（`revision` 變 2）後，裝置乙以 `expected_revision` 1 送出
- **THEN** 回 409 `revision_conflict`，乙的畫面以後端的內容為準

#### Scenario: 認領逾時被接手
- **WHEN** 一個認領超過逾時時間仍未完成，新的請求送出
- **THEN** 新請求接手（嘗試序號再加 1），舊執行者之後的替換失敗，舊的預扣由逾時規則退回

### Requirement: 修訂進行中不得開始新回合
修訂有未逾時的認領期間，同一存檔的 `turns/stream` SHALL 回 409 `turn_in_progress`；反之，同一存檔有未逾時的生成中回合時，修訂 SHALL 回 409 `not_revisable`（`in_progress`）。

#### Scenario: 修訂中送出新行動
- **WHEN** 最後一輪正在重新生成時玩家（例如另一個裝置）送出新行動
- **THEN** 回 409 `turn_in_progress`，沒有新的回合列、沒有預扣

### Requirement: 重新生成的免費次數與費用
每個回合前 N 次重新生成 SHALL 免費（N 由設定表現讀，初始 2），第 N+1 次起每次消耗一輪的幣價（現讀自設定表，初始 30）。次數 SHALL 以成功替換為準：成功的重新生成使該回合的已用次數加 1，失敗、中斷、被擋不加。免費的重新生成 SHALL NOT 預扣；付費者 SHALL 在認領的同一個交易內預扣，餘額不足 SHALL 回 409 `insufficient_coins` 且不認領。

#### Scenario: 前兩次免費
- **WHEN** 玩家對同一回合第 1 次與第 2 次重新生成
- **THEN** 兩次都不扣遊戲幣，帳本沒有新的預扣紀錄

#### Scenario: 第三次付費
- **WHEN** 玩家對同一回合第 3 次重新生成且餘額足夠
- **THEN** 預扣一輪的幣價，成功後結算

#### Scenario: 付費但餘額不足
- **WHEN** 第 3 次重新生成時餘額不足
- **THEN** 回 409 `insufficient_coins`，回合沒有被認領、免費次數不變

#### Scenario: 失敗不消耗免費次數
- **WHEN** 一次免費的重新生成被擋、中斷或失敗
- **THEN** 該回合的已用次數不變，下次仍是免費

### Requirement: 編輯輸入的費用與重置
編輯輸入 SHALL 一律預扣一輪的幣價（原本那輪已扣的幣不退），成功後使該回合的已用免費次數歸零。編輯的新行動 SHALL 與玩家送出行動走相同的清理與驗證（NFC、去除控制字元、1 至 1000 字）與規則層（命中必須拒絕的類別回 422 `input_blocked`，只帶類別），且 SHALL 不同於原行動（相同回 422）。

#### Scenario: 編輯扣幣並重置次數
- **WHEN** 已用 2 次免費重新生成的回合被編輯並成功
- **THEN** 扣一輪的幣價，已用次數歸零，之後再重新生成又有 N 次免費

#### Scenario: 行動沒有改變
- **WHEN** 編輯送出與原行動相同的文字
- **THEN** 回 422，沒有預扣、沒有認領

#### Scenario: 編輯的行動違規
- **WHEN** 編輯的行動命中必須拒絕的類別
- **THEN** 回 422 `input_blocked`（只有類別），沒有預扣、沒有認領

### Requirement: 重新生成理由的驗證與用途
重新生成 SHALL 帶分類（不符設定、劇情重複或無趣、錯字或文字品質、被轉場擋掉、其他）與理由文字（NFC 並去除控制字元後 5 至 50 個 Unicode 字元）；缺漏或不合 SHALL 回 422 並指出欄位。編輯 SHALL NOT 需要理由。理由 SHALL 只存入回饋表供營運檢視，SHALL NOT 進入任何模型呼叫、system_instruction 或上下文。

#### Scenario: 理由太短
- **WHEN** 理由少於 5 個字
- **THEN** 回 422 指出 `reason` 欄位，沒有認領

#### Scenario: 分類不在列舉內
- **WHEN** 送出未知的分類
- **THEN** 回 422 指出 `category` 欄位

#### Scenario: 理由不進模型
- **WHEN** 檢查重新生成送給模型的所有內容
- **THEN** 其中沒有理由與分類

### Requirement: 以回合前快照重算並原子替換
修訂 SHALL 以回合開始時保存的世界快照作為世界狀態、以不含目標回合的最近 10 個完成回合作為歷史、以目標回合原本的輪數重建上下文（重新生成沿用原行動，編輯使用新行動）。新回應 SHALL 完整通過輸出審查，且狀態呼叫成功並通過驗證後，才在單一交易內替換：回合內容、輪數與序號不變、`revision` 加 1、世界狀態寫回（以認領時的存檔版本比對，版本加 1）、預扣結算、回饋列標為 `applied`。新回應可提議結局，由 Service 照常確認後存檔完結。

#### Scenario: 還原再套用
- **WHEN** 最後一輪原本使地點改變、目標新增，玩家重新生成而新回應沒有這些變化
- **THEN** 替換後世界狀態沒有原本那輪造成的地點與目標變化，只有新回應的變化

#### Scenario: 輪數與序號不變
- **WHEN** 重新生成成功
- **THEN** 回合的輪數與序號與原本相同，`revision` 加 1，已完成輪數不變

#### Scenario: 狀態呼叫失敗
- **WHEN** 新文字通過審查但狀態呼叫失敗
- **THEN** 不替換，舊內容與舊世界狀態原樣保留，預扣退回，不消耗免費次數

#### Scenario: 世界版本在期間改變
- **WHEN** 寫回時存檔的世界版本已不是認領時的版本
- **THEN** 不替換（整個修訂視為失敗），舊內容保留，預扣退回

#### Scenario: 修訂後提議結局
- **WHEN** 新回應提議結局且劇本為自訂結局
- **THEN** 替換後存檔轉為已完結，`done` 狀態為 `ended`

### Requirement: 失敗、中斷與被擋時保留舊內容
修訂的新回應被審查擋下時，系統 SHALL 照回合的規則送出 `transition` 並自動重新生成一次（沿用同一筆預扣）；最終仍被擋、模型失敗或逾時、玩家中斷、非預期錯誤時，舊內容 SHALL 原樣保留、釋放認領、退回預扣、回饋列標為 `blocked` 或 `failed`、不消耗免費次數。新文字完整且通過審查之後才斷線者，收尾 SHALL 照常完成（成功即算用掉）。被擋的文字 SHALL NOT 保存，也 SHALL NOT 計入暫停對話的次數。

#### Scenario: 最終被擋
- **WHEN** 重新生成的新回應兩次都被審查擋下
- **THEN** 事件以 `done`（狀態 `blocked`）結束，舊內容與 `revision` 不變，預扣退回

#### Scenario: 串流中玩家斷線
- **WHEN** 新文字尚未完整時客戶端斷線
- **THEN** 認領被釋放、預扣退回，舊內容不變，回合仍可再次修訂

#### Scenario: 收尾階段斷線
- **WHEN** 新文字已完整且通過審查後客戶端斷線
- **THEN** 收尾照常完成，替換成功並結算

### Requirement: 預扣鍵含嘗試序號
修訂的遊戲幣預扣鍵 SHALL 為 `turn:{存檔識別碼}:{client_msg_id}:{attempt}`，`attempt` 隨每次認領加 1，使已退回的鍵不會被後來的修訂撿回，也不會與原回合已結算的鍵相同。付費重新生成與編輯 SHALL 使用同一套鍵。

#### Scenario: 退回後再次付費重新生成
- **WHEN** 第一次付費重新生成失敗並退回，玩家再次付費重新生成
- **THEN** 使用新的鍵重新預扣，淨扣一次

#### Scenario: 與原回合的鍵不同
- **WHEN** 對已結算的回合編輯
- **THEN** 預扣使用新的 `attempt`，不回傳原回合已結算的預扣

### Requirement: 記事簿不受修訂影響
修訂 SHALL NOT 讀寫記事簿，也 SHALL NOT 觸發記事簿抓取。記事簿的抓取視窗永遠不含最後一輪，所以修訂不需要回滾記事簿；邊界輪次（第 10、20 輪…）的抓取等下一輪完成才進行，使用的是修訂後的最終內容。

#### Scenario: 修訂不觸發抓取
- **WHEN** 任何修訂成功
- **THEN** 沒有記事簿抓取發生，`notebook_upto_turn` 與 `notebook_version` 不變

### Requirement: 理由回饋的保存、匯出與刪除
每次認領 SHALL 在同一個交易內寫入一列回饋（存檔、回合序號、嘗試序號、種類〔重新生成或編輯〕、分類與理由〔編輯沒有〕、是否免費、時間），結果欄位於收尾時標為 `applied`、`failed` 或 `blocked`；被接手的舊認領 SHALL 標為 `failed`。回饋 SHALL 隨存檔與帳號刪除而刪除，SHALL 納入個人資料匯出；被取代的輸入與輸出 SHALL NOT 保存。日誌 SHALL 只記內部編號、列舉與整數長度，SHALL NOT 記錄理由、行動與劇情。

#### Scenario: 失敗的理由也保留
- **WHEN** 重新生成失敗
- **THEN** 回饋列存在，結果為 `failed`，理由文字仍在

#### Scenario: 日誌不含理由
- **WHEN** 掃描一次重新生成全程的日誌
- **THEN** 沒有理由、行動與劇情的文字，只有編號、列舉與整數

#### Scenario: 刪除存檔
- **WHEN** 玩家刪除存檔或帳號
- **THEN** 該存檔的回饋列一併刪除

### Requirement: 修訂事件只用白名單欄位
修訂的 SSE 事件 SHALL 與回合串流相同種類（`meta`、`token`、`ping`、`transition`、`state`、`done`、`error`）。`meta` SHALL 帶 `kind`（`regenerate` 或 `edit`）、`attempt` 與修訂前的 `revision`；`done` SHALL 帶狀態（`complete`、`blocked`、`ended`）、輪數、剩餘遊戲幣、修訂後的 `revision` 與剩餘免費次數（`free_left`）。JSON 原文 SHALL NOT 以串流送給前端。

#### Scenario: 成功的事件序列
- **WHEN** 重新生成成功
- **THEN** 事件依序為 `meta`、若干 `token`、`state`、`done`，`done` 的 `revision` 比 `meta` 的大 1

#### Scenario: 被擋後保留舊內容的 done
- **WHEN** 重新生成最終被擋
- **THEN** `done` 的狀態為 `blocked`，`revision` 等於修訂前的值
