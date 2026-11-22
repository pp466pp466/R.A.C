# Spec Delta

## MODIFIED Requirements

### Requirement: 審核清單依狀態列出並排序
`GET /api/admin/scripts?status=` SHALL 接受 `pending_review`、`published`、`removed`、`reported`（預設 `pending_review`），其他值 SHALL 回 400。`pending_review` 的清單 SHALL 把模型層結果為 `flag` 的版本排最前，其餘依送審時間由舊到新（同時間依編號）；`published` 依發佈時間由新到舊；`removed` 依下架時間由新到舊；`reported` 只含上架中且有未結案檢舉的版本，排序見 `admin-script-reports`。`limit` 預設 50、至多 100，`offset` 預設 0。每筆 SHALL 只含：版本與劇本的對外識別碼、版本編號、標題、簡介、筆名與短代號、作者的對外識別碼（沒有作者的劇本為空，包含內建劇本與作者已註銷的劇本）、是否內建劇本（`is_builtin`，依種子鍵）、讀者頻道、內容模式、狀態、送審與發佈與決定的時間、模型層結果與有疑慮的類別（僅待審）、下架者與決定的類別、是否有審核備註，以及未結案的檢舉數（`report_count`）與是否有紅線理由的檢舉（`red_line_reported`）。清單 SHALL NOT 含劇本內文與審核備註全文。

#### Scenario: 有疑慮的排最前
- **WHEN** 待審有三份：甲（較舊、通過）、乙（較新、有疑慮）、丙（最新、通過）
- **THEN** 順序為乙、甲、丙

#### Scenario: 沒有疑慮時由舊到新
- **WHEN** 待審兩份都通過審查
- **THEN** 先送審的排在前面

#### Scenario: 清單沒有內文
- **WHEN** 讀取任一狀態的清單
- **THEN** 回應沒有背景、角色、里程碑與審核備註的文字

#### Scenario: 不合法的狀態
- **WHEN** `status` 為 `draft`
- **THEN** 回應 400 並指出該欄位

#### Scenario: 分頁
- **WHEN** 以 `limit=1&offset=1` 讀取有三份待審的清單
- **THEN** 回應排序後的第二份；`limit` 為 101 時回應 400

#### Scenario: 被檢舉狀態
- **WHEN** `status` 為 `reported`
- **THEN** 回應只含上架中且有未結案檢舉的版本

#### Scenario: 內建與已註銷作者的區別
- **WHEN** 清單同時有內建劇本與作者已註銷的劇本
- **THEN** 兩者的作者識別碼都為空，但內建劇本的 `is_builtin` 為 true、已註銷作者的為 false

#### Scenario: 計數鍵
- **WHEN** 讀取任一狀態的清單
- **THEN** 每一筆都有 `report_count`（整數）與 `red_line_reported`（布林）
