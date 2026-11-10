# rpg-notebook Specification

## Purpose
定義 RPG 記事簿（每個存檔一本的長期設定）的容量與狀態、玩家檢視與編輯的行為，以及隱私與日誌的限制。AI 如何每 10 輪追加重點另見 `rpg-notebook-capture`。

## Requirements

### Requirement: 容量依會員等級，只有一個來源
記事簿的容量 SHALL 依玩家目前的會員等級：free 2000、plus 5000、vip 10000 字；預備空間 SHALL 為容量的 25%（500、1250、2500 字），只供 AI 寫入，玩家編輯沒有預備空間。字數 SHALL 以 NFC 正規化後的 Unicode 字元計。容量 SHALL 由目前等級推得、不儲存；不認得的等級 SHALL 套用 free。容量的數值 SHALL 只有一個來源，客服對會員等級額度的說明 SHALL 使用同一個來源。

#### Scenario: 三個等級
- **WHEN** 查詢 free、plus、vip 的記事簿容量與預備空間
- **THEN** 依序得到 2000 與 500、5000 與 1250、10000 與 2500

#### Scenario: 不認得的等級
- **WHEN** 等級值不在三個等級之內
- **THEN** 套用 free 的容量

#### Scenario: 客服的額度說明
- **WHEN** 客服的額度說明被產生
- **THEN** 其中的記事簿容量等於上述容量表的值

### Requirement: 長度大於容量即為已滿
記事簿長度小於或等於容量時 SHALL 為正常；大於容量時 SHALL 為「已滿」。長度恰等於容量 SHALL 仍是正常。降級後長度超過新容量時 SHALL NOT 自動刪除，視同已滿；升級後 SHALL 立即以新容量判斷，AI 於下一個 10 輪起恢復更新。

#### Scenario: 剛好等於容量
- **WHEN** free 玩家的記事簿長度為 2000
- **THEN** 狀態為正常，不是已滿

#### Scenario: 超過一個字
- **WHEN** free 玩家的記事簿長度為 2001
- **THEN** 狀態為已滿

#### Scenario: 降級
- **WHEN** vip 玩家的記事簿有 3000 字，等級降為 free
- **THEN** 內容不變，狀態為已滿

#### Scenario: 升級
- **WHEN** free 玩家的記事簿有 2399 字（已滿），等級升為 plus
- **THEN** 狀態為正常，容量為 5000

### Requirement: 玩家可以檢視記事簿
系統 SHALL 提供 `GET /api/game/sessions/<id>/notebook`，回傳記事簿文字、版本、字數、容量、預備空間與是否已滿。端點 SHALL 要求登入並通過帳號與年齡閘門；`<id>` 為存檔的對外識別碼，非擁有者與不存在 SHALL 得到相同的 404；已完結的存檔 SHALL 仍可讀；回應 SHALL 禁止快取。

#### Scenario: 擁有者讀取
- **WHEN** 擁有者讀取新存檔的記事簿
- **THEN** 文字為空、版本為 1、字數為 0，並帶容量與預備空間

#### Scenario: 非擁有者
- **WHEN** 另一位玩家讀取該存檔的記事簿
- **THEN** 得到與不存在相同的 404

#### Scenario: 已完結的存檔
- **WHEN** 讀取已完結存檔的記事簿
- **THEN** 取得內容與版本

### Requirement: 玩家儲存編輯後的記事簿
系統 SHALL 提供 `PUT /api/game/sessions/<id>/notebook`（本文 `text` 與 `expected_version`），要求登入並通過 CSRF 與 Origin 檢查。文字 SHALL 先清理（NFC、統一換行為 `\n`、去除除換行外的控制字元、頭尾空白）；成功時 SHALL 以條件式寫入使版本加 1，回傳新的文字與版本。儲存 SHALL NOT 呼叫模型，也 SHALL NOT 改變已處理到的輪次。玩家 SHALL 可以清空記事簿。

#### Scenario: 成功儲存
- **WHEN** 玩家以正確的版本儲存長度在容量以內的文字
- **THEN** 回傳新文字與版本加 1，資料庫已更新，沒有任何模型呼叫

#### Scenario: 清理
- **WHEN** 儲存的文字含控制字元與頭尾空白
- **THEN** 儲存後的文字已去除這些字元

#### Scenario: 清空
- **WHEN** 玩家儲存空字串
- **THEN** 記事簿變為空，版本加 1

### Requirement: 儲存後的長度不得超過容量
儲存後的長度大於容量 SHALL 被拒絕為 422（代碼 `notebook_too_long`，帶字數與容量），資料不變；玩家編輯 SHALL NOT 使用預備空間。記事簿已滿時，玩家 SHALL 必須一次刪到容量以內才能儲存任何修改，不允許分次逐步刪減；儲存成功之後 AI 於下一個 10 輪起恢復更新。

#### Scenario: 超過容量
- **WHEN** free 玩家儲存 2001 字
- **THEN** 回應 422 與代碼 `notebook_too_long`，記事簿不變

#### Scenario: 已滿時分次刪減
- **WHEN** 長度為 2240 的記事簿，玩家把它刪到 2100 字後儲存
- **THEN** 被拒絕為 `notebook_too_long`，記事簿仍是 2240 字

#### Scenario: 一次刪到容量以內
- **WHEN** 長度為 2240 的記事簿，玩家把它刪到 2000 字後儲存
- **THEN** 儲存成功，狀態變為正常

#### Scenario: 已滿時未修改
- **WHEN** 已滿的記事簿原文原樣再儲存
- **THEN** 被拒絕為 `notebook_too_long`

### Requirement: 儲存時通過規則層
玩家儲存的文字 SHALL 通過伺服器端的規則層，只擋必須拒絕的類別（涉及未成年的性內容、性暴力、非自願的性情節、以真人名義的性內容）：命中時 SHALL 回應 422 與代碼 `notebook_blocked`，只帶類別、不回顯內容，記事簿不變。「分級不符」與有疑慮但非必須拒絕者 SHALL NOT 阻擋儲存。規則層 SHALL NOT 呼叫模型。

#### Scenario: 命中必須拒絕的類別
- **WHEN** 玩家儲存的文字命中必須拒絕的類別
- **THEN** 回應 422 與代碼 `notebook_blocked`，回應只含類別，記事簿不變

#### Scenario: 一般的設定文字
- **WHEN** 玩家儲存「師妹的父親是宗門長老，主角答應三年內回來」這類設定
- **THEN** 儲存成功

### Requirement: 版本不符時以後端為準
儲存時帶的 `expected_version` 與後端的 `notebook_version` 不一致 SHALL 回應 409（代碼 `notebook_conflict`），並附後端目前的文字與版本，資料不變；前端據此通知玩家並重新載入。AI 追加使版本加 1 之後，以舊版本儲存 SHALL 被拒絕，AI 追加的內容 SHALL NOT 被覆蓋。

#### Scenario: 編輯期間 AI 追加了內容
- **WHEN** 玩家開始編輯後，AI 追加了重點（版本加 1），玩家以舊版本儲存
- **THEN** 回應 409 與代碼 `notebook_conflict`，回應含後端最新的文字與版本，AI 追加的內容仍在

#### Scenario: 版本相符
- **WHEN** 玩家以目前版本儲存
- **THEN** 儲存成功

### Requirement: 已完結的存檔不可編輯
已完結的存檔 SHALL 拒絕記事簿的儲存（409，代碼 `session_ended`），資料不變；非擁有者 SHALL 得到 404。

#### Scenario: 已完結
- **WHEN** 玩家對已完結的存檔儲存記事簿
- **THEN** 回應 409 與代碼 `session_ended`，記事簿不變

### Requirement: 存檔快照含記事簿用量摘要
`GET /api/game/sessions/<id>` 的回應 SHALL 含記事簿的摘要：版本、字數、容量與是否已滿；SHALL NOT 含記事簿全文。

#### Scenario: 快照
- **WHEN** 讀取一個記事簿有 1800 字的 free 存檔
- **THEN** 摘要為字數 1800、容量 2000、未滿，且回應中沒有記事簿文字

### Requirement: 記事簿屬個資，日誌只記整數
記事簿內容由玩家文字衍生，SHALL 視為個資：隨存檔保存與刪除、納入個人資料匯出、不在管理員預設可檢視的範圍；日誌 SHALL 只記內部編號與整數長度，SHALL NOT 記錄記事簿內容。

#### Scenario: 日誌掃描
- **WHEN** 走過 AI 追加與玩家儲存後掃描全部日誌輸出
- **THEN** 找不到任何記事簿的文字

#### Scenario: 匯出與刪除
- **WHEN** 匯出個人資料，或刪除存檔
- **THEN** 匯出含記事簿全文；刪除後記事簿隨存檔消失

#### Scenario: 管理員
- **WHEN** 管理員使用後台功能
- **THEN** 沒有任何功能能讀取玩家的記事簿全文
