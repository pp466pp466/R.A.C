# Spec Delta

## MODIFIED Requirements

### Requirement: 劇本詳情只含公開的白名單欄位
`GET /api/sessions/catalog/<版本識別碼>` SHALL 只對 `published` 的版本回傳詳情，其他狀態（含擁有者自己的非上架版本）與不存在的識別碼 SHALL 得到相同的 404。回應 SHALL 只含：`id`、標題、簡介、世界名稱、標籤、筆名與短代號、內容模式標示、讀者頻道、版本編號、發佈時間、背景與體系、角色的姓名與職業、地點、是否有自訂結局、與目錄相同的 `content_mode`，以及 `report_state`（呼叫者對這個版本的檢舉狀態：`available` 可檢舉、`reported` 呼叫者已檢舉過、`unavailable` 不可檢舉——呼叫者自己的劇本或內建劇本）。SHALL NOT 含：角色的隱藏特質與個性、里程碑、結局說明文字、規則文字、初始對話、審查結果、內容雜湊、作者的使用者編號、任何檢舉的數量與內容，與任何內部整數編號。

#### Scenario: 詳情內容
- **WHEN** 玩家讀取上架版本的詳情
- **THEN** 有背景、角色姓名與職業、地點與標籤，沒有隱藏特質、里程碑與結局說明

#### Scenario: 已下架
- **WHEN** 玩家（含作者）讀取已下架版本的詳情
- **THEN** 回應 404

#### Scenario: 他人的草稿
- **WHEN** 玩家以他人草稿的識別碼讀取詳情
- **THEN** 回應 404，與不存在相同

#### Scenario: 不過年齡閘門
- **WHEN** 尚未完成年齡確認的管理員讀取詳情
- **THEN** 可以讀取（開存檔仍由既有閘門擋下）

#### Scenario: 可檢舉
- **WHEN** 玩家讀取他人上架劇本的詳情，且沒有檢舉過
- **THEN** `report_state` 為 `available`

#### Scenario: 已檢舉
- **WHEN** 玩家檢舉過這個版本後再讀取詳情
- **THEN** `report_state` 為 `reported`

#### Scenario: 自己的與內建的劇本
- **WHEN** 作者讀取自己上架劇本的詳情，或任何人讀取內建劇本的詳情
- **THEN** `report_state` 為 `unavailable`

#### Scenario: 不洩漏檢舉統計
- **WHEN** 一個版本已被許多玩家檢舉，另一位玩家讀取詳情
- **THEN** 回應沒有檢舉數量與檢舉內容
