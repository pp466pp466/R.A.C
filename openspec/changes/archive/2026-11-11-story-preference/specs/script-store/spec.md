# Spec Delta

## ADDED Requirements

### Requirement: 版本的讀者頻道
每個劇本版本 SHALL 記錄讀者頻道（`all`、`male`、`female`），可為空；空值（遷移之前建立的版本）SHALL 視同全讀者，SHALL NOT 回填。讀者頻道屬版本內容，與版本的其他內容一起不可變：`published` 的版本不能更改，要改頻道只能由該版本建立新草稿、修改後重新送審。由版本還原的標準化內容（供開存檔、再驗證與匯出使用）SHALL 只在頻道為 `male` 或 `female` 時帶有 `audience`。

#### Scenario: 舊版本為空值
- **WHEN** 讀取遷移之前建立的版本
- **THEN** 其頻道視為全讀者，還原的標準化內容沒有 `audience`

#### Scenario: 新草稿的頻道
- **WHEN** 作者儲存一份 `audience` 為 `female` 的草稿
- **THEN** 該版本的頻道為 `female`，還原的標準化內容帶有 `audience: "female"`

#### Scenario: 已發佈版本不可改頻道
- **WHEN** 嘗試修改 `published` 版本的頻道
- **THEN** 被拒絕，版本內容不變

#### Scenario: 改頻道要出新版本
- **WHEN** 作者由已發佈的全讀者版本建立新草稿並改為 `male`，核可後上架
- **THEN** 新版本為 `male` 並取代舊版，舊版仍是全讀者且內容不變

### Requirement: 公開清單可依故事偏好排序
讀取上架版本清單的方法 SHALL 接受選填的故事偏好（`all`、`male`、`female`）；有給時依 `story-preference` 的排序規則在分頁之前排序，沒有給或為 `all` 時維持既有順序（發佈時間由新到舊）。依標籤查詢的清單 SHALL 同樣支援。排序 SHALL NOT 改變清單包含哪些版本（內容模式篩選與只回傳上架版本的規則不變）。

#### Scenario: 依偏好排序
- **WHEN** 以偏好 `male` 讀取上架清單，有男頻、全讀者、女頻各一份
- **THEN** 順序為男頻、全讀者、女頻

#### Scenario: 沒有偏好
- **WHEN** 不帶偏好讀取上架清單
- **THEN** 順序與加入此功能之前相同

#### Scenario: 排序不改變集合
- **WHEN** 同一位玩家以不同偏好讀取清單
- **THEN** 各次得到的版本集合相同，只有順序不同

#### Scenario: 依標籤查詢也排序
- **WHEN** 以偏好 `female` 依標籤讀取清單，符合的有男頻與女頻各一份
- **THEN** 女頻在前
