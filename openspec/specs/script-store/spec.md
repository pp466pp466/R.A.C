# script-store Specification

## Purpose
定義劇本與劇本版本的資料行為：身分與擁有者、版本的狀態與不可變、每個劇本至多一個上架版本與一個進行中的版本、筆名快照的鎖定、標籤，以及私人內容與公開內容的讀取範圍。創作介面、審核流程與公開目錄建立在這個基礎上，由後續的 change 提供。

## Requirements

### Requirement: 劇本的身分、擁有者與內建劇本
每個劇本 SHALL 有對外識別碼（UUID），網址與 API 只使用它，內部整數編號 SHALL NOT 對外暴露，授權 SHALL NOT 依賴識別碼的不可猜測性。玩家創作的劇本 SHALL 記錄作者；內建劇本沒有作者，並以穩定的種子鍵辨識，種子鍵 SHALL 唯一。劇本 SHALL 記錄筆名與短代號的快照，內建劇本的筆名為「官方」。

#### Scenario: 對外識別碼
- **WHEN** 建立一個劇本
- **THEN** 它有一個 UUID 的對外識別碼，且與其他劇本不同

#### Scenario: 內建劇本沒有作者
- **WHEN** 檢視一個由種子建立的內建劇本
- **THEN** 它沒有作者，有種子鍵，筆名為「官方」

#### Scenario: 種子鍵唯一
- **WHEN** 嘗試以已存在的種子鍵再建立一個劇本
- **THEN** 被拒絕，原有劇本不變

#### Scenario: 多個玩家劇本沒有種子鍵
- **WHEN** 兩位玩家各建立一個劇本，兩者都沒有種子鍵
- **THEN** 兩個都能建立（沒有種子鍵不互相衝突）

### Requirement: 版本的狀態與轉移
每個劇本 SHALL 有一個或多個版本，版本編號在同一劇本內從 1 起連續遞增且唯一。版本的狀態只有 `draft`、`pending_review`、`published`、`rejected`、`superseded`、`removed`，且只允許下列轉移：`draft` 到 `pending_review`、`pending_review` 退回 `draft`（撤回）、`pending_review` 到 `published` 或 `rejected`、`published` 到 `superseded` 或 `removed`（下架）、`removed` 到 `published`（恢復）或 `superseded`（被新的上架版本取代）。內建劇本的版本 SHALL 不經審核直接為 `published`。其餘轉移 SHALL 被拒絕，且版本保持原狀態。

#### Scenario: 版本編號連續
- **WHEN** 同一劇本依序建立三個版本
- **THEN** 版本編號為 1、2、3

#### Scenario: 合法的轉移
- **WHEN** 一個 `draft` 版本送審後被核可
- **THEN** 狀態依序為 `pending_review`、`published`

#### Scenario: 非法的轉移
- **WHEN** 嘗試把 `rejected` 的版本改為 `published`
- **THEN** 被拒絕，狀態仍為 `rejected`

#### Scenario: 內建劇本直接上架
- **WHEN** 種子建立內建劇本的第一個版本
- **THEN** 該版本直接為 `published`，沒有經過 `pending_review`

#### Scenario: 下架與恢復
- **WHEN** 一個 `published` 版本被下架後又被恢復
- **THEN** 狀態依序為 `removed`、`published`

#### Scenario: 草稿不能直接下架
- **WHEN** 嘗試把 `draft` 或 `pending_review` 的版本改為 `removed`
- **THEN** 被拒絕，狀態不變

#### Scenario: 下架的版本不能被退回
- **WHEN** 嘗試把 `removed` 的版本改為 `rejected` 或 `draft`
- **THEN** 被拒絕，狀態仍為 `removed`

### Requirement: 非草稿的內容不可變
只有 `draft` 版本的內容 SHALL 可以修改。版本離開 `draft` 之後（`pending_review`、`published`、`rejected`、`superseded`、`removed`），其內容與標籤 SHALL NOT 被修改；要修改已發佈的內容 SHALL 建立新的版本。對非草稿版本的修改嘗試 SHALL 被拒絕且內容不變。下架與恢復 SHALL NOT 改變版本的內容。

#### Scenario: 修改草稿
- **WHEN** 擁有者修改 `draft` 版本的標題
- **THEN** 修改成功

#### Scenario: 修改已發佈的版本
- **WHEN** 嘗試修改 `published` 版本的背景文字
- **THEN** 被拒絕，原內容不變

#### Scenario: 修改待審的版本
- **WHEN** 嘗試修改 `pending_review` 版本的內容
- **THEN** 被拒絕，要修改須先撤回為 `draft`

#### Scenario: 修改下架的版本
- **WHEN** 嘗試修改 `removed` 版本的標題
- **THEN** 被拒絕，原內容不變

#### Scenario: 下架與恢復不改內容
- **WHEN** 版本被下架後恢復
- **THEN** 內容雜湊與下架之前相同

### Requirement: 每個劇本至多一個上架版本與一個進行中的版本
同一劇本在同一時間 SHALL 至多有一個 `published` 版本，且至多有一個進行中的版本（`draft` 或 `pending_review`）。這兩項 SHALL 由資料庫的約束保證，而不只是靠程式檢查，使並行的請求也無法同時建立第二個。違反時 SHALL 回應衝突，不留下半套資料。

#### Scenario: 第二個進行中的版本
- **WHEN** 一個劇本已有 `draft` 版本，嘗試再建立另一個 `draft` 版本
- **THEN** 被拒絕為衝突，只剩原本的 `draft`

#### Scenario: 並行建立
- **WHEN** 兩個請求同時為同一劇本建立新版本
- **THEN** 最多一個成功，另一個得到衝突

#### Scenario: 已發佈版本與新草稿並存
- **WHEN** 劇本有一個 `published` 版本，擁有者建立新的 `draft`
- **THEN** 兩者並存，上架的仍是原本的 `published` 版本

### Requirement: 上架新版本時取代舊版
一個版本被核可為 `published` 時，同一劇本原本的 `published` 版本與 `removed` 版本 SHALL 在同一個交易中改為 `superseded`；失敗時各版本 SHALL 都保持原狀態。被取代的版本 SHALL 保留其內容。

#### Scenario: 取代
- **WHEN** 劇本的第 2 版被核可，第 1 版原本是 `published`
- **THEN** 第 2 版為 `published`，第 1 版為 `superseded`，第 1 版的內容仍在

#### Scenario: 失敗時不留下半套
- **WHEN** 上架新版本的交易在中途失敗
- **THEN** 第 1 版仍為 `published`，第 2 版仍為 `pending_review`

#### Scenario: 取代下架的舊版
- **WHEN** 劇本的第 1 版是 `removed`，第 2 版被核可
- **THEN** 第 2 版為 `published`，第 1 版為 `superseded`

### Requirement: 筆名快照在第一次發佈時鎖定
劇本 SHALL 記錄筆名與短代號的快照。劇本從未發佈過時，筆名 SHALL 可以被更新（重新取用目前的暱稱）；劇本的第一個版本被核可為 `published` 的同一個交易中，筆名 SHALL 被鎖定並記錄鎖定時間。鎖定之後 SHALL NOT 更改，之後的新版本沿用原筆名；更改嘗試 SHALL 被拒絕並說明已鎖定。

#### Scenario: 發佈前可更新
- **WHEN** 從未發佈過的劇本更新筆名
- **THEN** 更新成功，仍未鎖定

#### Scenario: 第一次發佈時鎖定
- **WHEN** 劇本的第一個版本被核可
- **THEN** 筆名被鎖定，並有鎖定時間

#### Scenario: 鎖定後不可更改
- **WHEN** 筆名已鎖定，嘗試更新
- **THEN** 被拒絕，筆名不變

#### Scenario: 新版本沿用
- **WHEN** 已鎖定筆名的劇本核可了第 2 版
- **THEN** 筆名仍是原本的值

### Requirement: 標籤
每個版本 SHALL 有自己的標籤，儲存為可查詢的獨立資料而不是放在 JSON 裡，使之後能依標籤篩選。同一版本內標籤 SHALL 唯一（標準化且不分大小寫），標籤的數量與長度依固定規格。標籤 SHALL 與其版本一起不可變。

#### Scenario: 標籤可查詢
- **WHEN** 兩個已發佈版本分別有標籤 `仙俠` 與 `科幻`
- **THEN** 依標籤 `仙俠` 查詢只得到第一個

#### Scenario: 同一版本不重複
- **WHEN** 同一版本嘗試加入兩個標準化後相同的標籤
- **THEN** 只保存一個

### Requirement: 讀取範圍與擁有者隔離
非 `published` 的內容（`draft`、`pending_review`、`rejected`、`superseded`、`removed`）SHALL 只能由擁有者讀取，所有這類讀取方法 SHALL 接收擁有者的使用者編號並以它為查詢條件；非擁有者 SHALL 得到與「不存在」相同的結果（404），不得洩漏它是否存在。`published` 的版本 SHALL 可由任何已登入的使用者讀取（內建劇本亦同），不依玩家的內容模式篩選，但讀取方法 SHALL 只回傳上架中的版本，不提供只憑識別碼取得非公開內容的方法。管理員讀取他人送審過的內容 SHALL 走專用路徑並寫入稽核（見 `admin-script-review`），不在這個能力範圍內。

#### Scenario: 擁有者讀自己的草稿
- **WHEN** 擁有者以對外識別碼讀取自己的 `draft`
- **THEN** 取得內容

#### Scenario: 非擁有者讀他人草稿
- **WHEN** 另一位使用者以正確的對外識別碼讀取別人的 `draft`
- **THEN** 得到與不存在相同的 404

#### Scenario: 公開讀取只回傳上架版本
- **WHEN** 劇本同時有 `published` 的第 1 版與 `draft` 的第 2 版，另一位玩家讀取公開內容
- **THEN** 只看到第 1 版

#### Scenario: 被取代的版本不公開
- **WHEN** 另一位玩家嘗試讀取已被取代的舊版本
- **THEN** 得到與不存在相同的結果

#### Scenario: 依內容模式篩選
- **WHEN** 安全模式開的玩家列出公開劇本，其中一個未綁定安全模式（含舊值 `standard`）
- **THEN** 清單中仍有該劇本

#### Scenario: 下架的版本不公開
- **WHEN** 另一位玩家嘗試讀取已下架的版本
- **THEN** 得到與不存在相同的結果

### Requirement: 只有草稿可以刪除
擁有者 SHALL 只能刪除自己的 `draft` 版本；刪除唯一的版本時 SHALL 一併刪除劇本本身。`pending_review`、`published`、`rejected`、`superseded` 與 `removed` 版本 SHALL NOT 被擁有者刪除（下架與歷史保留由審核與下架流程處理）。非擁有者的刪除嘗試 SHALL 得到 404。

#### Scenario: 刪除草稿
- **WHEN** 擁有者刪除自己唯一的 `draft` 版本
- **THEN** 該版本與劇本都不存在

#### Scenario: 不得刪除已發佈的版本
- **WHEN** 擁有者嘗試刪除自己的 `published` 版本
- **THEN** 被拒絕，版本仍在

#### Scenario: 不得刪除下架的版本
- **WHEN** 擁有者嘗試刪除自己的 `removed` 版本
- **THEN** 被拒絕，版本仍在

### Requirement: 刪除帳號時劇本一併消失
帳號被抹除時，其全部劇本、版本與標籤 SHALL 一併被刪除；外鍵的處理 SHALL 偏向刪除（串聯刪除），使任何遺漏的處理都不會留下作者的內容。其他玩家以這些劇本開出的存檔 SHALL 保留（見存檔的複本規則），存檔對版本的參照 SHALL 被清為空值而不是阻止刪除。

#### Scenario: 作者被抹除
- **WHEN** 一位有草稿與已發佈劇本的作者被抹除
- **THEN** 他的劇本、版本與標籤都不存在

#### Scenario: 別人的存檔保留
- **WHEN** 另一位玩家用該作者已發佈的劇本開過存檔，作者之後被抹除
- **THEN** 那份存檔仍在，其劇本內容的複本不變，對版本的參照為空值

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

### Requirement: 版本記錄送審時間與人工決定
每個版本 SHALL 可記錄送審時間（`pending_review` 時設定，無值的舊版本以建立時間代替）、決定的類別（人工退回或管理員下架時的固定列舉：審查類別加 `other_policy`）、決定的時間、下架者（`author` 或 `admin`）與審核備註（至多 500 字，只供管理員）。這些欄位 SHALL 是中繼資料，不是內容，SHALL NOT 影響內容雜湊；SHALL 只由版本的狀態轉移寫入；恢復上架時 SHALL 清除決定的類別、備註、時間與下架者。類別不在列舉內 SHALL 被拒絕。

#### Scenario: 送審時間
- **WHEN** 草稿送審
- **THEN** 版本有送審時間

#### Scenario: 退回記錄
- **WHEN** 待審版本被人工退回
- **THEN** 版本記有類別與決定時間，下架者為空，內容雜湊不變

#### Scenario: 恢復清除
- **WHEN** 管理員下架後又恢復版本
- **THEN** 決定的類別、備註、時間與下架者為空

#### Scenario: 不合法的類別
- **WHEN** 嘗試以列舉之外的類別退回
- **THEN** 被拒絕，狀態不變

### Requirement: 上架版本的唯一性在恢復時同樣成立
恢復上架 SHALL 受「每個劇本至多一個 `published` 版本」的資料庫約束保證：同劇本已有另一個 `published` 版本時，恢復 SHALL 被拒絕為衝突，版本保持 `removed`，不留下半套資料，並行的恢復與核可也 SHALL 至多一個成功。

#### Scenario: 並行的恢復與核可
- **WHEN** 劇本的 `removed` 版本被恢復的同時，另一個版本被核可
- **THEN** 最終同一劇本至多一個 `published` 版本：恢復的版本若來不及就得到衝突或已是 `superseded`，沒有兩個上架版本同時存在

#### Scenario: 衝突不留下半套
- **WHEN** 恢復因約束衝突失敗
- **THEN** 該版本仍為 `removed`，原上架版本不變
