# Spec Delta

## MODIFIED Requirements

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

## ADDED Requirements

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

## REMOVED Requirements

### Requirement: 可玩劇本清單的範圍
**Reason**: 內容模式由三種改為兩種（安全開、安全關），原需求的情境（依模式過濾劇本、未成年玩家、一般模式）已不存在，改以新標題的需求取代。
**Migration**: 見同一份 delta 中以新標題新增的需求。

### Requirement: 清單回應帶內容模式摘要
**Reason**: 內容模式由三種改為兩種（安全開、安全關），原需求的情境（依模式過濾劇本、未成年玩家、一般模式）已不存在，改以新標題的需求取代。
**Migration**: 見同一份 delta 中以新標題新增的需求。
