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
每個劇本 SHALL 有一個或多個版本，版本編號在同一劇本內從 1 起連續遞增且唯一。版本的狀態只有 `draft`、`pending_review`、`published`、`rejected`、`superseded`，且只允許下列轉移：`draft` 到 `pending_review`、`pending_review` 退回 `draft`（撤回）、`pending_review` 到 `published` 或 `rejected`、`published` 到 `superseded`。內建劇本的版本 SHALL 不經審核直接為 `published`。其餘轉移 SHALL 被拒絕，且版本保持原狀態。

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

### Requirement: 非草稿的內容不可變
只有 `draft` 版本的內容 SHALL 可以修改。版本離開 `draft` 之後（`pending_review`、`published`、`rejected`、`superseded`），其內容與標籤 SHALL NOT 被修改；要修改已發佈的內容 SHALL 建立新的版本。對非草稿版本的修改嘗試 SHALL 被拒絕且內容不變。

#### Scenario: 修改草稿
- **WHEN** 擁有者修改 `draft` 版本的標題
- **THEN** 修改成功

#### Scenario: 修改已發佈的版本
- **WHEN** 嘗試修改 `published` 版本的背景文字
- **THEN** 被拒絕，原內容不變

#### Scenario: 修改待審的版本
- **WHEN** 嘗試修改 `pending_review` 版本的內容
- **THEN** 被拒絕，要修改須先撤回為 `draft`

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
一個版本被核可為 `published` 時，同一劇本原本的 `published` 版本 SHALL 在同一個交易中改為 `superseded`；失敗時兩者 SHALL 都保持原狀態。被取代的版本 SHALL 保留其內容。

#### Scenario: 取代
- **WHEN** 劇本的第 2 版被核可，第 1 版原本是 `published`
- **THEN** 第 2 版為 `published`，第 1 版為 `superseded`，第 1 版的內容仍在

#### Scenario: 失敗時不留下半套
- **WHEN** 上架新版本的交易在中途失敗
- **THEN** 第 1 版仍為 `published`，第 2 版仍為 `pending_review`

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
非 `published` 的內容（`draft`、`pending_review`、`rejected`、`superseded`）SHALL 只能由擁有者讀取，所有這類讀取方法 SHALL 接收擁有者的使用者編號並以它為查詢條件；非擁有者 SHALL 得到與「不存在」相同的結果（404），不得洩漏它是否存在。`published` 的版本 SHALL 可由任何已登入且符合內容模式的使用者讀取（內建劇本亦同），但讀取方法 SHALL 只回傳上架中的版本，不提供只憑識別碼取得非公開內容的方法。管理員讀取他人的待審內容 SHALL 走專用路徑並寫入稽核，不在這個能力範圍內。

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
- **WHEN** 安全模式的玩家列出公開劇本，其中一個宣告為 `standard`
- **THEN** 清單中沒有該劇本

### Requirement: 只有草稿可以刪除
擁有者 SHALL 只能刪除自己的 `draft` 版本；刪除唯一的版本時 SHALL 一併刪除劇本本身。`pending_review`、`published`、`rejected` 與 `superseded` 版本 SHALL NOT 被擁有者刪除（下架與歷史保留由後續的審核與發佈流程處理）。非擁有者的刪除嘗試 SHALL 得到 404。

#### Scenario: 刪除草稿
- **WHEN** 擁有者刪除自己唯一的 `draft` 版本
- **THEN** 該版本與劇本都不存在

#### Scenario: 不得刪除已發佈的版本
- **WHEN** 擁有者嘗試刪除自己的 `published` 版本
- **THEN** 被拒絕，版本仍在

### Requirement: 刪除帳號時劇本一併消失
帳號被抹除時，其全部劇本、版本與標籤 SHALL 一併被刪除；外鍵的處理 SHALL 偏向刪除（串聯刪除），使任何遺漏的處理都不會留下作者的內容。其他玩家以這些劇本開出的存檔 SHALL 保留（見存檔的複本規則），存檔對版本的參照 SHALL 被清為空值而不是阻止刪除。

#### Scenario: 作者被抹除
- **WHEN** 一位有草稿與已發佈劇本的作者被抹除
- **THEN** 他的劇本、版本與標籤都不存在

#### Scenario: 別人的存檔保留
- **WHEN** 另一位玩家用該作者已發佈的劇本開過存檔，作者之後被抹除
- **THEN** 那份存檔仍在，其劇本內容的複本不變，對版本的參照為空值
