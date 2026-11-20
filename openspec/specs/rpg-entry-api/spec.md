# rpg-entry-api Specification

## Purpose

提供 RPG 入口畫面所需的兩個唯讀端點：玩家可以開存檔的上架劇本清單，與玩家自己的存檔清單。兩者只回卡片需要的欄位，不含劇本全文、審查結果、內部編號與他人的個資。

## Requirements

### Requirement: 清單依故事偏好排序並分頁
清單 SHALL 以玩家的故事偏好排序（符合偏好的頻道最前，其次全讀者，其餘最後；偏好為全讀者時不重排；同一層依發佈時間由新到舊），排序 SHALL 在分頁之前完成，使分頁之間的順序一致。偏好 SHALL 只影響順序，SHALL NOT 增減清單包含的版本。分頁 SHALL 以游標進行：`limit`（預設 12、至多 24）與 `cursor`（上一頁回應的 `next_cursor`）控制分頁，回應 SHALL 含 `has_more` 與 `next_cursor`（沒有更多時為空）；篩選與關鍵字見 `rpg-script-catalog`。`limit` 不是合法整數（含負數、超過上限）或 `cursor` 損毀 SHALL 回 400 並指出欄位；`offset` 參數 SHALL 不再被讀取。

#### Scenario: 依偏好排序
- **WHEN** 偏好為男頻的玩家讀取清單，有男頻、全讀者、女頻各一份上架版本
- **THEN** 順序為男頻、全讀者、女頻

#### Scenario: 偏好為全讀者
- **WHEN** 偏好為全讀者的玩家讀取清單
- **THEN** 順序為發佈時間由新到舊

#### Scenario: 分頁串起來等於一次取得
- **WHEN** 以 `limit=2` 沿著 `next_cursor` 依序讀取每一頁直到 `has_more` 為假
- **THEN** 各頁串起來的順序與一次取得全部的順序相同，沒有重複或遺漏

#### Scenario: 偏好不改變集合
- **WHEN** 同一位玩家把偏好依序改為男頻、女頻與全讀者並讀取全部頁面
- **THEN** 每次得到的版本集合相同

#### Scenario: 不合法的分頁參數
- **WHEN** `limit` 為 `0`、`25` 或 `abc`，或 `cursor` 為損毀的字串
- **THEN** 回應 400 並指出該欄位，不回傳清單

#### Scenario: 最後一頁
- **WHEN** 讀到最後一頁
- **THEN** `has_more` 為假，`next_cursor` 為空

### Requirement: 劇本卡片只含公開的摘要欄位
清單每一筆 SHALL 只含：`id`、標題、簡介、世界名稱、標籤、筆名與短代號（內建劇本的筆名為「官方」，短代號可為空）、內容模式的綁定（`content_tier`，值只會是 `safe`＝綁定安全模式或 `adult`＝未綁定，舊值 `standard` 以 `adult` 回傳）與讀者頻道（空值以 `all` 回傳）。回應 SHALL NOT 含背景、角色、里程碑、規則等劇本內文，SHALL NOT 含內容雜湊、審查結果、作者的使用者編號或任何內部整數編號。取得清單所需的標籤與作者資料 SHALL 以固定次數的查詢完成，不隨筆數增加查詢次數。

#### Scenario: 欄位白名單
- **WHEN** 讀取清單並檢查一筆資料的鍵
- **THEN** 鍵集合恰為卡片欄位，沒有內文、雜湊、審查與內部編號

#### Scenario: 舊版本的頻道
- **WHEN** 清單含一個遷移之前建立、頻道為空值的版本
- **THEN** 該筆的讀者頻道為 `all`

#### Scenario: 舊值的綁定
- **WHEN** 清單含 `content_tier` 儲存為 `standard` 的版本
- **THEN** 該筆的 `content_tier` 為 `adult`

#### Scenario: 內建劇本的署名
- **WHEN** 清單含內建的首發劇本
- **THEN** 筆名為「官方」

#### Scenario: 查詢次數固定
- **WHEN** 清單頁含 1 筆與含 12 筆各讀取一次
- **THEN** 兩次執行的資料庫查詢次數相同

### Requirement: 我的存檔清單
`GET /api/sessions` SHALL 回傳呼叫者自己的存檔，依最後活動由新到舊（同時間依編號由新到舊），不含已封存者；每筆 SHALL 只含：`id`（存檔的對外識別碼）、劇本標題、內容模式（現算的生效模式，含該存檔劇本的綁定與緊急煞車，不是開存檔當下的值）、狀態（`active` 或 `ended`）、主角姓名、已完成的輪數（只計完整的回合，不含第 0 幕、被擋與中斷者）與最後活動時間。`limit`（預設 20、至多 50）與 `offset` 控制分頁並含 `has_more`，不合法的值回 400。回應 SHALL NOT 含劇本複本全文、主角的其餘角色卡與錨定事實、記事簿、世界狀態、回合內容與內部編號。各存檔的已完成輪數 SHALL 以固定次數的查詢取得。

#### Scenario: 只含自己的
- **WHEN** 使用者 A 與 B 各有存檔，A 讀取清單
- **THEN** 只含 A 的存檔

#### Scenario: 由新到舊
- **WHEN** A 有三個存檔，依序最後活動為 1 月、3 月、2 月
- **THEN** 順序為 3 月、2 月、1 月

#### Scenario: 已完成輪數
- **WHEN** 一個存檔有 2 個完整的回合、1 個被擋與 1 個中斷的回合
- **THEN** 該筆的已完成輪數為 2

#### Scenario: 已完結的存檔
- **WHEN** A 有一個已完結的存檔
- **THEN** 該筆狀態為 `ended`，仍在清單中

#### Scenario: 欄位白名單
- **WHEN** 讀取清單並檢查一筆資料的鍵
- **THEN** 鍵集合恰為上述欄位，沒有劇本內文、角色卡細節、記事簿與內部編號

#### Scenario: 內容模式是現算的
- **WHEN** 玩家在安全模式開時開了存檔（劇本未綁定），之後關閉安全模式再讀取清單
- **THEN** 該筆的內容模式為 `adult`

#### Scenario: 綁定的劇本
- **WHEN** 存檔的劇本綁定安全模式，玩家已關閉安全模式
- **THEN** 該筆的內容模式為 `safe`

#### Scenario: 空清單
- **WHEN** 沒有任何存檔的玩家讀取清單
- **THEN** 回應 200，清單為空且 `has_more` 為假

### Requirement: 清單端點的存取規則
兩個端點 SHALL 要求已登入，受限登入狀態（被鎖定、等待刪除、待確認年齡）SHALL 依既有規則回 403 與對應代碼；未登入回 401。兩者是唯讀端點，SHALL NOT 呼叫任何模型、SHALL NOT 改動遊戲幣與額度，也 SHALL NOT 因年齡閘門而拒絕尚未完成年齡確認的管理員（管理員讀到的是安全模式的清單；開存檔仍由既有的閘門拒絕）。回應 SHALL 禁止快取。

#### Scenario: 未登入
- **WHEN** 沒有登入就請求任一端點
- **THEN** 回應 401

#### Scenario: 受限登入狀態
- **WHEN** 被鎖定、等待刪除或待確認年齡的玩家請求任一端點
- **THEN** 回應 403 與各自的代碼

#### Scenario: 不改動錢包
- **WHEN** 玩家讀取兩個清單
- **THEN** 其遊戲幣餘額與帳本不變，也沒有模型呼叫

#### Scenario: 禁止快取
- **WHEN** 讀取任一端點
- **THEN** 回應標頭禁止快取

### Requirement: 目錄回應帶安全模式摘要
`GET /api/sessions/catalog` 的回應 SHALL 含 `content_mode`，內有玩家目前的生效模式（不含劇本，`effective`）與緊急煞車是否啟用（`forced`），讓畫面能判斷「點選未綁定的劇本時是否要詢問修改安全模式」。該摘要 SHALL 與 `/api/account/content-mode` 一致，SHALL NOT 含生日、年齡、年齡區間，也 SHALL NOT 含 `is_adult`。

#### Scenario: 安全模式開的玩家
- **WHEN** 預設設定的玩家讀取清單
- **THEN** `content_mode.effective` 為 `safe`、`forced` 為假，且回應沒有生日、年齡與年齡區間

#### Scenario: 已關閉安全模式
- **WHEN** 已關閉安全模式並同意目前規範的玩家讀取清單
- **THEN** `content_mode.effective` 為 `adult`

#### Scenario: 緊急煞車
- **WHEN** 緊急煞車啟用
- **THEN** `content_mode.effective` 為 `safe`、`forced` 為真

### Requirement: 目錄含全部上架劇本
`GET /api/sessions/catalog` SHALL 回傳上架中（`published`）的劇本版本，不論劇本是否綁定安全模式、也不論玩家目前的內容模式：所有玩家都看得到所有上架的劇本。草稿、待審、被退回與已被取代的版本 SHALL NOT 出現，擁有者自己的非上架版本也不出現。每筆以版本的對外識別碼作為 `id`，該 `id` SHALL 可直接用於 `/api/sessions/setup/<id>` 與開存檔。

#### Scenario: 只含上架版本
- **WHEN** 資料庫中有一個上架版本、一個草稿、一個待審與一個已被取代的版本
- **THEN** 清單只含那個上架版本

#### Scenario: 安全模式的玩家看得到全部
- **WHEN** 安全模式開的玩家讀取清單，資料庫中有綁定安全模式與未綁定（含舊值 `standard`、`adult`）的上架版本
- **THEN** 清單含全部上架版本

#### Scenario: 關閉安全模式的玩家
- **WHEN** 已關閉安全模式的玩家讀取清單
- **THEN** 清單與安全模式開的玩家相同，不因模式而不同

#### Scenario: 自己的草稿不在清單
- **WHEN** 作者讀取清單，而他自己有一份草稿
- **THEN** 清單不含該草稿
